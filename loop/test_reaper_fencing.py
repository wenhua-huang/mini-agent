"""
test_reaper_fencing.py — 第 4 步：reaper 守护扫描 + 窄窗口 fencing 对抗。

覆盖：
  A. find_zombie_tasks：活跃租约不报、过期才报；PENDING 排队不算僵尸；终态天然排除
  B. 审计事件：发现僵尸写 system_note，事后可查「何时被判定死亡」
  C. reap_once：接管后回 RUNNING、owner 换人、fencing +1；批量多任务
  D. CANCELLING 僵尸（点了取消后 worker 崩溃）：reap 接管 → loop 安全点收口 CANCELLED
  E. 窄窗口 1：append_event_guarded 在 owner 失配时拒绝写入
  F. 窄窗口 2（loop 级）：planner 长调用期间租约被抢，旧 worker 返回 lost_lease，
     事件流零污染，新 worker 能正常把任务跑完
  G. CLI：demo_worker.py reap 子命令作为独立进程完成接管
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import runtime as rt
from recoverable_loop import run_recoverable, rebuild_messages

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1; print("✓", msg)
    else:
        FAIL += 1; print("✗", msg)


def make_planner():
    def planner(messages):
        done = sum(1 for m in messages if m["role"] == "tool")
        if done == 0:
            return {"type": "tool", "name": "send_sms", "args": {"phone": "138****0001"}}
        return {"type": "final", "content": "完成"}
    return planner


def make_tools(counters):
    return {
        "send_sms": lambda args: counters.__setitem__("sms", counters["sms"] + 1)
            or {"sms_id": "SMS-1", "status": "sent"},
    }


def expire(conn, task_id):
    with conn:
        conn.execute("UPDATE tasks SET lease_expire_at=? WHERE task_id=?",
                     (time.time() - 1, task_id))


work = tempfile.mkdtemp(prefix="reaper_")

# ── A/B/C. reaper 基础能力 ─────────────────────────────────
conn = rt.connect(os.path.join(work, "a.db"))
t1 = rt.submit_task(conn, "任务1")
t2 = rt.submit_task(conn, "任务2")
t_pending = rt.submit_task(conn, "排队任务")  # 永不 claim，保持 PENDING
rt.claim_task(conn, "worker-1")  # t1
rt.claim_task(conn, "worker-1")  # t2

expect(rt.find_zombie_tasks(conn) == [], "租约活跃时没有僵尸")
expire(conn, t1)
zombies = rt.find_zombie_tasks(conn)
expect(zombies == [t1], "只有租约过期的 RUNNING 任务被判定僵尸（PENDING 排队不算）")
notes = [e for e in rt.replay_events(conn, t1) if e["event_type"] == "system_note"]
expect(len(notes) == 1 and notes[0]["payload"]["kind"] == "zombie_detected",
       "僵尸发现写入 system_note 审计事件（何时判定死亡有据可查）")

expire(conn, t2)
taken = rt.reap_once(conn, "reaper-1")
expect([t[0] for t in taken] == [t1, t2], "reap_once 按创建顺序批量接管两个僵尸")
row1 = rt.get_task(conn, t1)
expect(row1["status"] == rt.RUNNING and row1["lease_owner"] == "reaper-1"
       and row1["version"] == 3, "接管后回 RUNNING、owner 换人、fencing 升到 3")
expect(rt.get_task(conn, t_pending)["status"] == rt.PENDING,
       "PENDING 任务不被 reaper 当僵尸，但可被正常 claim 领取")

# 终态任务永不进僵尸名单
rt.finish_task(conn, t1, "reaper-1", 3, rt.SUCCEEDED)
expect(rt.find_zombie_tasks(conn) == [], "SUCCEEDED 终态天然排除在僵尸扫描外")

# ── D. CANCELLING 僵尸：取消后崩溃，reaper 接管并收口 ────────
conn_d = rt.connect(os.path.join(work, "d.db"))
td = rt.submit_task(conn_d, "取消后崩溃")
_, vd = rt.claim_task(conn_d, "worker-1")
rt.request_cancel(conn_d, td)          # 用户点取消 → CANCELLING
expect(rt.get_task(conn_d, td)["status"] == rt.CANCELLING, "取消请求置 CANCELLING")
expire(conn_d, td)                     # worker-1 没来得及收口就死了
z = rt.find_zombie_tasks(conn_d)
expect(z == [td], "CANCELLING + 租约过期同样被判僵尸（本步修复的漏洞）")
_, new_v = rt.claim_task(conn_d, "reaper-2")
outcome = run_recoverable(conn_d, td, "reaper-2", new_v, make_planner(),
                          make_tools({"sms": 0}))
expect(outcome == "cancelled"
       and rt.get_task(conn_d, td)["status"] == rt.CANCELLED,
       "接管者在安全点 1b 读到取消标志，收口为 CANCELLED，任务不再永久卡死")

# ── E. 窄窗口：guarded 写入的身份校验 ───────────────────────
conn_e = rt.connect(os.path.join(work, "e.db"))
te = rt.submit_task(conn_e, "窄窗口")
_, ve = rt.claim_task(conn_e, "worker-1")
try:
    rt.append_event_guarded(conn_e, te, "assistant", {"content": "x"},
                            "worker-9", ve)  # 冒充者
    expect(False, "非 owner 写入应被拒绝")
except RuntimeError:
    expect(True, "非 owner 的 guarded 写入被 fencing 拒绝")
try:
    rt.append_event_guarded(conn_e, te, "assistant", {"content": "x"},
                            "worker-1", ve + 1)  # 旧代次
    expect(False, "旧代次写入应被拒绝")
except RuntimeError:
    expect(True, "旧 fencing 代次的 guarded 写入被拒绝")
seq = rt.append_event_guarded(conn_e, te, "assistant", {"content": "ok"},
                              "worker-1", ve)
expect(seq == 1, "owner+代次匹配时 guarded 写入正常，seq=1")

# ── F. loop 级窄窗口：planner 长调用期间被接管 ──────────────
conn_f = rt.connect(os.path.join(work, "f.db"))
tf = rt.submit_task(conn_f, "长调用劫持")
_, vf = rt.claim_task(conn_f, "worker-1")
counters_f = {"sms": 0}

def hijacking_planner(messages):
    # 模拟「通过安全点后，LLM 长调用期间」发生的事：租约过期 + reaper 接管
    expire(conn_f, tf)
    _, vf_new = rt.claim_task(conn_f, "reaper-3")
    assert vf_new == vf + 1
    return {"type": "tool", "name": "send_sms", "args": {"phone": "138****0001"}}

outcome_f = run_recoverable(conn_f, tf, "worker-1", vf, hijacking_planner,
                            make_tools(counters_f))
expect(outcome_f == "lost_lease", "旧 worker 写入时发现失配，返回 lost_lease 静默退出")
expect(counters_f["sms"] == 0, "旧 worker 的工具结果被 fencing 拦截，幂等表零污染")
row_f = rt.get_task(conn_f, tf)
expect(row_f["lease_owner"] == "reaper-3", "任务归属完好，仍是 reaper-3")

# 新 owner 用全新连接视角把任务跑完
outcome_f2 = run_recoverable(conn_f, tf, "reaper-3", vf + 1,
                             make_planner(), make_tools(counters_f))
expect(outcome_f2 == "succeeded", "新 worker 不受旧 worker 干扰，正常跑到 SUCCEEDED")
expect(counters_f["sms"] == 1, "短信只由新 worker 真实发送 1 次")
roles = [m["role"] for m in rebuild_messages(rt.replay_events(conn_f, tf))]
expect(roles == ["user", "assistant", "tool", "assistant"],
       f"事件流视图干净无孤儿污染：{roles}")

# ── G. 真进程 reaper CLI ───────────────────────────────────
g_work = tempfile.mkdtemp(prefix="reaper_cli_")
g_db = os.path.join(g_work, "tasks.db")
py = os.path.abspath(os.path.join(HERE, "..", ".venv", "bin", "python"))
worker_py = os.path.join(HERE, "demo_worker.py")

def cli(*args):
    p = subprocess.run([py, worker_py, "--db", g_db, *args],
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout.strip()

code, out = cli("submit", "--input", "CLI reaper 测试")
tg = out
cli("run", "--worker", "worker-1", "--crash", "call-0:send_sms:AFTER_TOOL")
code, out_none = cli("reap", "--worker", "reaper-x")
expect(code == 0 and out_none == "NONE", "租约未过期时 reap 返回 NONE（不错杀活跃 worker）")
cli("expire", "--task-id", tg)
code, out_reap = cli("reap", "--worker", "reaper-x")
expect(code == 0 and tg in out_reap and "fencing_version=3" in out_reap,
       f"独立进程 reaper 接管僵尸成功：{out_reap}")
code, out_run = cli("run", "--worker", "reaper-x", "--task-id", tg)
expect(code == 0 and "outcome=succeeded" in out_run,
       "reaper 接管后显式续跑成功（同 owner 不二次 claim，幂等保证短信不重发）")
# 归属防线：别的 worker 不能抢跑别人持有的任务
code, out_steal = cli("run", "--worker", "thief", "--task-id", tg)
expect(code == 3 and "NOT_OWNED" in out_steal,
       "非 owner 显式续跑被 NOT_OWNED 拒绝（退出码 3）")

print(f"\n{'='*55}\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
