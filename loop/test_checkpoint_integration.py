"""
test_checkpoint_integration.py — checkpoint 长进 run_agent 的集成测试（不花 token）。

验证同一份 run_agent（不是平行实现）具备：
  1. 默认路径零变化：不给 checkpoint_db 时完全不碰 SQLite
  2. 开启后：任务 SUCCEEDED、事件流可回放、消息配对合法
  3. 崩溃恢复：工具执行后进程死亡，重跑 run_agent(resume_task_id=...) 时
     幂等缓存命中，工具副作用零重复（e2e 用 subprocess 真杀进程）
  4. 取消：request_cancel 后下一轮安全点收口 CANCELLED，后续模型不再被调用
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import mini_agent as ma
import runtime as rt
from test_loop_offline import (
    FakeFunction, FakeToolCall, FakeMessage, FakeResponse, SCRIPTED,
)

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1; print("✓", msg)
    else:
        FAIL += 1; print("✗", msg)


work = tempfile.mkdtemp(prefix="ckpt_integ_")


def fake_chat_factory(script):
    seq = {"i": 0}
    def _fake(messages):
        resp = script[seq["i"]]
        seq["i"] += 1
        return resp
    return _fake


# ── 1. 默认路径不碰 SQLite（旧行为）────────────────────────
ma._chat_create = fake_chat_factory([
    FakeResponse(FakeMessage(content="无 checkpoint 的答案"), tokens=50),
])
out = ma.run_agent(task="随便问", cwd=work, approver=lambda *a, **k: ("allow", ""))
expect(out == "无 checkpoint 的答案", "不给 checkpoint_db：旧路径照常跑通")

# ── 2. 开启 checkpoint：落库 + 回放合法 ────────────────────
db1 = os.path.join(work, "t1.db")
ma._chat_create = fake_chat_factory([r for r in SCRIPTED])  # read_file → 终答
out = ma.run_agent(task="读 INDEX", cwd=work,
                   approver=lambda *a, **k: ("allow", ""),
                   checkpoint_db=db1)
expect("笔记目录" in out, "checkpoint 模式正常拿到终答")
conn = rt.connect(db1)
tasks = conn.execute("SELECT task_id, status, cancel_requested FROM tasks").fetchall()
expect(len(tasks) == 1 and tasks[0]["status"] == rt.SUCCEEDED,
       f"任务终态 SUCCEEDED（实际 {tasks[0]['status'] if tasks else None}）")
events = rt.replay_events(conn, tasks[0]["task_id"])
types = [e["event_type"] for e in events]
expect(types == ["user", "assistant", "tool", "assistant"],
       f"事件流四类齐全且有序：{types}")
# 配对契约：每条 assistant 的 tool_call 都能找到同 id 的 tool 事件
assistant_tc_ids = {
    tc["id"] for e in events if e["event_type"] == "assistant"
    for tc in (e["payload"]["message"].get("tool_calls") or [])
}
tool_ids = {e["payload"]["message"]["tool_call_id"]
            for e in events if e["event_type"] == "tool"}
expect(assistant_tc_ids <= tool_ids and assistant_tc_ids,
       f"tool_call/tool 配对完整：{assistant_tc_ids} ⊆ {tool_ids}")
# 幂等表确实占了坑
idem_rows = conn.execute("SELECT idem_key, status FROM tool_invocations").fetchall()
expect(len(idem_rows) == 1 and idem_rows[0]["status"] == "DONE",
       f"真实工具执行在幂等表占坑：{[dict(r) for r in idem_rows]}")
conn.close()

# ── 3. 真进程崩溃恢复（subprocess + os._exit）──────────────
# 独立 worker 脚本放临时目录：第一次调用 read_file 后真死
db2 = os.path.join(work, "t2.db")
worker_script = os.path.join(work, "crash_worker.py")
marker = os.path.join(work, "side_effect.log")
with open(worker_script, "w", encoding="utf-8") as f:
    f.write(f"""
import os, sys
sys.path.insert(0, {HERE!r})
import mini_agent as ma
from test_loop_offline import FakeFunction, FakeToolCall, FakeMessage, FakeResponse

MARKER = {marker!r}

def fake_read(path, cwd=".", offset=1, limit=2000):
    # AFTER_TOOL 语义：工具正常返回（副作用写 marker），不在这里杀进程；
    # 由 run_tool_idempotent 写入 DONE 后、tool 事件落盘前的注入点杀
    with open(MARKER, "a") as g:
        g.write(f"read pid={{os.getpid()}}\\n")
    return "文件内容：INDEX"
ma.read_file = fake_read

# AFTER_TOOL 崩溃注入：包装 CheckpointStore.run_tool_idempotent，
# DONE 已落盘、调用方还没写 tool_result 事件时 os._exit（只在首次进程触发一次）
_orig_run = ma.CheckpointStore.run_tool_idempotent
def crash_after_idempotent(self, idem_key, fn):
    result, replayed = _orig_run(self, idem_key, fn)
    if not replayed and not os.path.exists(MARKER + ".crashed"):
        open(MARKER + ".crashed", "w").write("1")
        sys.stdout.flush()
        os._exit(1)
    return result, replayed
