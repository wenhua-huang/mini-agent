"""
mcp_client.py — 手写的最小 MCP 客户端（stdio 传输）

职责：
  1. 启动 MCP Server 子进程（stdio 传输）
  2. JSON-RPC 2.0 消息收发（每行一个 JSON）
  3. 生命周期：initialize → tools/list → tools/call
  4. 把工具结果返回给上层（Agent）

协议约定（MCP stdio 传输）：
  - stdout 只走协议消息（每行一个 JSON）
  - stderr 走日志（可以打印出来调试）
  - 请求带 id，响应用同一个 id 匹配
"""
import json
import queue
import subprocess
import threading


class McpClient:
    def __init__(self, command, args=None, env=None):
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.proc = None
        self._request_id = 0
        self._pending = {}          # id -> queue.Queue（等待响应的请求）
        self._reader_thread = None

    # ─────────────── ① 传输层 ───────────────

    def connect(self):
        """启动 Server 子进程，开始读它的输出"""
        self.proc = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,   # stderr 留给日志
            text=True,
            bufsize=1,                # 行缓冲
        )
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        return self

    def disconnect(self):
        if self.proc:
            self.proc.terminate()

    def _read_loop(self):
        """后台线程：逐行读 stdout，把响应分发到对应的 pending 队列"""
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                # 是某个请求的响应 → 唤醒等待方
                q = self._pending.pop(msg["id"], None)
                if q:
                    q.put(msg)
            # 没有 id 的消息是通知（如工具列表变化），这里忽略

    # ─────────────── ② 协议层（JSON-RPC） ───────────────

    def _request(self, method, params=None):
        """发一个 JSON-RPC 请求，阻塞等待对应 id 的响应"""
        self._request_id += 1
        req_id = self._request_id
        q = queue.Queue()
        self._pending[req_id] = q

        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params

        # MCP stdio 约定：stdout 每行写一个 JSON 消息
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

        resp = q.get(timeout=30)
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        return resp["result"]

    # ─────────────── ③ 生命周期 ───────────────

    def initialize(self):
        """第一步：协议握手（客户端第一个请求）"""
        return self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "mini-agent", "version": "0.1.0"},
        })

    def list_tools(self):
        """第二步：发现工具"""
        result = self._request("tools/list")
        return result["tools"]

    def call_tool(self, name, arguments):
        """第三步：调用工具"""
        result = self._request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        # 提取文本内容（MCP 返回 content 数组）
        texts = [c.get("text", "") for c in result.get("content", [])]
        return "\n".join(texts)


def to_openai_tool_schema(mcp_tool):
    """
    ④ 桥接层：把 MCP 工具 schema 转成 OpenAI Function Calling 格式

    MCP 工具格式：
        {"name": "get_time", "description": "...", "inputSchema": {...}}

    OpenAI 格式：
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    """
    return {
        "type": "function",
        "function": {
            "name": mcp_tool["name"],
            "description": mcp_tool.get("description", ""),
            "parameters": mcp_tool.get("inputSchema", {"type": "object", "properties": {}}),
        },
    }
