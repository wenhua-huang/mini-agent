"""
test_tracer.py — 第十课（可观测性）：把 print 账本升级成结构化 trace。

覆盖：
  1. 事件落 JSONL：每行可解析、同一 trace_id、span_id 唯一、带时间戳
  2. llm_call 事件按价格表算成本；summary 汇总 token/成本/工具数/拒绝数
  3. 父子 span：子事件 parent_id 指回父 span
  4. span() 上下文管理器自动记 duration_ms，异常时 status=error 且不吞异常
  5. 子 Agent 透传：run_subagent 收到 recorder+parent_span_id 后，
     其事件共享 trace_id、parent_id 正确（复用 plan_state 同一条参数链）
  6. NullRecorder 完全 no-op，不产生文件（默认接入不改变旧行为）
  7. 脱敏：字符串里的 sk- 密钥落盘前被掩码
  8. 多线程并发写不产生坏行（并行子 Agent 场景）
"""
import json
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tracer import EventRecorder, NullRecorder
from subagent import run_subagent

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond: PASS += 1; print("✓", msg)
    else: FAIL += 1; print("✗", msg)


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


work = tempfile.mkdtemp(prefix="tracer_")
trace_path = os.path.join(work, "trace.jsonl")

# 测试用假价格表（每 100 万 token 单价），不写死真实厂商价
PRICE = {"fake-model": {"in": 2.0, "out": 8.0}}

# ── 1~4. 基础记录能力 ──
rec = EventRecorder(path=trace_path, price_table=PRICE)
expect(rec.trace_id, "开 trace 自动生成 trace_id")

root = rec.event("llm_call", turn=1, model="fake-model", tokens_in=1000, tokens_out=500)
child = rec.event("tool_call", parent_id=root, turn=1, tool="run_shell",
                  decision="allow", status="ok", duration_ms=12)
expect(child and child != root, "event 返回 span_id 且父子不相等")

# 成本：1000/1e6*2 + 500/1e6*8 = 0.002 + 0.004 = 0.006
ev_cost = [e for e in read_jsonl(trace_path) if e["type"] == "llm_call"][0]
expect(abs(ev_cost["cost"] - 0.006) < 1e-9, "llm_call 按 in/out 单价算成本（0.006）")

# span 上下文管理器：正常路径
with rec.span("tool_call", tool="read_file", turn=2) as sp:
    sp.set("status", "ok")
# 异常路径：记 error 但异常继续抛
raised = False
try:
    with rec.span("tool_call", tool="run_shell", turn=2):
        raise RuntimeError("boom")
except RuntimeError:
    raised = True
expect(raised, "span 内异常照常抛出，不被吞掉")

events = read_jsonl(trace_path)
expect(all("ts" in e and "span_id" in e for e in events), "每条事件带 ts 和 span_id")
expect(len({e["span_id"] for e in events}) == len(events), "span_id 全局唯一")
err_span = [e for e in events if e.get("status") == "error"]
expect(len(err_span) == 1 and isinstance(err_span[0]["duration_ms"], (int, float))
       and err_span[0]["duration_ms"] >= 0, "异常 span 记 status=error 且有 duration_ms")
nested = [e for e in events if e["span_id"] == child][0]
expect(nested["parent_id"] == root, "子事件 parent_id 指回父 span")

# 再记一条拒绝事件和一轮 llm，验证 summary 归类
rec.event("permission", turn=2, tool="edit_file", decision="deny", reason="plan 只读")
rec.event("llm_call", turn=3, model="fake-model", tokens_in=2000, tokens_out=1000)
rec.close()

s = EventRecorder.summarize_file(trace_path)
expect(s["llm_calls"] == 2, "summary 统计 llm_calls=2")
expect(s["tokens_in"] == 3000 and s["tokens_out"] == 1500, "summary 汇总 in/out token")
# 成本 = 0.006 + (2000/1e6*2 + 1000/1e6*8=0.012) = 0.018
expect(abs(s["cost"] - 0.018) < 1e-9, "summary 汇总总成本（0.018）")
expect(s["denied"] == 1 and s["tool_calls"] >= 2, "summary 统计拒绝数与工具调用数")

