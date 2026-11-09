"""
test_worker_types.py — 第七课：子 Agent 类型注册表 + when_to_use 自选 + 按类型硬收窄（离线）

要证明的（对齐 Claude Code AgentTool）：
  1. 主模型给 worker_type 选类型；运行时按注册表给该类型的 system/工具表/预算。
  2. when_to_use 是软引导（出现在工具描述里），tool_names 是硬约束（实际工具表）。
  3. explore/planner 看不到 run_shell；ops 看得到——选错只读类型也越不了权。
  4. 未知类型 fail-closed 拒绝派单，且工人模型根本不会被调用。
  5. 并行混合类型：每个工人各自独立的工具表与人设，互不串。
  6. 权限层对未知类型 deny；ops 派单 allow（其 shell 仍逐次过闸）。
全部假模型 + 临时目录，不打真实 API。
"""
import os
import sys
import tempfile
import threading
import types as _types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mini_agent as ma
from permission import check_permission
from worker_types import (
    WORKER_TYPES, get_worker_type, render_worker_catalog, DEFAULT_WORKER_TYPE,
)

PASS = 0
def expect(cond, label):
    global PASS
    assert cond, "失败：" + label
    PASS += 1
    print("✓", label)


# ── 假模型基建 ───────────────────────────────────────────
class FF:
    def __init__(self, name, args): self.name, self.arguments = name, args
class TC:
    def __init__(self, cid, name, args): self.id, self.function = cid, FF(name, args)
class Msg:
    def __init__(self, content="", tool_calls=None): self.content, self.tool_calls = content, tool_calls
    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content,
                "tool_calls": [{"id": t.id, "type": "function",
                                "function": {"name": t.function.name, "arguments": t.function.arguments}}
                               for t in (self.tool_calls or [])]}
class Usage:
    def __init__(self, n): self.total_tokens = n
class Resp:
    def __init__(self, msg, tok=50): self.choices = [_types.SimpleNamespace(message=msg)]; self.usage = Usage(tok)


work = tempfile.mkdtemp(prefix="wtypes_")

# 记录每个工人「实际看到」的工具表名与人设（由 run_subagent 注入的 schemas/messages）
seen_lock = threading.Lock()
seen = []
calls = {"n": 0}

def spy_chat(messages, schemas):
    with seen_lock:
        seen.append({
            "system": messages[0]["content"],
            "tools": tuple(s["function"]["name"] for s in schemas),
        })
        calls["n"] += 1
    return Resp(Msg(content="这是该类型工人的结论。"), tok=40)

def deny_all(_tool, _args):
    return False


# ── 1. 注册表本身：声明式、含三类、缺省 explore ─────────
expect(set(WORKER_TYPES.keys()) == {"explore", "planner", "ops"}, "注册表含 explore/planner/ops 三类")
expect(DEFAULT_WORKER_TYPE == "explore", "缺省工人类型为 explore")
expect(get_worker_type("nope") is None, "未知类型 get_worker_type 返回 None（不兜底）")
catalog = render_worker_catalog()
for t in ("explore", "planner", "ops"):
    expect(t in catalog, f"类型清单里出现 {t}（when_to_use 会拼进工具描述）")

# dispatch_worker 的工具描述确实内嵌了清单（模型据此自选）
dw_desc = next(s["function"]["description"]
               for s in ma.TOOL_SCHEMAS if s["function"]["name"] == "dispatch_worker")
expect("可选工人类型" in dw_desc and "planner" in dw_desc,
       "dispatch_worker 描述内嵌类型清单（软引导 when_to_use 送达模型）")
dw_props = next(s["function"]["parameters"]["properties"]
                for s in ma.TOOL_SCHEMAS if s["function"]["name"] == "dispatch_worker")
expect(dw_props["worker_type"]["enum"] == ["explore", "planner", "ops"],
       "worker_type 参数用 enum 约束合法类型")


# ── 2. explore：实际工具表只有只读两件套，人设是勘察员 ──
seen.clear(); calls["n"] = 0
out = ma.dispatch_worker("定位登录逻辑", worker_type="explore",
                         cwd=work, approver=deny_all, chat_fn=spy_chat)
expect(calls["n"] >= 1, "explore 工人模型确实被调用")
expect(seen[0]["tools"] == ("read_file", "search_in_file"),
       "explore 实际工具表硬收窄为只读两件套，无 run_shell/edit_file")
expect("run_shell" not in seen[0]["tools"], "explore 看不到 run_shell")
expect("勘察" in seen[0]["system"], "explore 拿到的是勘察员专职 system prompt")
expect("[子Agent:explore" in out, "结果标注类型名 explore")


