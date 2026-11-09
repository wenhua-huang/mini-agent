"""
test_plan_lock_propagation.py — 第九课：Plan 锁必须传播给子 Agent。

漏洞背景：主会话进 plan 模式后，旧代码派 ops 工人时，子循环里的
check_permission 没收到 plan_state，于是 ops 的 run_shell 在子上下文里
不受 plan 只读约束——主脑上了锁，手却能从子 Agent 伸出去。

对齐 Claude Code spawnMultiAgent.ts：plan mode 优先，派生的 teammate
不继承 bypass 权限，但只读调研仍允许（锁是收窄，不是整类禁止派工）。
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mini_agent as ma
from plan_mode import PlanModeState
from permission import check_permission

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond: PASS += 1; print("✓", msg)
    else: FAIL += 1; print("✗", msg)


# ── 假工人模型：第一轮调 run_shell，第二轮交结论 ──
class FF:
    def __init__(self, name, args): self.name, self.arguments = name, args
class TC:
    def __init__(self, cid, name, args): self.id, self.function = cid, FF(name, args)
class M:
    def __init__(self, content="", tool_calls=None):
        self.content, self.tool_calls = content, tool_calls
    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content,
                "tool_calls": [{"id": t.id, "type": "function",
                                "function": {"name": t.function.name,
                                             "arguments": t.function.arguments}}
                               for t in (self.tool_calls or [])]}
class U:
    def __init__(self): self.total_tokens = 20
class Resp:
    def __init__(self, m): self.choices, self.usage = [types.SimpleNamespace(message=m)], U()


def make_worker_chat(shell_command):
    """工人第 1 轮调指定 shell 命令，第 2 轮交结论。"""
    script = [
        Resp(M(tool_calls=[TC("w1", "run_shell", f'{{"command": "{shell_command}"}}')])),
        Resp(M(content="工人调研结束。")),
    ]
    state = {"i": 0, "saw_tool_result": None}
    def chat(messages, tool_schemas=None):
        # 第 2 轮时账本最后一条应是 shell 的 tool 结果
        if state["i"] == 1:
            state["saw_tool_result"] = messages[-1]["content"]
        r = script[state["i"]]; state["i"] += 1
        return r
    chat.state = state
    return chat


work = tempfile.mkdtemp(prefix="plan_lock_")
marker = os.path.join(work, "hack.txt")

def allow_all(tool, args): return True

# ── 1. 派工闸本身：plan 模式下派 ops 仍允许（锁在子循环里收窄，而非整类禁）──
st = PlanModeState(base_dir=work); st.enter(agent_context=False)
d = check_permission("dispatch_worker",
                     {"task": "调研构建方式", "worker_type": "ops"},
                     approver=allow_all, cwd=work, plan_state=st)
expect(d[0] == "allow", "plan 模式仍允许派 ops 工人（靠传播收窄，不是禁止派工）")

# ── 2. 核心：plan 模式下 ops 工人的【非只读】shell 被硬挡，approver 放行也没用 ──
chat = make_worker_chat("touch hack.txt")
res = ma.dispatch_worker("调研项目，必要时跑命令", worker_type="ops",
                         cwd=work, approver=allow_all, chat_fn=chat, plan_state=st)
saw = chat.state["saw_tool_result"] or ""
expect("plan 模式" in saw and "只读" in saw,
       "plan 模式 ops 工人的写操作 shell 被 plan 锁拒绝")
expect(not os.path.exists(marker), "被拒命令没有真的落盘（hack.txt 不存在）")

# ── 3. plan 模式下 ops 工人的【只读】shell 仍允许（保留只读调研能力）──
chat2 = make_worker_chat("ls")
res2 = ma.dispatch_worker("列目录", worker_type="ops",
                          cwd=work, approver=allow_all, chat_fn=chat2, plan_state=st)
saw2 = chat2.state["saw_tool_result"] or ""
expect("plan 模式" not in saw2, "plan 模式只读 shell(ls) 不被 plan 锁拦")

# ── 4. execute 模式：plan 锁不插手，非只读 shell 回到正常审批（allow 则执行）──
st_exec = PlanModeState(base_dir=work)  # 默认 execute
chat3 = make_worker_chat("touch hack.txt")
res3 = ma.dispatch_worker("执行构建", worker_type="ops",
                          cwd=work, approver=allow_all, chat_fn=chat3, plan_state=st_exec)
expect(os.path.exists(marker), "execute 模式非只读 shell 经审批后正常执行（锁已释放）")
os.remove(marker)

# ── 5. 并行派工也要传播 plan 锁：混合 ops 工人的写操作同样被挡 ──
# 两个工人在不同线程，各自需要独立脚本；用 threading.local 隔离，汇总各自看到的结果。
import threading
seen_parallel = []
tls = threading.local()
def parallel_chat(messages, tool_schemas=None):
    if not hasattr(tls, "i"):
        tls.i = 0
        tls.script = [
            Resp(M(tool_calls=[TC("w1", "run_shell", '{"command": "rm -rf tmp_dir"}')])),
            Resp(M(content="工人调研结束。")),
        ]
    if tls.i == 1:
        seen_parallel.append(messages[-1]["content"])
    r = tls.script[tls.i]; tls.i += 1
    return r

out = ma.dispatch_workers(
    ["只读调研目录结构", {"task": "跑命令清理", "worker_type": "ops"}],
    cwd=work, approver=allow_all, chat_fn=parallel_chat, plan_state=st)
# explore 工人没有 run_shell（结果是“工人没有工具”），只有 ops 会触发 plan 锁拒绝
expect(any("plan 模式" in s and "只读" in s for s in seen_parallel),
       "dispatch_workers 并行派工同样把 plan 锁传播给 ops 工人")

# ── 6. 漏传 plan_state 的回归保护：execute 行为不变（不传＝不锁）──
chat5 = make_worker_chat("touch hack2.txt")
res5 = ma.dispatch_worker("旧调用方不传 plan_state", worker_type="ops",
                          cwd=work, approver=allow_all, chat_fn=chat5)  # 不传
expect(os.path.exists(os.path.join(work, "hack2.txt")),
       "不传 plan_state 时默认不锁（向后兼容旧调用方）")

print(f"\n[{'测试通过' if FAIL == 0 else '存在失败'}] Plan 锁传播：{PASS} 条断言通过，{FAIL} 条失败")
sys.exit(1 if FAIL else 0)
