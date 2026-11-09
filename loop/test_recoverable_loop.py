"""
test_recoverable_loop.py — 第 2 步离线测试：可恢复 Agent 循环。

覆盖：
  1. 正常跑完：两工具各执行一次，SUCCEEDED，事件流配对完整
  2. AFTER_TOOL 崩溃：工具已执行+占幂等坑后死亡，重放零重复（replayed=True）
  3. BEFORE_TOOL 崩溃：tool_call 已落盘但工具未执行，未配对事件被截掉，
     planner 重新决策后任务仍成功（本地无记录会真实执行一次，
     外部系统幂等键的必要性在注释/讲解中说明）
  4. 协作式取消：安全点读到标志立即 CANCELLED，工具零调用
  5. 租约丢失：续租失败返回 lost_lease，worker 自行退出不写终态
  6. rebuild_messages 的 tool_call/tool_result 配对契约
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt
from recoverable_loop import (
    run_recoverable, rebuild_messages, SimulatedCrash, BEFORE_TOOL, AFTER_TOOL,
)

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1; print("✓", msg)
    else:
        FAIL += 1; print("✗", msg)


def make_planner():
    """脚本化决策：按已完成的工具结果数推进 send_sms → query_stock → 终答。"""
    def planner(messages):
        done = sum(1 for m in messages if m["role"] == "tool")
        if done == 0:
            return {"type": "tool", "name": "send_sms", "args": {"phone": "138****0001"}}
        if done == 1:
            return {"type": "tool", "name": "query_stock", "args": {"sku": "A1"}}
        return {"type": "final", "content": "短信已发，库存 12 件，任务完成"}
    return planner


def make_toons(counters):
    return {
        "send_sms": lambda args: counters.__setitem__("sms", counters["sms"] + 1)
            or {"sms_id": "SMS-1001", "status": "sent"},
        "query_stock": lambda args: counters.__setitem__("stock", counters["stock"] + 1)
            or {"sku": "A1", "qty": 12},
    }


def expire_lease(conn, task_id):
    """模拟 worker 心跳停止：把租约改到过去，让别的 worker 能接管。"""
    with conn:
        conn.execute("UPDATE tasks SET lease_expire_at=? WHERE task_id=?",
                     (time.time() - 1, task_id))


# ── 1. 正常跑完 ────────────────────────────────────────────
work = tempfile.mkdtemp(prefix="recloop_")
conn = rt.connect(os.path.join(work, "tasks.db"))
counters = {"sms": 0, "stock": 0}
tid = rt.submit_task(conn, "发短信并查库存")
_, ver = rt.claim_task(conn, "worker-1")
outcome = run_recoverable(conn, tid, "worker-1", ver, make_planner(), make_toons(counters))
row = rt.get_task(conn, tid)
expect(outcome == "succeeded" and row["status"] == rt.SUCCEEDED, "正常流程跑到 SUCCEEDED")
expect(counters == {"sms": 1, "stock": 1}, "两个工具各真实执行一次")
events = rt.replay_events(conn, tid)
kinds = [e["event_type"] for e in events]
expect(kinds == ["user", "tool_call", "tool_result", "tool_call", "tool_result", "assistant"],
       f"事件流顺序与配对完整：{kinds}")

# ── 2. AFTER_TOOL 崩溃：最干净的恢复路径 ───────────────────
conn2 = rt.connect(os.path.join(work, "crash_after.db"))
c2 = {"sms": 0, "stock": 0}
t2 = rt.submit_task(conn2, "崩溃测试-AFTER")
_, v2 = rt.claim_task(conn2, "worker-1")
try:
    run_recoverable(conn2, t2, "worker-1", v2, make_planner(), make_toons(c2),
                    crash_points={"call-0:send_sms": AFTER_TOOL})
    expect(False, "应抛出 SimulatedCrash")
except SimulatedCrash:
    expect(True, "AFTER_TOOL 夹缝注入崩溃（进程死亡，内存全丢）")
expect(c2["sms"] == 1, "崩溃前短信真实发出 1 次")
# 进程重启视角：新连接、新 worker，租约过期后接管
conn2b = rt.connect(os.path.join(work, "crash_after.db"))
expire_lease(conn2b, t2)
t2b, v2b = rt.claim_task(conn2b, "worker-2")
expect(t2b == t2 and v2b == v2 + 1, "重启后 worker-2 接管，fencing 代次 +1")
outcome2 = run_recoverable(conn2b, t2, "worker-2", v2b, make_planner(), make_toons(c2))
expect(outcome2 == "succeeded", "恢复后任务跑到 SUCCEEDED")
expect(c2["sms"] == 1, "★ 短信全程只发 1 次：重放命中幂等缓存，工具零重复")
replayed_events = [e for e in rt.replay_events(conn2b, t2)
                   if e["event_type"] == "tool_result"]
expect(replayed_events[0]["payload"]["replayed"] is True,
       "第一条 tool_result 标记 replayed=True（走缓存未真实执行）")
expect(replayed_events[1]["payload"]["replayed"] is False,
       "第二步 query_stock 首次真实执行")

# ── 3. BEFORE_TOOL 崩溃：未配对事件截掉后重新决策 ──────────
conn3 = rt.connect(os.path.join(work, "crash_before.db"))
c3 = {"sms": 0, "stock": 0}
t3 = rt.submit_task(conn3, "崩溃测试-BEFORE")
_, v3 = rt.claim_task(conn3, "worker-1")
try:
    run_recoverable(conn3, t3, "worker-1", v3, make_planner(), make_toons(c3),
                    crash_points={"call-0:send_sms": BEFORE_TOOL})
except SimulatedCrash:
    pass
# 崩溃现场：tool_call 事件在，但没有配对的 tool_result
mid = rt.replay_events(conn3, t3)
expect(mid[-1]["event_type"] == "tool_call" and len(mid) == 2,
       "崩溃现场留下未配对的 tool_call 事件（user + tool_call）")
msgs = rebuild_messages(mid)
expect(all("tool_calls" not in m for m in msgs) and len(msgs) == 1,
       "rebuild 截掉未配对 tool_call，只剩 user 消息，历史保持合法配对")
# 重启接管
conn3b = rt.connect(os.path.join(work, "crash_before.db"))
expire_lease(conn3b, t3)
_, v3b = rt.claim_task(conn3b, "worker-2")
outcome3 = run_recoverable(conn3b, t3, "worker-2", v3b, make_planner(), make_toons(c3))
expect(outcome3 == "succeeded" and c3 == {"sms": 1, "stock": 1},
       "重放后 planner 重新决策，任务成功，工具各执行 1 次（本地无坑可命中）")

# ── 4. 协作式取消 ──────────────────────────────────────────
conn4 = rt.connect(os.path.join(work, "cancel.db"))
c4 = {"sms": 0, "stock": 0}
t4 = rt.submit_task(conn4, "取消测试")
_, v4 = rt.claim_task(conn4, "worker-1")
rt.request_cancel(conn4, t4)  # 用户在 worker 开跑前点了取消
outcome4 = run_recoverable(conn4, t4, "worker-1", v4, make_planner(), make_toons(c4))
expect(outcome4 == "cancelled" and rt.get_task(conn4, t4)["status"] == rt.CANCELLED,
       "安全点读到取消标志，干净收口为 CANCELLED")
expect(c4 == {"sms": 0, "stock": 0}, "取消后零工具调用，无副作用")

# ── 5. 租约丢失：fencing 失配 → 续租失败 → lost_lease ─────
conn5 = rt.connect(os.path.join(work, "lost.db"))
c5 = {"sms": 0, "stock": 0}
t5 = rt.submit_task(conn5, "租约丢失测试")
_, v5 = rt.claim_task(conn5, "worker-1")
# 模拟 worker-1 卡顿期间租约过期、被 worker-2 接管（代次升到 v5+1）
expire_lease(conn5, t5)
_, v5_new = rt.claim_task(conn5, "worker-2")
expect(v5_new == v5 + 1, "worker-2 接管时代次递增")
outcome5 = run_recoverable(conn5, t5, "worker-1", v5, make_planner(), make_toons(c5))
expect(outcome5 == "lost_lease", "旧 worker-1 续租被拒，返回 lost_lease 自行退出")
expect(rt.get_task(conn5, t5)["lease_owner"] == "worker-2",
       "旧 worker 没有篡改任务归属，owner 仍是 worker-2")
expect(c5 == {"sms": 0, "stock": 0}, "旧 worker 一个工具都没执行，双跑被 fencing 挡住")

print(f"\n{'='*50}\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