# ── 3. planner：同样只读，人设是规划专家 ────────────────
seen.clear()
out = ma.dispatch_worker("设计缓存方案", worker_type="planner",
                         cwd=work, approver=deny_all, chat_fn=spy_chat)
expect(seen[0]["tools"] == ("read_file", "search_in_file"), "planner 也只有只读工具")
expect("规划" in seen[0]["system"], "planner 拿到规划专家 system prompt")


# ── 4. ops：工具表含 run_shell（唯一能执行命令的类型）──
seen.clear()
out = ma.dispatch_worker("看下 git 状态", worker_type="ops",
                         cwd=work, approver=deny_all, chat_fn=spy_chat)
expect(seen[0]["tools"] == ("read_file", "search_in_file", "run_shell"),
       "ops 实际工具表含 run_shell（命令能力由类型授予）")
expect("沙箱" in seen[0]["system"], "ops 人设说明命令在沙箱内运行")
# 注意：ops 能"看到" run_shell，但真执行时仍走 check_permission + seatbelt，下面第 7 点验证闸门仍在。


# ── 5. 缺省类型（不传 worker_type）走 explore ──────────
seen.clear()
ma.dispatch_worker("随便看看", cwd=work, approver=deny_all, chat_fn=spy_chat)
expect(seen[0]["tools"] == ("read_file", "search_in_file") and "勘察" in seen[0]["system"],
       "不传 worker_type 时缺省为 explore")


# ── 6. 未知类型 fail-closed：拒绝派单，模型不被调用 ────
seen.clear(); calls["n"] = 0
out = ma.dispatch_worker("干坏事", worker_type="hacker",
                         cwd=work, approver=deny_all, chat_fn=spy_chat)
expect("拒绝派单" in out and "未知" in out, "未知 worker_type 返回拒绝")
expect(calls["n"] == 0, "未知类型时工人模型根本没被启动（不放任默认类型）")


# ── 7. ops 虽有 run_shell，真正执行仍逐次过权限闸 ──────
decision = check_permission("run_shell", {"command": "rm -rf ~"}, cwd=work,
                            approver=deny_all)[0]
expect(decision == "deny", "ops 工人内部调 run_shell 仍被同一闸门 deny（类型授权不等于绕过审批）")
# 派 ops 这一动作本身 allow，危险在执行环节拦
d2 = check_permission("dispatch_worker",
                      {"task": "看状态", "worker_type": "ops"},
                      cwd=work, approver=deny_all)[0]
expect(d2 == "allow", "派单 ops 本身 allow（能力边界由执行环节的沙箱+审批保证）")
d3 = check_permission("dispatch_worker",
                      {"task": "x", "worker_type": "ghost"},
                      cwd=work, approver=deny_all)[0]
expect(d3 == "deny", "权限层对未知 worker_type 直接 deny")


# ── 8. 并行混合类型：三个工人各拿各的工具表与人设 ──────
# 每个工人第一轮 len(messages)==2，直接给结论；spy 已线程安全记录。
seen.clear()
summary = ma.dispatch_workers(
    [
        "找文件 A",
        {"task": "规划功能 B", "worker_type": "planner"},
        {"task": "跑测试 C", "worker_type": "ops"},
    ],
    cwd=work, approver=deny_all, chat_fn=spy_chat,
)
# 三个工人都被启动，且看到的工具表集合正好是三种类型各自的
tool_sets = {s["tools"] for s in seen}
expect(("read_file", "search_in_file") in tool_sets, "并行批次里有只读工人（explore/planner）")
expect(("read_file", "search_in_file", "run_shell") in tool_sets, "并行批次里 ops 独立持有 run_shell")
# 两个只读工人人设不同（勘察 vs 规划），证明同工具表也按类型区分人设
sys_snippets = {("勘察" if "勘察" in s["system"] else
                 "规划" if "规划" in s["system"] else
                 "沙箱" if "沙箱" in s["system"] else "?") for s in seen}
expect({"勘察", "规划", "沙箱"} <= sys_snippets, "三个工人拿到三种专职人设，互不串")
expect("explore" in summary and "planner" in summary and "ops" in summary,
       "汇总文本标明每个工人的类型")
# 并行结果里没有把 ops 的 run_shell 泄漏给只读工人
readonly_views = [s for s in seen if "run_shell" not in s["tools"]]
expect(len(readonly_views) == 2, "explore/planner 两个工人都看不到 run_shell（工具表物理隔离）")


print(f"\n[测试通过] worker_type 注册表：{PASS} 条断言全绿（自选/硬收窄/未知拒绝/混合并行）")