ma.CheckpointStore.run_tool_idempotent = crash_after_idempotent
ma._chat_create = lambda msgs: FakeResponse(FakeMessage(tool_calls=[
    FakeToolCall("call_1", "read_file", '{{"path": "INDEX.md"}}')
]), tokens=100) if not any(m.get("role") == "tool" for m in msgs) else \\
    FakeResponse(FakeMessage(content="恢复后完成"), tokens=60)

tid = sys.argv[1] if len(sys.argv) > 1 else ""
resume = sys.argv[2] if len(sys.argv) > 2 else ""
ma.run_agent(task="读文件（崩溃演示）", cwd={work!r},
             approver=lambda *a, **k: ("allow", ""),
             checkpoint_db={db2!r},
             resume_task_id=resume)
# 把 task_id 打印给父进程（新建模式从库里读唯一任务）
import runtime as _rt
_c = _rt.connect({db2!r})
_row = _c.execute("SELECT task_id FROM tasks LIMIT 1").fetchone()
print("TASK_ID=" + _row["task_id"])
""")

py = os.path.abspath(os.path.join(HERE, "..", ".venv", "bin", "python"))
# 第一次跑：崩溃退出码 1
p1 = subprocess.run([py, worker_script], capture_output=True, text=True, timeout=60)
expect(p1.returncode == 1, f"首次运行真崩溃 exit=1（实际 {p1.returncode}）")
with open(marker, encoding="utf-8") as f:
    first_marker = f.read().strip()
expect("pid=" in first_marker, f"崩溃前副作用已发生：{first_marker}")

# 查僵尸任务，模拟租约过期后由恢复进程接管
conn2 = rt.connect(db2)
zombie = conn2.execute("SELECT task_id, status FROM tasks").fetchone()
expect(zombie["status"] == rt.RUNNING, f"崩溃后任务卡 RUNNING（实际 {zombie['status']}）")
conn2.close()

# 第二次跑：同库 resume，CheckpointStore 强制过期旧租约后 claim
p2 = subprocess.run([py, worker_script, "resume", zombie["task_id"]],
                    capture_output=True, text=True, timeout=60)
expect(p2.returncode == 0 and "恢复后完成" in p2.stdout,
       f"恢复进程跑到终答：{p2.stdout[-120:]}")
with open(marker, encoding="utf-8") as f:
    lines = [l for l in f.read().splitlines() if l.strip()]
expect(len(lines) == 1,
       f"★ read_file 全程只真实执行 1 次（marker {len(lines)} 行），重放走幂等缓存")
conn2 = rt.connect(db2)
row = rt.get_task(conn2, zombie["task_id"])
expect(row["status"] == rt.SUCCEEDED and row["version"] == 3,
       f"恢复后 SUCCEEDED，fencing 2→3（实际 v{row['version']}）")
replayed = [e for e in rt.replay_events(conn2, zombie["task_id"])
            if e["event_type"] == "tool"]
expect(replayed and replayed[0]["payload"]["replayed"] is True,
       "tool 事件带 replayed=true，审计上可区分真实执行与缓存回放")

# ── 4. 取消：安全点收口，模型不再被调用 ───────────────────
db3 = os.path.join(work, "t3.db")
llm_calls = {"n": 0}
def cancel_chat(messages):
    llm_calls["n"] += 1
    if llm_calls["n"] == 1:
        return SCRIPTED[0]  # 第一轮请求工具
    raise AssertionError("取消后不应再调用模型")

ma._chat_create = cancel_chat
# 先建任务再置取消标志：通过直接操作存储模拟「第一轮工具后用户点取消」
store_con = rt.connect(db3)
tid3 = rt.submit_task(store_con, "取消演示")
rt.request_cancel(store_con, tid3)
store_con.close()

# run_agent 恢复路径会先 claim；CANCELLING+过期租约可被接管，首轮安全点即停
# 手动过期以满足 claim 条件
store_con = rt.connect(db3)
with store_con:
    store_con.execute("UPDATE tasks SET lease_expire_at=0 WHERE task_id=?", (tid3,))
store_con.close()
out3 = ma.run_agent(task="取消演示", cwd=work,
                    approver=lambda *a, **k: ("allow", ""),
                    checkpoint_db=db3, resume_task_id=tid3)
expect("已取消" in out3 and llm_calls["n"] == 0,
       f"取消任务在首轮安全点收口，零模型调用（实际 {llm_calls['n']} 次）")
conn3 = rt.connect(db3)
expect(rt.get_task(conn3, tid3)["status"] == rt.CANCELLED, "终态 CANCELLED")
conn3.close()

print(f"\n{'='*55}\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
