"""
mini_agent.py — 最小工具调用循环（本地工具版，不依赖 MCP）

对应 Claude Code 源码学习：
  第 7 课  工具调用循环 + 消息账本
  第 8 课  工具合同：name / description / parameters(JSON Schema) / 执行函数
  第 11 课 两道防失控闸门：max_turns、token 预算

协议差异：Anthropic 工具结果 role=user；OpenAI 兼容协议(百炼) role=tool + tool_call_id。
"""
import json
import os
import subprocess
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from context_builder import build_context_block
from permission import check_permission, interactive_approver
from api_retry import APIError, call_with_retry, classify_error
from safety import wrap_untrusted
from compaction import (
    compact_messages, estimate_tokens, SUMMARY_PROMPT, STRUCTURED_PROMPT,
)
from worker_types import WORKER_TYPES, render_worker_catalog
from plan_mode import PlanModeState
from tracer import NullRecorder

# 从 loop/.env 读取 key（已加入 .gitignore，不提交）
load_dotenv(Path(__file__).parent / ".env")

llm = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL"),
)
MODEL = "qwen-plus"

# ── 两道闸门（第 11 课）──────────────────────────────
MAX_TURNS = 6            # 最多几轮模型出手，防无限循环
MAX_TOTAL_TOKENS = 20000  # token 累计预算，防费用失控
COMPACT_THRESHOLD = 16000  # 账本估值到这个 token 数就先压缩，别等 400（占窗口 80%）
KEEP_LAST = 6             # 压缩时最近多少条消息保留原文（不切断 tool 配对）


def _llm_summarizer(old_messages: list, prompt: str = SUMMARY_PROMPT) -> str:
    """真实运行时：把较早历史交给模型，按指定提示词生成摘要（自由文本或 JSON）。"""
    resp = llm.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user",
                   "content": prompt + json.dumps(old_messages, ensure_ascii=False)}],
    )
    return resp.choices[0].message.content


