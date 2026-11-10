# mini-agent — 手写 LLM Agent 工程内核

[English](#english) · [中文](#中文)

> A from-scratch LLM Agent runtime built **without LangChain or any agent framework**,
> to understand and harden the real engineering behind tool-calling agents: the run loop,
> context management, security boundaries, observability, multi-agent orchestration,
> MCP integration, crash recovery, and evaluation.
>
> 不依赖 LangChain 等任何 Agent 框架，从零手写的可运行 LLM Agent，用来吃透工具调用型 Agent
> 的真实工程问题：运行循环、上下文工程、安全边界、可观测性、多智能体编排、MCP 接入、
> 崩溃恢复与评测。**这是一个学习/工程实践项目，不是商业产品。**

---

<a id="english"></a>
## English

### Why this project exists

Calling an LLM API is easy. Keeping an agent **reliable, safe, and observable across long,
multi-step, tool-using tasks** is the actual engineering problem. Every mechanism here is
implemented by hand and backed by an offline test, rather than imported from a framework.

### Layout

| Dir | What it holds |
|---|---|
| `loop/` | Core runtime: tool-calling loop, context compaction, permission gate, prompt-injection defenses, macOS Seatbelt sandbox, tracer, sub-agents, RAG, recoverable workflow runtime |
| `mcp/` | A hand-written MCP client/server over stdio + JSON-RPC 2.0, including a full OAuth 2.1 dynamic-registration flow |
| `eval/` | A miniature SWE-bench-style replay harness: fixed kata issues, hidden judges that inspect world state, `pass@1` over repeated runs |

### Mechanisms (each has offline tests)

- **Run loop & reliability** — ReAct-style `tool_call → local execution → tool_result` loop
  with `max_turns` and token-budget stop conditions; API error classification with
  exponential backoff that distinguishes retryable from fatal errors.
- **Context engineering** — file reads use keyword locate + line-window reading instead of
  full dumps (validated on a ~200k-line / 20MB synthetic log: ~120 chars to locate, ~630
  chars to read). Three compaction strategies (truncate / summarize / structured) trigger on
  a token threshold with schema validation and fail-closed degradation (10 messages,
  283 → 96 tokens in the benchmark case).
- **Observability & cost** — a structured JSONL tracer records every model/tool call as a
  span (`span_id`/`parent_id`, `duration_ms`), computes token cost from a price table, and
  redacts secrets (e.g. `sk-...`) before writing to disk.
- **Defense in depth** — application-layer allow/deny (sensitive paths, egress domain
  allowlist, decode-then-inspect encoded commands, untrusted-result wrapping, policy file the
  agent cannot rewrite), plus six real bypasses reproduced and fixed to prove string
  blacklists are not a security boundary. The real wall is an OS-level **macOS Seatbelt**
  sandbox (network denied by default, only the working directory writable, fail-closed),
  verified with positive/negative controls on a local loopback service.
- **Multi-agent** — orchestrator/worker: sub-agents get isolated context, isolated budgets,
  and a narrowed read-only tool set, returning conclusions only. ThreadPoolExecutor
  fan-out/fan-in preserves order and isolates failures (3 workers: 0.41s vs 1.2s serial, ~66%
  lower latency).
- **MCP** — minimal client and server over stdio (JSON-RPC 2.0,
  `initialize → tools/list → tools/call`), plus OAuth 2.1 with RFC 7591 dynamic client
  registration, authorization code, and Bearer enforcement.
- **Recoverable runtime** — task entity + event stream + lease/fencing tokens, so a real
  process crash (`os._exit`) is taken over by a separate process and replayed; an idempotency
  gate prevents side effects after a safe point from running twice.
- **Evaluation** — fixed katas run in clean copies; the judge only inspects the world's end
  state (test exit code), never the agent's self-report, and supports repeated trials with
  `pass@1`. Judges self-check both ways: the broken start must fail, a correct fix must pass.

### Test scale (measured, not estimated)

- **26** `test_*.py` files — **25** self-contained assertion suites plus 1 mock-server fixture
  (`mcp/test_server_oauth.py`, a `serve_forever` harness the OAuth flow test depends on).
- **347** `expect(...)` assertions (plus 32 bare `assert` statements, 379 total; counted by AST on 2026-11-10). All 25
  suites pass offline; the OAuth flow passes once its fixture server is started.
- Live model runs use Alibaba Bailian `qwen-plus` over an OpenAI-compatible API and cost real
  tokens, so they are kept separate from the zero-token offline regression suites.

### Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Offline tests (no network, no tokens) — run from inside each directory:
cd loop && for t in test_*.py; do python "$t"; done; cd ..

# MCP OAuth flow needs its mock server first (two terminals):
python mcp/test_server_oauth.py      # terminal 1: fixture on :9000
python mcp/test_oauth_flow.py        # terminal 2: full discovery→register→auth→call flow

# Live model run requires loop/.env with DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL
# (this file is git-ignored and is never committed):
python loop/mini_agent.py <project_dir> "<task>" [trace.jsonl]
```

### Honest scope

- RAG is a minimal in-memory vector store, not a production retrieval system.
- The eval harness currently ships one kata; the single live-model trial recorded in
  `eval/results/` did **not** pass, which is exactly why the framework measures `pass@1`
  across repeated runs rather than trusting one demo. No pass rate is claimed.
- The sandbox target is macOS Seatbelt; a Linux port would use bubblewrap + seccomp.

---

<a id="中文"></a>
## 中文

### 为什么做这个项目

调 LLM API 很简单；让一个 Agent 在长链路、多步骤、带工具调用的任务里保持**可靠、安全、
可观测**，才是真正的工程问题。这里每个机制都手写实现并配离线测试，而不是从框架里 import。

### 目录结构

| 目录 | 内容 |
|---|---|
| `loop/` | 核心运行时：工具调用循环、上下文压缩、权限闸、提示注入防御、macOS Seatbelt 沙箱、tracer、子 Agent、RAG、可恢复工作流运行时 |
| `mcp/` | 手写 MCP 客户端/服务端（stdio + JSON-RPC 2.0），含完整 OAuth 2.1 动态注册流程 |
| `eval/` | 缩小版 SWE-bench 回放评测：固定 kata 题、只看世界终态的隐藏 judge、重复运行统计 pass@1 |

### 核心机制（每个都有离线测试）

- **运行循环与可靠性**：ReAct 式 `tool_call → 本地执行 → tool_result` 循环，轮次与 token
  预算双停止条件；API 错误分类 + 指数退避，区分可重试与致命错误。
- **上下文工程**：文件读取改为「关键词定位 + 行窗口精读」（约 20 万行/20MB 假日志验证：
  定位约 120 字符、精读约 630 字符替代全量入栈）；truncate/summarize/structured 三种压缩
  策略按 token 阈值触发，摘要结构校验失败 fail-closed（基准用例 10 条消息 283→96 token）。
- **可观测与成本**：结构化 JSONL tracer，每次模型/工具调用记为 span（span_id/parent_id、
  duration_ms），按价格表折算 token 成本，落盘前对 `sk-` 等密钥脱敏。
- **安全纵深防御**：应用层 allow/deny（敏感路径、出站域名白名单、编码命令解码再审、不可信
  结果包裹、防 Agent 自改策略文件），并复现修复 6 类真实绕过，证明字符串黑白名单不是安全
  边界；真正的墙是 OS 层 **macOS Seatbelt** 沙箱（默认断网、仅工作目录可写、fail-closed），
  用本地回环服务做正反对照验证。
- **多智能体**：orchestrator/worker，子 Agent 独立上下文、独立预算、收窄的只读工具集、只回传
  结论；ThreadPoolExecutor fan-out/fan-in 按序归位、单点失败不拖垮整批（3 工人 0.41s vs
  串行 1.2s，降约 66%）。
- **MCP**：stdio 最小客户端/服务端（JSON-RPC 2.0，`initialize → tools/list → tools/call`），
  另实现 OAuth 2.1（RFC 7591 动态注册、授权码、Bearer 校验）完整本地流程。
- **可恢复运行时**：任务实体 + 事件流 + 租约/fencing 令牌，进程真崩溃（`os._exit`）后由独立
  进程接管重放，幂等闸门保证安全点之后的副作用不重复执行。
- **评测**：固定 kata 在干净副本运行，judge 只看世界终态（测试退出码）不看 Agent 自述，
  支持重复试验统计 pass@1；判分标准双向自检（缺陷起点必红、正确修复必绿）。

### 测试规模（实测，非估算）

- **26** 个 `test_*.py` 文件：**25** 个自包含断言套件 + 1 个模拟服务器夹具
  （`mcp/test_server_oauth.py`，OAuth 流程测试依赖的 `serve_forever` 服务）。
- **347** 条 `expect(...)` 断言（另有 32 条裸 `assert`，合计 379；2026-11-10 用 AST 逐文件清点）。25 个套件离线全绿；
  OAuth 流程在夹具服务器启动后通过。
- 真模型联调用阿里百炼 `qwen-plus`（OpenAI 兼容协议）、真实计费，与零 token 的离线回归
  套件严格分开。

### 运行方式

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 离线测试（不触网、不花 token），在各目录内运行：
cd loop && for t in test_*.py; do python "$t"; done; cd ..

# MCP OAuth 流程需先启动模拟服务器（两个终端）：
python mcp/test_server_oauth.py      # 终端1：:9000 夹具
python mcp/test_oauth_flow.py        # 终端2：发现→注册→授权→调用 全流程

# 真模型运行需在 loop/.env 配置 DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL
# （该文件已被 git-ignore，绝不入库）：
python loop/mini_agent.py <项目目录> "<任务>" [trace.jsonl]
```

### 诚实边界

- RAG 是内存向量库的最小实践，不是生产检索系统。
- 评测框架目前只有 1 个 kata；`eval/results/` 里唯一一次真模型试验**未通过**——这正是框架
  要用重复运行的 pass@1 而不是单次演示说话的原因，本人不声称任何通过率。
- 沙箱针对 macOS Seatbelt；Linux 移植方向是 bubblewrap + seccomp。
