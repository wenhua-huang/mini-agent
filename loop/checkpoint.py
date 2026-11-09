"""
checkpoint.py — 把 runtime.py 的运行时原语适配进 run_agent 的 OpenAI 消息账本。

定位：runtime.py 不懂 OpenAI 消息格式，mini_agent.py 不该被 SQL 污染；
本模块是两者之间的薄翻译层。run_agent 只在五个位置调用本类：
  启动建任务/恢复 → 循环顶安全点 → assistant 工具请求落盘 →
  工具执行走幂等闸门 → tool 结果落盘/终态收口。

事件 payload 直接存 OpenAI 协议消息字典，回放零转换；
崩溃夹缝里未配对的 assistant(tool_calls) 整条截掉，
已完成的工具靠幂等表在重放时跳过真实副作用。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt

# 状态常量 re-export，让 mini_agent 不直接依赖 runtime 模块
CANCELLED, SUCCEEDED, FAILED = rt.CANCELLED, rt.SUCCEEDED, rt.FAILED


class CancellationRequested(Exception):
    """安全点检测到取消标志/fencing 失配，run_agent 据此走取消收口。"""


class CheckpointStore:
    def __init__(self, db_path, user_input, resume_task_id=None,
                 worker_id="mini-agent", lease_seconds=30.0):
        self.conn = rt.connect(db_path)
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        if resume_task_id:
            # 恢复模式：校验任务存在且租约可接管（过期或无主），claim 拿新 fencing
            row = rt.get_task(self.conn, resume_task_id)
            if row is None:
                raise ValueError(f"任务不存在：{resume_task_id}")
            if row["status"] in rt.TERMINAL_STATES:
                raise ValueError(f"任务已终态 {row['status']}，不能恢复")
            # 模拟重启：旧租约需已过期。测试/演示用 expire 加速；这里强制兜底过期一次，
            # 真实部署由 reaper 扫描或 wait 租约超时完成。
            with self.conn:
                self.conn.execute(
                    "UPDATE tasks SET lease_expire_at=? WHERE task_id=?",
                    (0.0, resume_task_id))
            self.task_id, self.fencing = rt.claim_task(
                self.conn, worker_id, lease_seconds=lease_seconds)
            if self.task_id != resume_task_id:
                raise RuntimeError("恢复领取到意外任务，拒绝继续")
            self.user_input = row["user_input"]
        else:
            # 新建模式：submit 已写 seq=0 的 user 事件（runtime 原生 content 信封）
            self.task_id = rt.submit_task(self.conn, user_input)
            self.task_id, self.fencing = rt.claim_task(
                self.conn, worker_id, lease_seconds=lease_seconds)
            self.user_input = user_input

    # ── 回放：事件流 → OpenAI messages（system 由调用方放在最前）──
    def restore_messages(self):
        events = rt.replay_events(self.conn, self.task_id)
        paired = {
            e["payload"]["message"]["tool_call_id"]
            for e in events
            if e["event_type"] == "tool"
        }
        messages = []
        for e in events:
            if e["event_type"] == "system_note":
                continue  # 僵尸审计等系统事件不进模型上下文
            if e["event_type"] == "user":
                # 兼容两种信封：runtime 原生 {"content": ...} 与适配层 {"message": {...}}
                p = e["payload"]
                messages.append(p["message"] if "message" in p
                                else {"role": "user", "content": p["content"]})
            elif e["event_type"] == "assistant":
                msg = e["payload"]["message"]
                tcs = msg.get("tool_calls") or []
                # 任一 tool_call 没配对 = 崩溃孤儿，整条截掉（幂等表保证重放安全）
                if tcs and any(tc["id"] not in paired for tc in tcs):
                    continue
                messages.append(msg)
            elif e["event_type"] == "tool":
                messages.append(e["payload"]["message"])
            # system_note（僵尸审计等）不进模型上下文
        return messages

    # ── 安全点：循环顶部调用，取消/失配抛异常由 run_agent 收口 ──
    def safe_point(self):
        row = rt.get_task(self.conn, self.task_id)
        if (row is None or row["lease_owner"] != self.worker_id
                or row["version"] != self.fencing):
            raise CancellationRequested("fencing 失配：执行权已被接管")
        if row["cancel_requested"]:
            raise CancellationRequested("用户请求取消")
        if not rt.renew_lease(self.conn, self.task_id, self.worker_id,
                              self.fencing, self.lease_seconds):
            raise CancellationRequested("续租失败：租约已被接管")

    def _guard(self, fn):
        """把存储层 fencing RuntimeError 统一翻译成取消语义。"""
        try:
            return fn()
        except RuntimeError as e:
            raise CancellationRequested(str(e))

    # ── 事件落盘（全部带 fencing 校验）──
    def log_assistant(self, openai_msg):
        self._guard(lambda: rt.append_event_guarded(
            self.conn, self.task_id, "assistant",
            {"message": openai_msg}, self.worker_id, self.fencing))

    def log_tool_result(self, tool_call_id, openai_tool_message, idem_key, replayed):
        payload = {"message": openai_tool_message, "idem_key": idem_key,
                   "replayed": replayed}
        self._guard(lambda: rt.append_event_guarded(
            self.conn, self.task_id, "tool", payload,
            self.worker_id, self.fencing))

    def run_tool_idempotent(self, idem_key, fn):
        """真实工具执行的幂等闸门；返回 (result, replayed)。"""
        return self._guard(lambda: rt.run_idempotent(
            self.conn, self.task_id, idem_key, fn,
            worker_id=self.worker_id, fencing_version=self.fencing))

    def next_idem_key(self, tool_name):
        """
        确定性键：数事件流里已配对完成的 tool 事件数（不依赖内存 messages，
        因此崩溃重放、孤儿截掉后仍能算出与首次完全相同的键）。
        """
        done = sum(1 for e in rt.replay_events(self.conn, self.task_id)
                   if e["event_type"] == "tool")
        return f"tool-{done}:{tool_name}"

    # ── 终态 ──
    def finish(self, status, error=""):
        self._guard(lambda: rt.finish_task(
            self.conn, self.task_id, self.worker_id,
            self.fencing, status, error))

    def current_status(self):
        return rt.get_task(self.conn, self.task_id)["status"]