# ── 1. 工具的「执行函数」：真正干活的本地代码 ─────────
# 关键：工具要在「目标项目目录」里执行，而不是 agent 脚本所在目录，
# 否则相对路径和 find . 都会跑错地方 → 模型拿到空结果 → 幻觉。
def read_file(path: str, cwd: str = ".", offset: int = 1, limit: int = 200) -> str:
    """读文件的一个行窗口（上下文工程：大文件永远不整份进上下文）。

    offset=起始行(1起)，limit=最多读多少行。默认读前 200 行；
    大文件应先用 search_in_file 定位行号，再带着 offset/limit 精读窗口。
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    if not p.exists():
        return f"错误：文件不存在 {path}"
    try:
        out, count = [], 0
        with p.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i < offset:
                    continue
                if count >= limit:
                    out.append(f"...(仅显示第 {offset} 行起 {limit} 行；"
                               f"需要后续内容请增大 offset，先用 search_in_file 定位)")
                    break
                out.append(f"{i}|{line.rstrip()}")
                count += 1
        return "\n".join(out)
    except Exception as e:
        return f"读取失败：{type(e).__name__}: {e}"


def search_in_file(path: str, pattern: str, cwd: str = ".",
                   max_matches: int = 50, context: int = 0) -> str:
    """grep 式搜索：大文件只回「行号+命中行」，不把文件灌进上下文。

    返回命中行号和内容（默认最多 50 条、每条截断 300 字符）；
    context>0 时附带每个命中前后 context 行。拿到行号后用 read_file 精读。
    """
    import re
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    if not p.exists():
        return f"错误：文件不存在 {path}"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"错误：非法正则表达式：{e}"

    hits, total = [], 0
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取失败：{type(e).__name__}: {e}"

    for i, line in enumerate(lines, start=1):
        if rx.search(line):
            total += 1
            if len(hits) >= max_matches:
                continue
            lo, hi = max(1, i - context), min(len(lines), i + context)
            block = []
            for j in range(lo, hi + 1):
                mark = ">>" if j == i else "  "
                block.append(f"{mark}{j}|{lines[j-1].rstrip()[:300]}")
            hits.append("\n".join(block))
    if total == 0:
        return f"未匹配：{pattern}（文件共 {len(lines)} 行）"
    head = f"命中 {total} 处（文件共 {len(lines)} 行），"
    if total > max_matches:
        head += f"仅展示前 {max_matches} 处，请用更精确的 pattern 或加大 max_matches：\n"
    else:
        head += "行号如下，可用 read_file(offset=行号) 精读：\n"
    return head + "\n--\n".join(hits)


def run_shell(command: str, cwd: str = ".", allow_network: bool = False) -> str:
    """在 OS 沙箱内执行 shell（真墙）：默认断网、仅可写 cwd、敏感路径禁读。

    - 保留 shell 字符串表达力（管道/复合命令），但外层 argv + shell=False，
      内层 sh -c 关在 macOS seatbelt 里（见 sandbox.py）。
    - 沙箱不可用 → fail-closed 报错，绝不静默退回裸跑 subprocess(shell=True)。
    - allow_network 应由权限层在确认目标域名可信后才传 True（默认 False=断网）。
    """
    from sandbox import SandboxUnavailable, run_sandboxed
    try:
        out = run_sandboxed(command, cwd=cwd, timeout=30, allow_network=allow_network)
    except SandboxUnavailable as e:
        return f"[拒绝执行] OS 沙箱不可用，按 fail-closed 策略不裸跑：{e}"
    except subprocess.TimeoutExpired:
        return "[错误] 命令超时（30s）已被终止"
    return (out.stdout or "") + (f"\n[stderr] {out.stderr}" if out.stderr else "")


def edit_file(path: str, old_string: str, new_string: str, cwd: str = ".") -> str:
    """对应 Claude Code 的 FileEditTool：精确查找-替换，绝不整文件重写。
    匹配 0 个或多个都报错——强制模型先 Read 看清真实内容再下手。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    if not p.exists():
        return f"错误：文件不存在 {path}，如需新建请用 write_file"
    content = p.read_text(encoding="utf-8", errors="replace")
    n = content.count(old_string)
    if n == 0:
        return ("错误：old_string 在文件中没有精确匹配。请先用 read_file 查看文件真实内容，"
                "逐字复制要替换的片段（含缩进/空格/换行），不要凭记忆构造。")
    if n > 1:
        return f"错误：old_string 在文件中匹配了 {n} 处，不唯一。请加长上下文使其只匹配一处。"
    if old_string == new_string:
        return "错误：old_string 与 new_string 相同，没有改动。"
    p.write_text(content.replace(old_string, new_string, 1), encoding="utf-8")
    return f"已替换成功：{path}（1 处）"


def write_file(path: str, content: str, cwd: str = ".") -> str:
    """整文件写入工具。当前主要用于 Plan Mode 写计划工件，业务文件新建仍需权限审批。"""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入：{path}（{len(content)} 字符）"


# ── 多 Agent（第七课）：子 Agent 类型注册表驱动 ─────────
# 每类型能干什么由 worker_types.WORKER_TYPES 声明（硬收窄），
# 主模型只通过 worker_type 选类型；这里没有「任务X→派Y」的 if/else。
def _schema_by_name(name):
    for s in TOOL_SCHEMAS:
        if s["function"]["name"] == name:
            return s
    raise KeyError(name)


# 工人可用工具的执行函数工厂；不在表里的名字（如 edit_file）工人永远拿不到。
_WORKER_TOOL_FACTORIES = {
    "read_file": lambda cwd: partial(read_file, cwd=cwd),
    "search_in_file": lambda cwd: partial(search_in_file, cwd=cwd),
    "run_shell": lambda cwd: partial(run_shell, cwd=cwd),
}


def _resolve_worker(worker_type: str, cwd: str):
    """按类型名查注册表，构造该类型专属 (声明, 工具表, schema 表)。未知类型返回 (None)*3。"""
    from worker_types import get_worker_type, DEFAULT_WORKER_TYPE
    wt = get_worker_type(worker_type or DEFAULT_WORKER_TYPE)
    if wt is None:
        return None, None, None
    tools = {n: _WORKER_TOOL_FACTORIES[n](cwd) for n in wt.tool_names}
    schemas = [_schema_by_name(n) for n in wt.tool_names]
    return wt, tools, schemas


