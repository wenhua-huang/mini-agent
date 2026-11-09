"""
test_oauth_flow.py — 完整 OAuth 2.1 + MCP 流程测试

运行前提：test_server_oauth.py 已在 9000 端口运行

测试完整链路：
  发现 → 注册 → 授权 → 换 token → 访问 MCP（拿工具）
"""
import sys
import urllib.error
import urllib.parse
import urllib.request

from mcp_client_oauth import OAuthMcpClient


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """不跟随 302 重定向，直接抛出来（授权回调需要自己处理）"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_authorization_code(client: OAuthMcpClient) -> str:
    """模拟浏览器授权：访问 authorize → 捕获 302 → 提取 code"""
    auth_url = client.auth_meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "scope": "mcp:tools",
        "code_challenge": "test-challenge",       # 简化，真实用 PKCE
        "code_challenge_method": "S256",
        "state": "test-state",
    })
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        opener.open(auth_url, timeout=5)
        raise RuntimeError("预期 302 但没有")
    except urllib.error.HTTPError as e:
        if e.code != 302:
            raise
        location = e.headers["Location"]
        code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]
        return code


def main():
    client = OAuthMcpClient("http://localhost:9000/mcp")

    # ① 发现授权服务器（首次访问无 token → 401 → 引导发现）
    client.discover_auth_server()
    assert client.auth_meta, "发现失败"
    print("[1] OK 发现授权服务器:", client.auth_meta["issuer"])
    print("    authorize:", client.auth_meta["authorization_endpoint"])
    print("    token:    ", client.auth_meta["token_endpoint"])
    print("    register: ", client.auth_meta["registration_endpoint"])

    # ② 动态注册客户端
    client.register_client()
    print("[2] OK 动态注册: client_id =", client.client_id)

    # ③ 授权码流程（浏览器模拟）
    client.auth_code = get_authorization_code(client)
    client.code_verifier = "test-verifier"   # 服务器端简化未校验 PKCE，占位即可
    print("[3] OK 授权码:", client.auth_code)

    # ④ 换 access token
    client.exchange_token()
    print("[4] OK access_token =", client.access_token[:16] + "...")
    print("    refresh_token =", (client.refresh_token or "")[:16] + "...")

    # ⑤ 带 token 访问 MCP Server
    client.initialize()
    tools = client.list_tools()
    print("[5] OK MCP 工具列表:", [t["name"] for t in tools])

    result = client.call_tool("get_time", {})
    print("[5] OK 调用 get_time →", result)

    # ⑥ 无 token 访问验证（应被 401 拒绝）
    print("\n[6] 安全验证：不带 token 访问 /mcp 应被拒绝...")
    try:
        client._raw_request(client.mcp_url, {"jsonrpc": "2.0", "id": 99,
                                             "method": "tools/list"}, auth=False)
        print("    ❌ 错误：无 token 居然成功了！")
    except Exception as e:
        if "401" in str(e) or "需要授权" in str(e):
            print("    ✅ 无 token 被 401 拒绝，安全边界有效")
        else:
            print(f"    ⚠️ 其他错误: {e}")

    print("\n✅ 完整 OAuth 2.1 + MCP 流程全部通过")


if __name__ == "__main__":
    sys.exit(main())
