"""
test_runtime.py — runtime.py 第 1 步离线测试（不依赖 LLM、不联网）。

覆盖：
  1. submit：先落库再受理，PENDING + seq=0 user 事件同事务
  2. claim：原子领取转 RUNNING，带租约和 fencing version
  3. 租约内不接管、过期才接管，接管时代次号 +1（防双跑）
  4. request_cancel 只置 CANCELLING/标志位；终态不可取消
  5. should_cancel：fencing 失配的旧 worker 被要求停手（fail-closed）
  6. finish_task：旧 owner/旧代次写入被拒
  7. append_event 的 seq 连续 + replay 按序回放
  8. run_idempotent：副作用只执行一次，重放走缓存；FAILED 后允许重试
  9. renew_lease 心跳：owner 匹配才续得上
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1; print("✓", msg)
    else:
        FAIL += 1; print("✗", msg)


work = tempfile.mkdtemp(prefix="runtime_")
db = os.path.join(work, "tasks.db")
conn = rt.connect(db)

# ── 1. submit ──────────────────────────────────────────────
tid = rt.submit_task(conn, "帮我查库存并下单")
row = rt.get_task(conn, tid)
expect(row is not None and row["status"] == rt.PENDING, "submit 后任务落库且为 PENDING")
events = rt.replay_events(conn, tid)
expect(len(events) == 1 and events[0]["event_type"] == "user"
       and events[0]["seq"] == 0, "seq=0 是 user 事件，原话已持久化（不靠函数栈传递）")

# ── 2. claim ───────────────────────────────────────────────
tid2, ver = rt.claim_task(conn, "worker-A", lease_seconds=30)
expect(tid2 == tid and ver == 2, "worker-A 领取成功，fencing version=2（初始 1，接管 +1）")
row = rt.get_task(conn, tid)
expect(row["status"] == rt.RUNNING and row["lease_owner"] == "worker-A"
       and row["lease_expire_at"] > time.time(), "任务转 RUNNING，租约指向 worker-A 且未过期")
empty, no_ver = rt.claim_task(conn, "worker-B")
expect(empty is None, "无其他可领任务时 claim 返回 None")

# ── 3. 租约保护与过期接管 ──────────────────────────────────
tid3, ver3 = rt.claim_task(conn, "worker-B", lease_seconds=30)
expect(tid3 is None, "租约未过期，worker-B 不得接管（防双跑）")
# 人为把租约改到过去，模拟 worker-A 心跳停止
with conn:
    conn.execute("UPDATE tasks SET lease_expire_at=? WHERE task_id=?",
                 (time.time() - 1, tid))
tid3, ver3 = rt.claim_task(conn, "worker-B", lease_seconds=30)
expect(tid3 == tid and ver3 == 3, "租约过期后 worker-B 接管，代次号升到 3")

# ── 4. 取消语义 ────────────────────────────────────────────
ok = rt.request_cancel(conn, tid)
row = rt.get_task(conn, tid)
expect(ok and row["status"] == rt.CANCELLING and row["cancel_requested"] == 1,
       "取消请求只置 CANCELLING + 标志位，不假装已停下")
expect(rt.should_cancel(conn, tid, "worker-B", 3) is True,
       "当前 owner 在安全点能读到取消标志")
expect(rt.should_cancel(conn, tid, "worker-A", 2) is True,
       "旧 worker-A（fencing 失配）被要求停手，防止两代 worker 并发")

# ── 5. 终态回写的 fencing 校验 ─────────────────────────────
expect(rt.finish_task(conn, tid, "worker-A", 2, rt.CANCELLED) is False,
       "旧 owner/旧代次的终态写入被拒")
expect(rt.finish_task(conn, tid, "worker-B", 3, rt.CANCELLED) is True,
       "当前 owner 收口成功，转 CANCELLED 并释放租约")
row = rt.get_task(conn, tid)
expect(row["status"] == rt.CANCELLED and row["lease_owner"] is None,
       "终态任务租约已清空")
expect(rt.request_cancel(conn, tid) is False, "终态任务不可再取消")
gone, _ = rt.claim_task(conn, "worker-C")
expect(gone is None, "终态任务永不被重新领取")

# ── 6. 事件流 seq 与回放 ───────────────────────────────────
t2 = rt.submit_task(conn, "第二个任务")
rt.claim_task(conn, "worker-A")
s1 = rt.append_event(conn, t2, "assistant", {"content": "思考：先查库存"})
s2 = rt.append_event(conn, t2, "tool_call", {"name": "query_stock", "args": {"sku": "A1"}})
s3 = rt.append_event(conn, t2, "tool_result", {"output": "库存 12 件"})
expect((s1, s2, s3) == (1, 2, 3), "事件 seq 在任务内连续递增")
replayed = rt.replay_events(conn, t2)
expect([e["seq"] for e in replayed] == [0, 1, 2, 3]
       and replayed[2]["payload"]["name"] == "query_stock",
       "回放按 seq 有序，工具调用参数完整可重建")

# ── 7. 幂等闸门：副作用只执行一次 ─────────────────────────
calls = {"n": 0}
def send_sms():
    calls["n"] += 1
    return {"sms_id": "SMS-1001", "status": "sent"}

r1, replayed1 = rt.run_idempotent(conn, t2, "step-3:send_sms", send_sms)
r2, replayed2 = rt.run_idempotent(conn, t2, "step-3:send_sms", send_sms)
expect(calls["n"] == 1, "同一幂等键下真实副作用只执行一次（崩溃重放不重复发短信）")
expect(replayed1 is False and replayed2 is True and r1 == r2 == {"sms_id": "SMS-1001", "status": "sent"},
       "首次返回真实结果，重放直接返回缓存结果且标记 replayed=True")

# 失败的调用不占成功坑，允许重试
def flaky():
    calls.setdefault("fail_n", 0)
    calls["fail_n"] += 1
    raise RuntimeError("网关超时")

try:
    rt.run_idempotent(conn, t2, "step-4:risk_check", flaky)
except RuntimeError:
    pass
retry_r, retry_replayed = rt.run_idempotent(conn, t2, "step-4:risk_check",
                                            lambda: {"risk": "PASS"})
expect(calls["fail_n"] == 1 and retry_r == {"risk": "PASS"} and retry_replayed is False,
       "FAILED 记录不阻塞重试，成功后才占幂等坑")

# ── 8. 心跳续租 ────────────────────────────────────────────
t3 = rt.submit_task(conn, "第三个任务")
_, v3 = rt.claim_task(conn, "worker-A", lease_seconds=30)
expect(rt.renew_lease(conn, t3, "worker-A", v3) is True, "owner 匹配时心跳续租成功")
expect(rt.renew_lease(conn, t3, "worker-X", v3) is False, "非 owner 续租被拒")

# ── 9. 重开连接：持久化验证 ────────────────────────────────
conn.close()
conn2 = rt.connect(db)  # 同库重连，表已存在，走 IF NOT EXISTS
row = rt.get_task(conn2, tid)
expect(row is not None and row["status"] == rt.CANCELLED,
       "进程重开后任务状态仍在（这就是崩溃恢复的存储地基）")
expect(len(rt.replay_events(conn2, t2)) == 4, "进程重开后事件流完整可回放")
conn2.close()

print(f"\n{'='*50}\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
