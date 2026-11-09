"""
runtime.py — 可恢复工作流运行时（第 1 步：实体化 + 事件流 + 租约 + 幂等基座）。

把「一次 run_agent() 函数调用」升级成「表里的一行任务」：
  tasks         任务实体：状态机六态 + 租约 + 取消标志 + 乐观锁版本号（fencing token）
  task_events   事件流：每条消息/工具结果 append 一行，按 seq 回放重建内存 messages
  tool_invocations  工具幂等去重：同一 (task_id, idem_key) 的副作用只执行一次

三问的分工（别混）：
  状态机      解决「现在算什么情况」（tasks.status）
  事件流      解决「崩了从哪接着跑」（task_events 回放）
  租约/心跳   解决「谁有资格跑」（lease_owner + lease_expire_at）
  幂等表      解决「重放会不会重复闯祸」（tool_invocations）

本模块只做存储与状态原语，不依赖 LLM，离线可测；agent loop 包装在第 2 步。
"""
import json
import os
import sqlite3
import time
import uuid

# 状态机六态（PENDING/RUNNING/CANCELLING/CANCELLED/SUCCEEDED/FAILED）
PENDING, RUNNING, CANCELLING = "PENDING", "RUNNING", "CANCELLING"
CANCELLED, SUCCEEDED, FAILED = "CANCELLED", "SUCCEEDED", "FAILED"
ACTIVE_STATES = (PENDING, RUNNING, CANCELLING)          # 可被 worker 领取/接管
TERMINAL_STATES = (CANCELLED, SUCCEEDED, FAILED)        # 终态，任何 worker 不得再动


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id          TEXT PRIMARY KEY,
    user_input       TEXT NOT NULL,
    status           TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_expire_at  REAL,
    version          INTEGER NOT NULL DEFAULT 1,   -- 乐观锁/fencing token，每次接管 +1
    error            TEXT,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,                   -- 任务内单调递增，回放顺序
    event_type TEXT NOT NULL,                      -- user/assistant/tool_call/tool_result/system_note
    payload    TEXT NOT NULL,                      -- JSON 文本
    created_at REAL NOT NULL,
    UNIQUE(task_id, seq)
);
CREATE TABLE IF NOT EXISTS tool_invocations (
    task_id  TEXT NOT NULL,
    idem_key TEXT NOT NULL,                       -- 确定性派生：hash(task_id+step+tool)，非随机
    status   TEXT NOT NULL,                       -- DONE / FAILED
    result   TEXT NOT NULL,                       -- 首次执行结果的 JSON，重放直接返回
    PRIMARY KEY (task_id, idem_key)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """建连接并初始化表。WAL 让崩溃后不易锁库，新文件自动建表。"""
    fresh = not os.path.exists(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if fresh:
        conn.executescript(SCHEMA)
        conn.commit()
    else:
        conn.executescript(SCHEMA)  # IF NOT EXISTS，老库幂等
    return conn


def submit_task(conn: sqlite3.Connection, user_input: str) -> str:
    """
    先落库再受理：任务行(PENDING) + seq=0 的 user 事件在同一事务提交。
    提交成功后调用方才可把 task_id 返回客户端（submit→poll 模式）。
    """
    task_id = uuid.uuid4().hex
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO tasks(task_id, user_input, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, user_input, PENDING, now, now),
        )
        conn.execute(
            "INSERT INTO task_events(task_id, seq, event_type, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, 0, "user", json.dumps({"content": user_input}, ensure_ascii=False), now),
        )
    return task_id


def claim_task(conn: sqlite3.Connection, worker_id: str, lease_seconds: float = 30.0):
    """
    原子领取一个可跑任务：PENDING，或 RUNNING/CANCELLING 但租约已过期的崩溃任务。
    返回 (task_id, fencing_version)；无任务返回 (None, None)。

    关键规则：
      - 租约未过期绝不接管（防双跑：旧 worker 可能只是卡顿）
      - 每次接管 version+1，作为 fencing token 交给 worker，后续状态回写必须带它
      - 终态任务永不重领
    """
    now = time.time()
    with conn:
        row = conn.execute(
            "SELECT task_id, version FROM tasks "
            "WHERE status IN (?, ?) "
            "AND (lease_expire_at IS NULL OR lease_expire_at < ?) "
            "ORDER BY created_at LIMIT 1",
            (PENDING, RUNNING, now),
        ).fetchone()
        if row is None:
            return None, None
        task_id, old_version = row["task_id"], row["version"]
        new_version = old_version + 1
        conn.execute(
            "UPDATE tasks SET status=?, lease_owner=?, lease_expire_at=?, "
            "version=?, updated_at=? WHERE task_id=? AND version=?",
            (RUNNING, worker_id, now + lease_seconds, new_version, now, task_id, old_version),
        )
        return task_id, new_version


def renew_lease(conn: sqlite3.Connection, task_id: str, worker_id: str,
                fencing_version: int, lease_seconds: float = 30.0) -> bool:
    """心跳/续租。只有当前 owner 且 fencing token 匹配才续得上；返回是否成功。"""
    now = time.time()
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET lease_expire_at=?, updated_at=? "
            "WHERE task_id=? AND lease_owner=? AND version=? AND status NOT IN (?,?,?)",
            (now + lease_seconds, now, task_id, worker_id, fencing_version,
             *TERMINAL_STATES),
        )
        return cur.rowcount == 1


