"""
tracer.py — 结构化轨迹记录器（第十课：可观测性）。

把原来散落在控制台的 print（[账本]/[工具]/[权限]/[重试]）升级成
append-only 的 JSONL 事件流，一次任务 = 一个 trace_id，
每次模型/工具调用 = 一个带 span_id（可有 parent_id）的事件。

三层定位（别混）：
  messages[] 喂模型（要压缩、会丢历史）；
  print      给人调试（一闪而过、机器查不了）；
  trace      给机器查（落盘、可回放、可算成本，本模块）。

设计：
  - EventRecorder：真实记录器，持有文件句柄 + 线程锁，支持多线程并行子 Agent。
  - NullRecorder：no-op 兜底，默认接入时不改变任何旧行为、不产生文件。
  - span()：上下文管理器，自动记 duration_ms；异常记 status=error 后照常抛出。
  - 成本：llm_call 事件按 price_table（每百万 token 的 in/out 单价）折算。
  - 脱敏：所有字符串字段落盘前过 redact_text，掩码 sk- 密钥等。
"""
import json
import re
import threading
import uuid
from datetime import datetime

# sk- 开头的密钥（含可能的后缀字符）；命中整体替换，避免进 trace 文件
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")
_REDACTION = "REDACTED"


def redact_text(value):
    """对单个字符串做密钥掩码；非字符串原样返回。"""
    if not isinstance(value, str):
        return value
    return _SECRET_RE.sub(_REDACTION, value)


def _redact_fields(fields):
    """对事件字典里所有字符串值脱敏（嵌套不深，一层够用）。"""
    out = {}
    for k, v in fields.items():
        if isinstance(v, str):
            out[k] = redact_text(v)
        elif isinstance(v, dict):
            out[k] = {kk: redact_text(vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out


class _Span:
    """span() 返回的句柄：进入时计时，退出时落一条带 duration_ms 的事件。"""

    def __init__(self, recorder, event_type, fields):
        self._rec = recorder
        self._type = event_type
        self._fields = dict(fields)
        self._span_id = None
        self._t0 = None

    def set(self, key, value):
        """在 with 块内补充/覆盖字段，如 sp.set('status','ok')。"""
        self._fields[key] = value

    def __enter__(self):
        import time
        self._t0 = time.perf_counter()
        self._span_id = self._rec._new_span_id()
        return self

    def __exit__(self, exc_type, exc, tb):
        import time
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        fields = dict(self._fields)
        fields["duration_ms"] = duration_ms
        if exc_type is not None:
            fields["status"] = "error"
            fields["error_type"] = exc_type.__name__
        self._rec._write(self._type, span_id=self._span_id, **fields)
        return None  # 不吞异常，让调用方的 try/except 照常工作


class EventRecorder:
    """把结构化事件写入 JSONL 文件的真实记录器，线程安全。"""

    def __init__(self, path, price_table=None, trace_id=None):
        self.path = path
        self.price_table = price_table or {}
        self.trace_id = trace_id or ("t-" + uuid.uuid4().hex[:12])
        self._lock = threading.Lock()
        self._fh = open(path, "a", encoding="utf-8")
        self._closed = False

    @staticmethod
    def _new_span_id():
        return "s-" + uuid.uuid4().hex[:16]

    def _cost_for(self, model, tokens_in, tokens_out):
        price = self.price_table.get(model)
        if not price:
            return 0.0
        return (tokens_in or 0) / 1e6 * price.get("in", 0.0) \
            + (tokens_out or 0) / 1e6 * price.get("out", 0.0)

    def _write(self, event_type, span_id=None, **fields):
        if self._closed:
            return None
        span_id = span_id or self._new_span_id()
        event = {
            "trace_id": self.trace_id,
            "span_id": span_id,
            "type": event_type,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
        }
        event.update(_redact_fields(fields))
        # llm_call 折算成本（输入/输出单价不同，必须拆开）
        if event_type == "llm_call":
            event["cost"] = round(self._cost_for(
                fields.get("model"),
                fields.get("tokens_in", 0), fields.get("tokens_out", 0)), 9)
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
        return span_id

    def event(self, event_type, parent_id=None, **fields):
        """立即记录一条事件，返回 span_id（parent_id 可挂到父 span）。"""
        if parent_id is not None:
            fields["parent_id"] = parent_id
        return self._write(event_type, **fields)

    def span(self, event_type, parent_id=None, **fields):
        """返回上下文管理器，退出时自动落事件并记 duration_ms。"""
        if parent_id is not None:
            fields["parent_id"] = parent_id
        return _Span(self, event_type, fields)

    def close(self):
        with self._lock:
            if not self._closed:
                self._fh.close()
                self._closed = True

    # ── 离线汇总：读整个 JSONL，产出成本/质量台账 ──
    @staticmethod
    def summarize_file(path):
        summary = {
            "llm_calls": 0, "tool_calls": 0, "denied": 0, "errors": 0,
            "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
        }
        try:
            f = open(path, encoding="utf-8")
        except FileNotFoundError:
            return summary
        with f:
            for line in f:
                if not line.strip():
                    continue
                e = json.loads(line)
                t = e.get("type")
                if t == "llm_call":
                    summary["llm_calls"] += 1
                    summary["tokens_in"] += e.get("tokens_in", 0)
                    summary["tokens_out"] += e.get("tokens_out", 0)
                    summary["cost"] += e.get("cost", 0.0)
                elif t == "tool_call":
                    summary["tool_calls"] += 1
                if t == "permission" and e.get("decision") == "deny":
                    summary["denied"] += 1
                if e.get("status") == "error":
                    summary["errors"] += 1
        summary["cost"] = round(summary["cost"], 9)
        return summary


class _NullSpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None  # 不吞异常

    def set(self, key, value):
        pass


class NullRecorder:
    """no-op 记录器：默认接入用它，不建文件、不产 span_id，旧行为完全不变。"""

    def __init__(self, path=None, price_table=None, trace_id=None):
        self.path = path
        self.trace_id = None

    def event(self, event_type, parent_id=None, **fields):
        return None

    def span(self, event_type, parent_id=None, **fields):
        return _NullSpan()

    def close(self):
        pass

    @staticmethod
    def summarize_file(path):
        return {"llm_calls": 0, "tool_calls": 0, "denied": 0, "errors": 0,
                "tokens_in": 0, "tokens_out": 0, "cost": 0.0}
