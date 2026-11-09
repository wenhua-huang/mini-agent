"""test_plan_mode_e2e.py — Plan Mode 接入主循环后的端到端离线证明。"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mini_agent as ma
from plan_mode import PlanModeState


class FakeFunction:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args


class FakeToolCall:
    def __init__(self, cid, name, args="{}"):
        self.id = cid
        self.function = FakeFunction(name, args)


class FakeMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in (self.tool_calls or [])
            ],
        }


class FakeUsage:
    def __init__(self): self.total_tokens = 20


class FakeChoice:
    def __init__(self, msg): self.message = msg


class FakeResponse:
    def __init__(self, msg):
        self.choices = [FakeChoice(msg)]
        self.usage = FakeUsage()


work = tempfile.mkdtemp(prefix="plan_e2e_")
app = os.path.join(work, "app.py")
with open(app, "w", encoding="utf-8") as f:
    f.write("VERSION = 1\n")

SCRIPT = [
    FakeResponse(FakeMessage(tool_calls=[FakeToolCall("c1", "enter_plan_mode")])),
    # 计划阶段试图先斩后奏改业务文件：必须被权限层挡住
    FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
        "c2", "edit_file", '{"path":"app.py","old_string":"VERSION = 1","new_string":"VERSION = 999"}')])),
    # 被挡后老实写计划
    FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
        "c3", "write_plan_file", '{"plan":"# 计划\\n把 VERSION 改成 2 并验证。"}')])),
    # 请求用户批准
    FakeResponse(FakeMessage(tool_calls=[FakeToolCall("c4", "exit_plan_mode")])),
    # 批准后执行真正修改
    FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
        "c5", "edit_file", '{"path":"app.py","old_string":"VERSION = 1","new_string":"VERSION = 2"}')])),
    FakeResponse(FakeMessage(content="已按批准的计划完成修改。")),
]


class FakeCompletions:
    def __init__(self): self.calls = 0
    def create(self, **kwargs):
        r = SCRIPT[self.calls]
        self.calls += 1
        return r


ma.llm = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions()))
state = PlanModeState(base_dir=work)

allowed = []
def approver(tool, args):
    allowed.append(tool)
    return tool in ("exit_plan_mode", "edit_file")

answer = ma.run_agent("先规划再修改版本号", cwd=work, approver=approver,
                      plan_state=state, summarizer=lambda m, *a, **k: "x")

assert ma.llm.chat.completions.calls == 6, "应完整走 6 轮假模型"
with open(app, encoding="utf-8") as f:
    content = f.read()
assert content == "VERSION = 2\n", f"最终文件错误：{content!r}"
assert "VERSION = 999" not in content, "计划阶段的偷改绝不能落盘"
assert state.mode == "execute", "批准退出后应回到 execute"
assert os.path.exists(state.plan_path), "计划工件应落盘"
assert "edit_file" in allowed and "exit_plan_mode" in allowed
print("✓ plan 阶段 edit_file 被挡，业务文件没有被先斩后奏改成 999")
print("✓ 计划写入独立 plan 文件")
print("✓ exit_plan_mode 经用户批准后才切回 execute")
print("✓ 批准后的 edit_file 才真正落盘为 VERSION = 2")
print("\n[测试通过] Plan Mode 主循环端到端：进入→硬挡→写计划→批准→执行 全链路正确")
