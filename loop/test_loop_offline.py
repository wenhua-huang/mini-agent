"""离线验证 mini_agent 的循环骨架：用脚本化的假模型代替百炼 API。
不测试模型智商，只验证：工具分发 → 结果 role=tool 回灌 → 纯文本结束 → 账本结构。"""
import types

import mini_agent as ma


class FakeFunction:
    def __init__(self, name, args):
        self.name = name
        self.arguments = args


class FakeToolCall:
    def __init__(self, cid, name, args):
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
                {
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                } for tc in (self.tool_calls or [])
            ],
        }


class FakeUsage:
    def __init__(self, n): self.total_tokens = n


class FakeChoice:
    def __init__(self, msg): self.message = msg


class FakeResponse:
    def __init__(self, msg, tokens):
        self.choices = [FakeChoice(msg)]
        self.usage = FakeUsage(tokens)


SCRIPTED = [
    # 第 1 轮：模型请求读 INDEX.md
    FakeResponse(FakeMessage(tool_calls=[
        FakeToolCall("call_1", "read_file", '{"path": "../../notes/INDEX.md"}')
    ]), tokens=120),
    # 第 2 轮：模型拿到工具结果，给最终答案
    FakeResponse(FakeMessage(content="笔记目录包含 INDEX、LOG 和六个分类文件。"), tokens=80),
]


class FakeCompletions:
    def __init__(self): self.calls = 0
    def create(self, **kwargs):
        r = SCRIPTED[self.calls]
        self.calls += 1
        # 回灌校验：第 2 次调用时，账本里必须已经有 role=tool 的结果
        if self.calls == 2:
            last = kwargs["messages"][-1]
            assert last["role"] == "tool", f"回灌角色错误: {last['role']}"
            assert last["tool_call_id"] == "call_1"
            assert "学习知识库索引" in last["content"], "工具结果内容不对"
            print("[测试] 第 2 轮请求前账本最后一条 = role=tool 且对得上 call_1 ✓")
        return r


class FakeChat:
    def __init__(self): self.completions = FakeCompletions()


ma.llm = types.SimpleNamespace(chat=FakeChat())

answer = ma.run_agent("看看 INDEX.md 讲了什么")
assert "笔记目录" in answer
assert ma.llm.chat.completions.calls == 2
print("\n[测试通过] 循环骨架正确：2 轮出手，工具分发+回灌+纯文本退出 全部符合预期")