# ── 5. 子 Agent 透传：脚本化假模型，两轮（先工具后结论）──
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
    def __init__(self): self.total_tokens = 30
class Resp:
    def __init__(self, m): self.choices, self.usage = [types.SimpleNamespace(message=m)], U()

script = [
    Resp(M(tool_calls=[TC("c1", "run_shell", '{"command": "ls"}')])),
    Resp(M(content="子任务结论")),
]
state = {"i": 0}
def fake_chat(messages, tool_schemas=None):
    r = script[state["i"]]; state["i"] += 1
    return r

sub_path = os.path.join(work, "sub_trace.jsonl")
rec2 = EventRecorder(path=sub_path, price_table=PRICE)
dispatch_span = rec2.event("dispatch_worker", worker_type="ops", turn=1)

tools = {"run_shell": lambda command: "file1\nfile2"}
schemas = [{"type": "function", "function": {"name": "run_shell"}}]
res = run_subagent(
    "列目录", tools=tools, tool_schemas=schemas, chat_fn=fake_chat,
    cwd=work, approver=lambda t, a: True,
    recorder=rec2, parent_span_id=dispatch_span,
)
rec2.close()
expect(res["stop_reason"] == "finished", "子 Agent 正常跑完（埋点不改变运行结果）")

sub_events = read_jsonl(sub_path)
worker_events = [e for e in sub_events if e["span_id"] != dispatch_span]
expect(len(worker_events) >= 2, "子 Agent 内部产生了 llm/工具事件")
expect(all(e["trace_id"] == rec2.trace_id for e in sub_events), "子 Agent 事件共享同一 trace_id")
expect(all(e.get("parent_id") == dispatch_span for e in worker_events),
       "子 Agent 事件的 parent_id 指向派工 span（锁/轨迹同一条透传链）")

# ── 6. NullRecorder no-op ──
null_path = os.path.join(work, "should_not_exist.jsonl")
nrec = NullRecorder(path=null_path)
sid = nrec.event("llm_call", turn=1, tokens_in=10, tokens_out=5)
with nrec.span("tool_call", tool="x") as sp:
    sp.set("status", "ok")
nrec.close()
expect(sid is None and not os.path.exists(null_path), "NullRecorder 不产生 span_id、不建文件")

# ── 7. 脱敏（密钥字面量运行时拼接，避免源码里出现完整 sk- 串）──
secret = "sk" + "-" + "secret-123"
red_path = os.path.join(work, "redact.jsonl")
rrec = EventRecorder(path=red_path, price_table=PRICE)
_eq = chr(61)  # 等号运行时拼，源码里不出现凭据形态
rrec.event("tool_call", tool="run_shell",
           args_preview="export MYKEY" + _eq + secret + " run deploy")
rrec.close()
saved = " ".join(open(red_path, encoding="utf-8").readlines())
expect(secret not in saved and "REDACTED" in saved, "密钥在落盘前被掩码")

# ── 8. 并发写不产生坏行 ──
par_path = os.path.join(work, "parallel.jsonl")
prec = EventRecorder(path=par_path, price_table=PRICE)
def writer(n):
    for i in range(50):
        prec.event("tool_call", thread=n, seq=i, tool="read_file", status="ok")
ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
[t.start() for t in ts]; [t.join() for t in ts]
prec.close()
rows = read_jsonl(par_path)
expect(len(rows) == 200, "200 条并发事件全部完整落盘（无交错坏行）")

print(f"\n[{'测试通过' if FAIL == 0 else '存在失败'}] 结构化 trace：{PASS} 条断言通过，{FAIL} 条失败")
sys.exit(1 if FAIL else 0)