def dispatch_worker(task: str, worker_type: str = "", cwd: str = ".",
                    approver=None, chat_fn=None, plan_state=None,
                    recorder=None, parent_span_id=None) -> str:
    """编排者工具：把一个独立子任务派给某个【类型】的全新隔离工人 Agent。

    选哪个类型由主模型给 worker_type（它看过工具描述里的 when_to_use 清单）；
    该类型实际能调哪些工具、用什么人设和预算，由注册表硬决定，模型改不了。
    plan_state 非 None 时，主会话的 plan 只读锁传播给工人（第九课）。
    recorder 非 None 时，工人事件挂到本次 dispatch span 下，共享 trace_id（第十课）。
    """
    from subagent import run_subagent
    wt, worker_tools, worker_schemas = _resolve_worker(worker_type, cwd)
    if wt is None:
        return f"[拒绝派单] 未知 worker_type={worker_type!r}，可选：explore/planner/ops"
    fn = chat_fn or _chat_create
    rec = recorder or NullRecorder()
    dispatch_span = rec.event("dispatch_worker", parent_id=parent_span_id,
                              worker_type=wt.name, turn_hint=worker_type)
    result = run_subagent(
        task, tools=worker_tools, tool_schemas=worker_schemas,
        chat_fn=fn, cwd=cwd, approver=approver,
        max_turns=wt.max_turns, system_prompt=wt.system_prompt,
        plan_state=plan_state,
        recorder=rec, parent_span_id=dispatch_span,
    )
    return (f"[子Agent:{wt.name}/stop={result['stop_reason']}, "
            f"turns={result['turns']}, tokens={result['tokens']}, "
            f"工具调用={result['tool_calls']}]\n{result['conclusion']}")


def dispatch_workers(tasks, cwd=".", approver=None, chat_fn=None, plan_state=None,
                     recorder=None, parent_span_id=None) -> str:
    """编排者工具（并行版）：把多个互相独立的子任务同时派出，每个可带自己的类型。

    tasks 元素兼容两种写法：
      - 字符串：旧用法，按默认类型 explore；
      - {"task": ..., "worker_type": "planner"|"ops"|...}：显式选类型。
    各工人独立账本/工具表/预算，按注册表硬收窄；并行跑完按提交顺序汇总。
    """
    from subagent import run_subagents_parallel
    from worker_types import DEFAULT_WORKER_TYPE

    # 归一化为 (task, worker_type)
    norm = [(t, DEFAULT_WORKER_TYPE) if isinstance(t, str)
            else (t.get("task", ""), t.get("worker_type", "") or DEFAULT_WORKER_TYPE)
            for t in tasks]

    shared_tools = {
        "read_file": partial(read_file, cwd=cwd),
        "search_in_file": partial(search_in_file, cwd=cwd),
        "run_shell": partial(run_shell, cwd=cwd),
    }
    # 所有工人工具的并集（线程内各自按 specs 收窄，互不可见对方工具）
    union_schemas = [_schema_by_name(n) for n in shared_tools]
    specs, resolved_tasks, type_names = [], [], []
    for task_text, wtype in norm:
        wt, _, _ = _resolve_worker(wtype, cwd)
        if wt is None:
            return f"[拒绝派单] 未知 worker_type={wtype!r}，可选：explore/planner/ops"
        type_names.append(wt.name)
        resolved_tasks.append(task_text)
        specs.append({
            "tools": {n: shared_tools[n] for n in wt.tool_names},
            "tool_schemas": [_schema_by_name(n) for n in wt.tool_names],
            "system_prompt": wt.system_prompt,
            "max_turns": wt.max_turns,
        })

    fn = chat_fn or _chat_create
    rec = recorder or NullRecorder()
    parallel_span = rec.event("dispatch_workers", parent_id=parent_span_id,
                              worker_types=type_names, n=len(resolved_tasks))
    results = run_subagents_parallel(
        resolved_tasks, tools=shared_tools, tool_schemas=union_schemas,
        chat_fn=fn, cwd=cwd, approver=approver, specs=specs,
        plan_state=plan_state,  # 经 **kw 透传给每个工人的 check_permission
        recorder=rec, parent_span_id=parallel_span,  # 轨迹同样透传，工人事件挂同一父 span
    )
    lines = [f"共并行派出 {len(results)} 个子Agent（类型：{', '.join(type_names)}）："]
    for r in results:
        wtname = type_names[r["index"]]
        lines.append(
            f"\n— 子Agent #{r['index'] + 1} [{wtname}/stop={r['stop_reason']}, "
            f"turns={r['turns']}, tokens={r['tokens']}] 任务：{r['task']}\n结论：{r['conclusion']}"
        )
    return "\n".join(lines)