def request_cancel(conn: sqlite3.Connection, task_id: str) -> bool:
    """
    用户点取消：只置标志位 + 转 CANCELLING，绝不假装已停下。
    已终态的任务返回 False。真正的 CANCELLED 由 worker 在安全点收口后写入。
    """
    now = time.time()
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET cancel_requested=1, status=?, updated_at=? "
            "WHERE task_id=? AND status NOT IN (?,?,?)",
            (CANCELLING, now, task_id, *TERMINAL_STATES),
        )
        return cur.rowcount == 1


def should_cancel(conn: sqlite3.Connection, task_id: str,
                  worker_id: str, fencing_version: int) -> bool:
    """安全点检查：worker 每轮 LLM/工具调用前调用，决定要不要走取消收口。"""
    row = conn.execute(
        "SELECT cancel_requested, lease_owner, version, status FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return True  # 任务行都没了，按停止处理（fail-closed）
    if row["lease_owner"] != worker_id or row["version"] != fencing_version:
        return True  # 已被别的 worker 接管（fencing 失配），旧 worker 必须停手
    return bool(row["cancel_requested"])


def finish_task(conn: sqlite3.Connection, task_id: str, worker_id: str,
                fencing_version: int, status: str, error: str = "") -> bool:
    """终态回写（SUCCEEDED/FAILED/CANCELLED），带 fencing 校验，旧 owner 写入被拒。"""
    assert status in TERMINAL_STATES
    now = time.time()
    with conn:
        cur = conn.execute(
            "UPDATE tasks SET status=?, error=?, lease_owner=NULL, lease_expire_at=NULL, "
            "updated_at=? WHERE task_id=? AND lease_owner=? AND version=?",
            (status, error, now, task_id, worker_id, fencing_version),
        )
        return cur.rowcount == 1


def append_event(conn: sqlite3.Connection, task_id: str, event_type: str, payload: dict) -> int:
    """追加一条事件，seq 在事务内取 max+1 保证连续；返回新 seq。"""
    now = time.time()
    payload_json = json.dumps(payload, ensure_ascii=False)
    with conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) AS m FROM task_events WHERE task_id=?",
            (task_id,),
        ).fetchone()
        seq = row["m"] + 1
        conn.execute(
            "INSERT INTO task_events(task_id, seq, event_type, payload, created_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, seq, event_type, payload_json, now),
        )
    return seq


def replay_events(conn: sqlite3.Connection, task_id: str):
    """按 seq 回放全部事件，恢复者据此重建内存 messages。"""
    rows = conn.execute(
        "SELECT seq, event_type, payload, created_at FROM task_events "
        "WHERE task_id=? ORDER BY seq",
        (task_id,),
    ).fetchall()
    return [
        {"seq": r["seq"], "event_type": r["event_type"],
         "payload": json.loads(r["payload"]), "created_at": r["created_at"]}
        for r in rows
    ]


def get_task(conn: sqlite3.Connection, task_id: str):
    return conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()


def run_idempotent(conn: sqlite3.Connection, task_id: str, idem_key: str, fn):
    """
    工具调用的幂等闸门：
      首次：执行 fn() 拿真实结果，落 tool_invocations（DONE + 结果）
      重放：不执行 fn，直接返回缓存结果 → 短信/付款等副作用绝不重复
    返回 (result, replayed)；fn 抛异常时记 FAILED 并照常抛出（允许重试）。
    """
    cached = conn.execute(
        "SELECT status, result FROM tool_invocations WHERE task_id=? AND idem_key=?",
        (task_id, idem_key),
    ).fetchone()
    if cached is not None and cached["status"] == "DONE":
        return json.loads(cached["result"]), True
    try:
        result = fn()
    except Exception as exc:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tool_invocations(task_id, idem_key, status, result) "
                "VALUES (?,?,?,?)",
                (task_id, idem_key, "FAILED", json.dumps({"error": str(exc)}, ensure_ascii=False)),
            )
        raise
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO tool_invocations(task_id, idem_key, status, result) "
            "VALUES (?,?,?,?)",
            (task_id, idem_key, "DONE", json.dumps(result, ensure_ascii=False)),
        )
    return result, False
