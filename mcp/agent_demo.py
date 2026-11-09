"""
agent_demo.py — Mini Agent + MCP 客户端 + 百炼 LLM

流程：
  1. 连接 MCP Server，发现工具
  2. 把工具转成 OpenAI Function Calling 格式
  3. Agent 循环：LLM 决定 → MCP 执行 → 结果回传 → 再决策
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from mcp_client import McpClient, to_openai_tool_schema

load_dotenv()

LLM = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL"),
)
MODEL = "qwen-plus"


def run_agent(mcp: McpClient, user_input: str, max_rounds: int = 5):
    """Agent 主循环：LLM 决策 ↔ MCP 工具执行"""
    # ① 从 MCP Server 发现工具，转成 LLM 能懂的格式
    mcp_tools = mcp.list_tools()
    tools = [to_openai_tool_schema(t) for t in mcp_tools]
    print(f"[Agent] 发现 MCP 工具: {[t['name'] for t in mcp_tools]}")

    # ② 消息历史（MCP 工具结果也放这里）
    messages = [
        {"role": "system", "content": "你是一个助手，可以使用 MCP 工具获取信息。"},
        {"role": "user", "content": user_input},
    ]

    for _round in range(max_rounds):
        resp = LLM.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
        )
        msg = resp.choices[0].message

        # ── 情况 A：LLM 没有调工具 → 直接输出答案，结束 ──
        if not msg.tool_calls:
            print(f"\n[Agent] 最终回答:\n{msg.content}")
            return msg.content

        # ── 情况 B：LLM 决定调用工具 → 记录它的请求 ──
        print(f"\n[Agent] 第{_round + 1}轮：LLM 决定调用工具")
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # ── 执行 MCP 工具，结果放回消息历史 ──
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            print(f"[MCP] 调用 {name}({args}) ...")
            result = mcp.call_tool(name, args)
            print(f"[MCP] 结果: {result}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,   # MCP 返回的文本直接作为工具结果
            })

    print("[Agent] 达到最大轮次，停止")
    return None


if __name__ == "__main__":
    # 启动测试 MCP Server（用绝对路径，避免在别的工作目录下找不到脚本）
    server_path = str(Path(__file__).parent / "test_server.py")
    mcp = McpClient("python3", [server_path])
    mcp.connect()
    mcp.initialize()

    run_agent(mcp, "现在几点了？")

    # 再测一个需要计算的问题
    run_agent(mcp, "帮我算一下 137 + 286 等于多少？")

    mcp.disconnect()