# ── 2. 工具合同：name + description + JSON Schema 参数 ─
#    模型就是靠这张「说明书」决定调谁、怎么填参数
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取指定路径文本文件的一个行窗口。默认只返回前200行（带行号）。"
                "大文件请先用 search_in_file 定位，再传 offset(起始行,1起)/limit(行数) 精读那一段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对或相对路径"},
                    "offset": {"type": "integer", "description": "起始行号（从1开始），默认1"},
                    "limit": {"type": "integer", "description": "最多读取行数，默认200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_file",
            "description": (
                "在大文件里按正则搜索，只返回命中行的行号和内容（默认最多50条），"
                "不会把整个文件读入。排查大型日志/数据文件时先用它定位行号，"
                "再用 read_file(offset=行号, limit=行数) 精读那一小段。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "pattern": {"type": "string", "description": "正则表达式，如 ERROR|Traceback"},
                    "max_matches": {"type": "integer", "description": "最多返回多少条命中，默认50"},
                    "context": {"type": "integer", "description": "每个命中附带的前后行数，默认0"},
                },
                "required": ["path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在终端执行 shell 命令并返回输出。用于列目录、统计行数、运行测试等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "精确修改文件：用 old_string 查找、new_string 替换。"
                "必须先 read_file 读取真实内容，再逐字复制要改的片段作为 old_string（含缩进换行），"
                "保证它在文件中唯一。只改这一处，不重写整个文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "old_string": {"type": "string", "description": "文件中必须逐字一致、且唯一的原文片段"},
                    "new_string": {"type": "string", "description": "替换后的新内容"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "整文件写入文本。除 Plan Mode 的计划文件外，业务文件新建仍必须获得权限批准。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件路径"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "enter_plan_mode",
            "description": (
                "面对非平凡实现任务时进入主会话 Plan Mode：只读探索和设计，不改业务代码；"
                "把方案写到计划文件后调用 exit_plan_mode 请求用户批准。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_plan_file",
            "description": (
                "把实施计划写入 Plan Mode 指定的计划文件。只能在 enter_plan_mode 之后使用。"
                "计划应包含分步方案、关键文件、风险与验证方式。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {"type": "string", "description": "完整实施计划"},
                },
                "required": ["plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exit_plan_mode",
            "description": "计划写好后提交给用户审批；只有用户批准后才会回到 execute 模式并允许改业务代码。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_worker",
            "description": (
                "把一个独立、可并行的子任务派给一个全新的子Agent。子Agent看不到当前对话，"
                "有自己干净的上下文和独立预算，跑完只返回一段结论。用 worker_type 选择专职类型，"
                "该类型实际能调哪些工具由系统硬性决定。\n"
                + render_worker_catalog()
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "完整自包含的子任务描述（子Agent看不到对话历史，背景要写全）",
                    },
                    "worker_type": {
                        "type": "string",
                        "enum": list(WORKER_TYPES.keys()),
                        "description": "工人类型，按上面清单的 when_to_use 选择；缺省 explore",
                    },
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dispatch_workers",
            "description": (
                "一次性把【多个互相独立】的子任务【并行】派给多个全新子Agent同时执行，"
                "全部完成后按提交顺序汇总返回。每个子Agent上下文/工具/预算独立。"
                "适合同时调研多个互不相干对象；任务间有先后依赖时不要用。\n"
                + render_worker_catalog()
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "多个子任务。每项可以是一句任务字符串（默认 explore），"
                                       "或 {\"task\": 任务, \"worker_type\": 类型} 显式选类型。",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "task": {"type": "string"},
                                        "worker_type": {"type": "string",
                                                        "enum": list(WORKER_TYPES.keys())},
                                    },
                                    "required": ["task"],
                                },
                            ]
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
]


def _chat_create(messages, tool_schemas=None):
    """真正发起一次 API 调用；openai 异常翻译成带 HTTP 状态码的 APIError。

    tool_schemas 默认用主 Agent 的全量工具；子 Agent 传入收窄后的只读工具表。
    """
    try:
        return llm.chat.completions.create(
            model=MODEL, messages=messages,
            tools=tool_schemas if tool_schemas is not None else TOOL_SCHEMAS,
        )
    except Exception as e:
        # openai SDK 的异常都带 status_code（版本 1.x）
        status = getattr(e, "status_code", None) or getattr(e, "status", None)
        name = type(e).__name__
        if status is None:
            # 按异常类型兜底映射
            if "Authentication" in name:
                status = 401
            elif "RateLimit" in name:
                status = 429
            elif "Timeout" in name or "APITimeout" in name:
                raise TimeoutError(str(e)) from e
            elif "Connection" in name:
                raise ConnectionError(str(e)) from e
            else:
                raise
        raise APIError(status, str(e)) from e


def run_agent(task: str, cwd: str = ".", approver=interactive_approver,
              summarizer=_llm_summarizer, compact_strategy: str = "summarize",
              worker_chat_fn=None, plan_state=None, recorder=None,
              trace_path: str = "") -> str:
    # ── Plan Mode（第八课）：主会话状态机；可注入便于离线测试 ──
    if plan_state is None:
        plan_state = PlanModeState(base_dir=cwd)

    # ── 结构化 trace（第十课）：默认 NullRecorder 不产生文件、不改旧行为；
    #    显式给 trace_path 才落 JSONL。注入 recorder 优先（测试用）。──
    if recorder is None:
        if trace_path:
            from tracer import EventRecorder
            import atexit
            recorder = EventRecorder(path=trace_path)
            atexit.register(recorder.close)  # 每事件已 flush；atexit 只做句柄收尾
        else:
            recorder = NullRecorder()

    # ── 3. 消息账本：贯穿整个会话，只增不减（第 7 课核心）──
    # 系统消息 = 基础人设 + 上下文构建器喂的背景情报（第 12 课）
    system_prompt = (
        "你是一个能用工具完成任务的助手。需要信息时先调工具，拿到结果后再用中文回答。\n"
        "凡是需要修改或新建文件，必须调用 edit_file/write_file 真正落盘，"
        "不要只在回复文本里贴代码；工具报错（含参数 JSON 错误）时请按提示修正参数后重试，不要放弃工具改用口述。\n\n"
        + build_context_block(cwd)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    total_tokens = 0

    # 工具分发表：把本次会话的目标目录绑定进每个工具（模型只传业务参数，不知道 cwd）
    tool_implementations = {
        "read_file": partial(read_file, cwd=cwd),
        "search_in_file": partial(search_in_file, cwd=cwd),
        "run_shell": partial(run_shell, cwd=cwd),
        "edit_file": partial(edit_file, cwd=cwd),
        "write_file": partial(write_file, cwd=cwd),
        # Plan Mode（第八课）
        "enter_plan_mode": lambda: plan_state.enter(agent_context=False),
        "write_plan_file": lambda plan: write_file(plan_state.plan_path, plan, cwd=cwd),
        "exit_plan_mode": lambda: (
            "用户已批准计划，现在进入 execute 模式。按以下计划实施：\n\n"
            + plan_state.read_plan()
            if plan_state.mode == "execute"
            else "当前不在 plan 模式。"
        ),
        # 多 Agent：派单给只读工人；worker_chat_fn 仅测试注入，生产默认走真实模型。
        # plan_state 透传给工人：主会话 plan 模式的只读锁随之传播（第九课）。
        # recorder 透传：工人的 trace 事件挂到主任务同一 trace_id 下（第十课）。
        "dispatch_worker": partial(dispatch_worker, cwd=cwd,
                                   approver=approver, chat_fn=worker_chat_fn,
                                   plan_state=plan_state, recorder=recorder),
        "dispatch_workers": partial(dispatch_workers, cwd=cwd,
                                    approver=approver, chat_fn=worker_chat_fn,
                                    plan_state=plan_state, recorder=recorder),
    }

    # ── 4. 工具调用循环本体 ────────────────────────────
    for turn in range(1, MAX_TURNS + 1):
        # 压缩闸门：账本到阈值（约窗口80%）先压缩，别等 API 400
        if estimate_tokens(messages) > COMPACT_THRESHOLD:
            before = len(messages)
            # 真实 LLM 摘要器在 structured 策略下换用强制 JSON 的提示词；
            # 测试注入的摘要器原样使用（它只接收 old_messages）。
            active_summarizer = summarizer
            if compact_strategy == "structured" and summarizer is _llm_summarizer:
                active_summarizer = partial(_llm_summarizer, prompt=STRUCTURED_PROMPT)
            messages = compact_messages(
                messages, active_summarizer, keep_last=KEEP_LAST, strategy=compact_strategy)
            label = {"summarize": "历史摘要", "structured": "结构化摘要",
                     "truncate": "截断说明"}.get(compact_strategy, compact_strategy)
            print(f"[压缩:{compact_strategy}] 账本 {before} 条 → {len(messages)} 条"
                  f"（system + {label} + 最近 {KEEP_LAST} 条原文）")
            recorder.event("compaction", turn=turn, strategy=compact_strategy,
                           messages_before=before, messages_after=len(messages))

        print(f"\n===== 第 {turn} 轮：调用模型 =====")
        try:
            resp = call_with_retry(
                lambda: _chat_create(messages),
                max_retries=4,
                on_retry=lambda a, k, d: (
                    print(f"[重试] {k}，第 {a} 次，{d:.1f}s 后再试"),
                    recorder.event("retry", turn=turn, attempt=a,
                                   error_kind=k, backoff_s=round(d, 2)),
                ),
            )
        except APIError as e:
            kind = classify_error(e)
            if kind == "auth":
                return "（认证失败 401/403：请检查 .env 里的 API key，重试无意义，已停止）"
            if kind == "bad_request":
                return f"（请求被拒绝 400（可能上下文过长）：{e}）"
            raise
        except RuntimeError as e:
            return f"（{e}）"  # 连续过载 → 真实系统这里会换备用模型

        # 闸门②：token 预算（流式时也是逐条 usage 累加，这里非流式同理）
        total_tokens += resp.usage.total_tokens
        print(f"[账本] 本轮 {resp.usage.total_tokens} tokens，累计 {total_tokens}")
        recorder.event(
            "llm_call", turn=turn, model=MODEL,
            tokens_in=getattr(resp.usage, "prompt_tokens", 0),
            tokens_out=getattr(resp.usage, "completion_tokens", 0),
        )
        if total_tokens > MAX_TOTAL_TOKENS:
            print(f"[闸门] 超过 token 预算 {MAX_TOTAL_TOKENS}，强制停止")
            recorder.event("budget_exceeded", turn=turn,
                           total_tokens=total_tokens, limit=MAX_TOTAL_TOKENS)
            return "（已达 token 预算上限，任务中止）"

        msg = resp.choices[0].message

        # 情况 A：模型没要工具 → 纯文本就是最终答案，循环结束
        if not msg.tool_calls:
            print("[循环] 模型返回纯文本 → 结束")
            print(f"\n最终答案：\n{msg.content}")
            return msg.content

        # 情况 B：模型请求调工具。先把它的「请求」原样记入账本
        messages.append(msg.model_dump(exclude_none=True))

        # 逐个执行工具（真实 Claude Code 里只读工具在这里并行）
        for tc in msg.tool_calls:
            name = tc.function.name
            with recorder.span("tool_call", tool=name, turn=turn,
                               tool_call_id=tc.id) as sp:
                # 真实模型偶发吐出非法 JSON 参数（如未转义的 \d）。坏参数绝不能炸循环：
                # 当作工具错误回灌，让模型下一轮自己修正（真实联调 2026-11-02 暴露）。
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    bad_args = None
                except json.JSONDecodeError as e:
                    args = {}
                    bad_args = (f"工具参数不是合法 JSON，本次未执行：{type(e).__name__}: {e}。"
                                "请修正参数（注意 JSON 字符串里的反斜杠要双写，如 \\d 写成 \\\\d）后重试。")
                    print(f"[工具] {name} 参数 JSON 解析失败：{e}")
                    sp.set("status", "invalid_args")
                    sp.set("error_type", type(e).__name__)

                if bad_args is not None:
                    result = bad_args
                else:
                    print(f"[工具] 模型请求调用 {name}({args})")

                    # ── 权限闸门（第 13 课）：执行前先检查 ──
                    # check_permission 内部已处理 ask（问 approver），返回值只有 allow/deny
                    decision = check_permission(name, args, approver, cwd=cwd,
                                                plan_state=plan_state)
                    if decision[0] == "deny":
                        # 不执行工具，把拒绝原因当 tool_result 回灌（拒绝也是普通消息）
                        result = decision[1]
                        print(f"[权限] 拒绝：{result}")
                        sp.set("decision", "deny")
                        sp.set("status", "denied")
                        recorder.event("permission", turn=turn, tool=name,
                                       decision="deny", reason=decision[1])
                    else:
                        sp.set("decision", "allow")
                        fn = tool_implementations.get(name)
                        if fn is None:
                            result = f"错误：未知工具 {name}"
                            sp.set("status", "no_tool")
                        else:
                            try:
                                result = fn(**args)
                                sp.set("status", "ok")
                            except Exception as e:
                                # 工具报错不能炸掉整个循环：把异常当结果回灌，模型可以据此改主意
                                result = f"工具执行出错：{type(e).__name__}: {e}"
                                sp.set("status", "error")
                                sp.set("error_type", type(e).__name__)
                print(f"[工具] 结果前 120 字：{str(result)[:120].replace(chr(10), ' ')}")

                # 结果回灌：OpenAI 协议 role=tool，必须带 tool_call_id 对上号
                # 安全：工具返回一律按「不可信数据」包裹（可能含提示词注入），
                # 但硬防线不在这里——危险动作在上面的 check_permission 已被拦截。
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": wrap_untrusted(name, str(result)),
                })
        # for 结束 → 回到循环顶部，带着工具结果再次请求模型

    # 闸门①：max_turns 到顶
    print(f"\n[闸门] 已达最大轮次 {MAX_TURNS}，强制停止")
    recorder.event("max_turns_reached", turn=MAX_TURNS, limit=MAX_TURNS)
    return "（已达最大轮次，任务中止）"


if __name__ == "__main__":
    import sys
    # 默认目标：claude-code-learning 仓库（带 git 上下文）
    # 注意：相对的是「脚本文件位置」而不是运行时 CWD——VSCode 调试器的 CWD 可能是别的目录
    default_cwd = Path(__file__).resolve().parents[2] / "claude-code-learning"
    target_cwd = sys.argv[1] if len(sys.argv) > 1 else str(default_cwd)
    task = sys.argv[2] if len(sys.argv) > 2 else "这个项目当前在哪个 git 分支？最近一次提交说了什么？"
    trace_path = sys.argv[3] if len(sys.argv) > 3 else ""  # 给路径就落结构化 trace JSONL
    run_agent(task, cwd=target_cwd, trace_path=trace_path)
