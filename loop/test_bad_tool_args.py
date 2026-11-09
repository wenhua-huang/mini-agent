"""test_bad_tool_args.py — 真实模型联调暴露的健壮性问题：工具参数是非法 JSON 时，
循环必须容错（回灌错误让模型自己修），而不是 json.loads 抛异常炸掉整个 Agent。"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mini_agent as ma


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


class R:
    def __init__(self, m): self.choices, self.usage = [types.SimpleNamespace(message=m)], U()


work = tempfile.mkdtemp(prefix="bad_args_")

BAD = '{"pattern": "\\d+", "path": "todo.py"}'   # \d 是非法 JSON 转义，真实 qwen 就这么吐过
SCRIPT = [
    R(M(tool_calls=[TC("c1", "search_in_file", BAD)])),       # 非法参数
    R(M(content="我修正参数后已完成。")),                      # 模型看到报错后改正/放弃，正常收尾
]


class Comp:
    def __init__(self): self.calls = 0
    def create(self, **kw):
        r = SCRIPT[self.calls]; self.calls += 1
        if self.calls == 2:
            last = kw["messages"][-1]
            assert last["role"] == "tool", "坏参数应作为 tool 结果回灌"
            assert "JSON" in last["content"] or "json" in last["content"], \
                "回灌内容应提示是参数 JSON 解析错误"
        return r


ma.llm = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Comp()))

st = ma.PlanModeState(base_dir=work)
ans = ma.run_agent("测试坏参数容错", cwd=work,
                   approver=lambda *a, **k: True, plan_state=st,
                   summarizer=lambda m, *a, **k: "x")

assert ma.llm.chat.completions.calls == 2, "坏参数不应中断循环，应继续到第 2 轮"
assert "完成" in ans
print("✓ 模型吐出非法 JSON 转义参数时主循环不崩")
print("✓ 解析错误作为 tool_result 回灌，模型能继续并正常收尾")
print("\n[测试通过] 工具参数非法 JSON 容错正确")
