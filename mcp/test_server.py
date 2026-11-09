"""
test_server.py — 最小 MCP Server（stdio 传输，用于本地测试客户端）

实现：
  - initialize 握手
  - tools/list：返回 2 个工具（get_time / add）
  - tools/call：执行工具并返回结果

协议约定：
  - stdout 每行一个 JSON 消息
  - 日志走 stderr（用 print(..., file=sys.stderr)）
"""
import json
import sys
from datetime import datetime


# ─────────── 工具实现 ───────────

def get_time(arguments):
    """返回当前时间"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add(arguments):
    """两个数相加"""
    a = arguments.get("a", 0)
    b = arguments.get("b", 0)
    return str(a + b)


TOOLS = [
    {
        "name": "get_time",
        "description": "获取当前时间",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "add",
        "description": "计算两个整数之和",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "第一个数"},
                "b": {"type": "integer", "description": "第二个数"},
            },
            "required": ["a", "b"],
        },
    },
]

HANDLERS = {
    "get_time": get_time,
    "add": add,
}


# ─────────── 协议处理 ───────────

def handle_request(msg):
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test-server", "version": "0.1.0"},
        }

    elif method == "tools/list":
        return {"tools": TOOLS}

    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in HANDLERS:
            return {
                "content": [{"type": "text", "text": f"未知工具: {name}"}],
                "isError": True,
            }
        try:
            result = HANDLERS[name](arguments)
            return {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"工具执行失败: {e}"}],
                "isError": True,
            }

    elif method == "ping":
        return {}

    else:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    print("test-server 启动", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "method" in msg and "id" not in msg:
            # 通知类消息（如 notifications/initialized），无需响应
            print(f"收到通知: {msg.get('method')}", file=sys.stderr)
            continue

        result = handle_request(msg)
        # 带 id 的请求必须回响应（id 原样带回，客户端靠它匹配）
        response = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
