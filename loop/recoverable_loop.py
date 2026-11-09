"""
recoverable_loop.py — 可恢复 Agent 循环（第 2 步：把 runtime 原语织进 agent loop）。

不依赖真实 LLM：planner 可注入，离线测试用脚本化假决策，真模型在后续步骤替换。
循环每一轮的固定结构（安全点在前）：

  1. 安全点：should_cancel（取消标志 / fencing 失配）→ 协作式取消收口
  2. 心跳：renew_lease，续不上说明已被接管，直接停手
  3. 决策：planner(messages) → 工具调用 or 最终回答
  4a. 工具：先落 tool_call 事件 → 幂等闸门执行 → 再落 tool_result 事件
  4b. 终答：落 assistant 事件 → finish SUCCEEDED

两个崩溃夹缝（crash_point 注入，第 3 步会换成真 os._exit）：
  BEFORE_TOOL   tool_call 事件已落盘、工具还没执行：本地无幂等记录，重放会再执行一次，
                这就是幂等键必须同时传给外部系统的原因（本地闸门挡不住未知副作用）。
  AFTER_TOOL    工具已执行且 DONE 已占坑、tool_result 事件没来得及落：重放命中缓存，
                工具零重复执行——最干净的恢复路径。

消息配对契约（OpenAI 兼容接口同理）：assistant 的 tool_call 必须有同 call_id 的
tool_result 配对。重建内存 messages 时，末尾未配对的 tool_call 事件直接截掉，
让 planner 重新决策（替代方案：补一条 interrupted 的 tool 消息，见 rebuild 注释）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt

# 崩溃注入点
BEFORE_TOOL, AFTER_TOOL = "BEFORE_TOOL", "AFTER_TOOL"


class SimulatedCrash(Exception):
    """测试用：模拟进程在指定夹缝死亡（内存状态全丢，磁盘事件保留）。"""


def rebuild_messages(events):
    """
    回放事件流重建内存 messages。
    末尾若有未配对的 tool_call（崩溃夹缝），截掉不放进 messages，
    使历史保持「tool_call 必有 tool_result」的合法配对。
    替代方案是给它补一条 content=interrupted 的 tool 消息，保留决策痕迹，
    两种都合法；这里选截掉，因为幂等闸门保证重放安全、planner 可重新决策。
    """
    paired_ids = set()
    for e in events:
        if e["event_type"] == "tool_result":
            paired_ids.add(e["payload"]["call_id"])

    messages = []
    for e in events:
        p = e["payload"]
        if e["event_type"] == "user":
            messages.append({"role": "user", "content": p["content"]})
        elif e["event_type"] == "assistant":
            messages.append({"role": "assistant", "content": p.get("content", "")})
        elif e["event_type"] == "tool_call":
            if p["call_id"] in paired_ids:  # 未配对的直接截掉
                messages.append({"role": "assistant", "tool_calls": [p]})
        elif e["event_type"] == "tool_result":
            messages.append({"role": "tool", "tool_call_id": p["call_id"],
                             "content": str(p["output"])})
    return messages


def _next_idem_key(events, tool_name):
    """
    确定性幂等键：只数「已配对完成」的工具调用序号，崩溃夹缝里的未配对事件不计入。
    因此重放后同一业务步骤算出的 key 与首次完全一致（派生，非随机 UUID）。
    """
    done = sum(1 for e in events if e["event_type"] == "tool_result")
    return f"call-{done}:{tool_name}"


def run_recoverable(conn, task_id, worker_id, fencing_version,
                    planner, tools, lease_seconds=30.0, crash_points=None):
    """
    planner(messages) -> {"type": "tool", "name", "args"} | {"type": "final", "content"}
    tools: {name: callable(args_dict) -> JSON 可序列化结果}
    crash_points: {idem_key: BEFORE_TOOL/AFTER_TOOL}，测试注入用
    返回 "succeeded" / "cancelled" / "lost_lease"。
    """
    crash_points = crash_points or {}

    while True:
        # ── 安全点 1a：验明正身。fencing 失配=租约已被新 worker 接管，
        #    旧 worker 必须静默退出，无权写任何状态（更不能替新 owner 写终态）──
        row = rt.get_task(conn, task_id)
        if (row is None or row["lease_owner"] != worker_id
                or row["version"] != fencing_version):
            return "lost_lease"

        # ── 安全点 1b：用户主动取消，当前 owner 才有资格收口为 CANCELLED ──
        if row["cancel_requested"]:
            rt.finish_task(conn, task_id, worker_id, fencing_version, rt.CANCELLED)
            return "cancelled"

        # ── 安全点 2：心跳续租，续不上=租约在两步之间被接管 ──
        if not rt.renew_lease(conn, task_id, worker_id, fencing_version, lease_seconds):
            return "lost_lease"

        events = rt.replay_events(conn, task_id)
        messages = rebuild_messages(events)
        decision = planner(messages)

        if decision["type"] == "final":
            rt.append_event(conn, task_id, "assistant",
                            {"kind": "final", "content": decision["content"]})
            rt.finish_task(conn, task_id, worker_id, fencing_version, rt.SUCCEEDED)
            return "succeeded"

        # ── 工具调用分支 ──
        tool_name, args = decision["name"], decision["args"]
        call_id = f"call_{len(messages)}_{tool_name}"
        idem_key = _next_idem_key(events, tool_name)

        # 4a-1 先落 tool_call 事件（崩溃后世界知道这步「打算做什么」）
        rt.append_event(conn, task_id, "tool_call",
                        {"kind": "tool_call", "call_id": call_id,
                         "name": tool_name, "args": args})

        if crash_points.get(idem_key) == BEFORE_TOOL:
            crash_points.pop(idem_key)  # 只崩一次：重放时该夹缝不再触发
            raise SimulatedCrash(f"崩溃于 {idem_key} 的 BEFORE_TOOL 夹缝")

        # 4a-2 幂等闸门：重放命中 DONE 缓存则工具函数根本不会被调用
        def invoke():
            return tools[tool_name](args)

        result, replayed = rt.run_idempotent(conn, task_id, idem_key, invoke)

        if crash_points.get(idem_key) == AFTER_TOOL:
            crash_points.pop(idem_key)  # 只崩一次
            raise SimulatedCrash(f"崩溃于 {idem_key} 的 AFTER_TOOL 夹缝")

        # 4a-3 再落 tool_result，配对完成
        rt.append_event(conn, task_id, "tool_result",
                        {"kind": "tool_result", "call_id": call_id,
                         "idem_key": idem_key, "replayed": replayed, "output": result})
