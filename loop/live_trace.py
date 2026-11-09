"""
live_trace.py — 第十课真实联调：真 qwen-plus 跑一次，落结构化 trace JSONL。

护栏：独立临时目录 + 只读自动审批（read/search 放行，写/shell 拒绝）。
诚实边界：这里不传 price_table，真实单价需从百炼官网查后填入，
因此事件里 cost=0（成本折算机制由 test_tracer.py 的假价格表离线证明，不在此伪造金额）。
"""
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mini_agent as ma
from tracer import EventRecorder

ma.MAX_TURNS = 8
ma.MAX_TOTAL_TOKENS = 40000

work = tempfile.mkdtemp(prefix="live_trace_")
with open(os.path.join(work, "calc.py"), "w", encoding="utf-8") as f:
    f.write(
        '"""极简计算器，只有加和减。"""\n'
        "def add(a, b):\n    return a + b\n\n"
        "def sub(a, b):\n    return a - b\n"
    )
with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as f:
    f.write("# calc\n演示用四则运算小工具，目前只实现了 add 和 sub。\n")

trace_path = os.path.join(work, "trace.jsonl")
print("临时项目目录：", work)
print("trace 输出：", trace_path)


def read_only_approver(tool, args):
    # 只放只读工具；任何写/shell 都拒绝（联调不需要副作用）
    return tool in ("read_file", "search_in_file")


task = ("读一下这个项目的 calc.py 和 README.md，告诉我现在实现了哪些函数、"
        "分别在第几行，以及 README 是怎么描述它的。用工具真的去读，不要凭空回答。")

print("任务：", task, "\n" + "=" * 60)
try:
    answer = ma.run_agent(task, cwd=work, approver=read_only_approver,
                          trace_path=trace_path)
except Exception:
    print("\n[联调过程中抛异常]")
    traceback.print_exc()
    answer = "(异常)"

print("=" * 60)
print("\n【最终回答】\n", answer)

print("\n【trace.jsonl 原始事件】")
rows = []
with open(trace_path, encoding="utf-8") as f:
    for line in f:
        if line.strip():
            e = json.loads(line)
            rows.append(e)
            print(json.dumps(e, ensure_ascii=False))

print("\n【台账汇总】")
summary = EventRecorder.summarize_file(trace_path)
print(json.dumps(summary, ensure_ascii=False, indent=2))

# ── 客观校验：真实 trace 必须满足的不变量 ──
checks = []
trace_ids = {e["trace_id"] for e in rows}
checks.append((len(trace_ids) == 1, "所有事件共享同一 trace_id"))
checks.append((all("span_id" in e and "ts" in e for e in rows), "每条事件带 span_id 和 ts"))
llm = [e for e in rows if e["type"] == "llm_call"]
checks.append((len(llm) >= 1, "至少一次真实 llm_call"))
checks.append((all(e.get("tokens_in", 0) > 0 or e.get("tokens_out", 0) > 0 for e in llm),
               "真实 usage 拆出了 in/out token（非全 0）"))
tools = [e for e in rows if e["type"] == "tool_call"]
checks.append((len(tools) >= 1, "至少一次工具 span"))
checks.append((all(e.get("duration_ms") is not None for e in tools), "工具 span 都有 duration_ms"))
ok = all(c[0] for c in checks)
print("\n【不变量校验】")
for cond, msg in checks:
    print(("✓ " if cond else "✗ ") + msg)
print("\n联调结论：", "全部通过" if ok else "存在不通过项")
sys.exit(0 if ok else 1)
