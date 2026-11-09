"""
mcp_client_oauth.py — 远程 HTTP MCP 客户端（完整 OAuth 2.1 授权流程）

与 mcp_client.py（stdio 本地版）的区别：
  stdio 版：无网络认证，直接连
  本版：    发现 → 注册 → 授权 → 换 token → 访问 → 刷新

流程：
  1. 请求 MCP Server → 401 → 发现授权服务器
  2. 获取授权端点、token 端点、注册端点
  3. 动态注册客户端（拿 client_id / client_secret）
  4. 浏览器走 PKCE 授权码流程
  5. 换 access_token
  6. 带 token 访问 MCP Server（initialize → tools/list → tools/call）
  7. token 过期自动刷新
"""
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

MCP_SERVER_URL = "https://mcp.example.com/mcp"


class OAuthMcpClient:
    def __init__(self, mcp_server_url, redirect_port=8765):
        self.mcp_url = mcp_server_url
        self.redirect_port = redirect_port
        self.redirect_uri = f"http://localhost:{redirect_port}/callback"

        self.auth_meta = None          # 授权服务器元数据
        self.client_id = None
        self.client_secret = None
        self.access_token = None
        self.refresh_token = None
        self.token_expires_at = 0
        self._request_id = 0

    # ═════════════ 阶段一：发现授权服务器 ═════════════

    def discover_auth_server(self):
        """① 请求 MCP Server → 401 → 发现授权服务器"""
        try:
            self._raw_request(self.mcp_url, {})
        except MCPAuthError as e:
            print(f"[1] 收到 401，开始发现授权服务器...")
            # 从 WWW-Authenticate 或默认位置发现
            well_known = self.mcp_url.replace("/mcp", "/.well-known/oauth-authorization-server")
            self.auth_meta = self._get_json(well_known)
            print(f"[1] 发现授权服务器: {self.auth_meta['issuer']}")
            print(f"    authorize: {self.auth_meta['authorization_endpoint']}")
            print(f"    token:     {self.auth_meta['token_endpoint']}")
            print(f"    register:  {self.auth_meta['registration_endpoint']}")

    # ═════════════ 阶段二：动态客户端注册 ═════════════

    def register_client(self):
        """② 向 registration_endpoint 注册，拿 client_id/client_secret"""
        body = {
            "client_name": "mini-agent",
            "redirect_uris": [self.redirect_uri],
            "token_endpoint_auth_method": "client_secret_post",
        }
        resp = self._post_json(self.auth_meta["registration_endpoint"], body)
        self.client_id = resp["client_id"]
        self.client_secret = resp.get("client_secret")
        print(f"[2] 注册成功: client_id={self.client_id}")

    # ═════════════ 阶段三：PKCE 授权码流程 ═════════════

    def authorize(self):
        """③ 生成 PKCE，打开浏览器授权，拦截回调拿 code"""
        # PKCE：随机 verifier → SHA256 → challenge
        self.code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64url(hashlib.sha256(
            self.code_verifier.encode()).digest())

        state = secrets.token_urlsafe(16)

        # 构造授权 URL
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "mcp:tools",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        auth_url = (self.auth_meta["authorization_endpoint"] + "?"
                    + urllib.parse.urlencode(params))

        print(f"[3] 打开浏览器授权: {auth_url}")
        webbrowser.open(auth_url)

        # 本地起一个回调服务器，拦截授权码
        self.auth_code = None
        server = HTTPServer(("localhost", self.redirect_port),
                            self._make_callback_handler(state))
        server.handle_request()   # 等一个请求就够
        server.server_close()
        print(f"[3] 拿到授权码: {self.auth_code[:8]}...")

    # ═════════════ 阶段四：换 token ═════════════

    def exchange_token(self):
        """④ 用 code + code_verifier 换 access_token"""
        body = {
            "grant_type": "authorization_code",
            "code": self.auth_code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code_verifier": self.code_verifier,   # PKCE 验证
        }
        resp = self._post_json(self.auth_meta["token_endpoint"], body)
        self._save_tokens(resp)
        print(f"[4] 获得 access_token（{resp['expires_in']}s 有效）")

    # ═════════════ 阶段六：刷新 token ═════════════

    def refresh_token_if_needed(self):
        """⑤ token 过期前自动刷新"""
        if time.time() < self.token_expires_at - 60:
            return
        body = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        resp = self._post_json(self.auth_meta["token_endpoint"], body)
        self._save_tokens(resp)
        print(f"[刷新] token 已刷新")

    # ═════════════ 阶段五：访问 MCP Server ═════════════

    def initialize(self):
        return self._mcp_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "mini-agent-oauth", "version": "0.1.0"},
        })

    def list_tools(self):
        return self._mcp_request("tools/list")["tools"]

    def call_tool(self, name, arguments):
        result = self._mcp_request("tools/call", {"name": name, "arguments": arguments})
        return "\n".join(c.get("text", "") for c in result.get("content", []))

    # ───────────── 内部工具方法 ─────────────

    def _mcp_request(self, method, params=None):
        """带 token 的 MCP JSON-RPC 请求（401 时自动刷新重试一次）"""
        self.refresh_token_if_needed()
        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params:
            body["params"] = params
        try:
            resp = self._raw_request(self.mcp_url, body, auth=True)
        except MCPAuthError:
            # token 失效 → 刷新 → 重试一次
            print("token 失效，刷新后重试...")
            self.refresh_token()
            resp = self._raw_request(self.mcp_url, body, auth=True)
        return resp["result"]

    def _raw_request(self, url, body, auth=False):
        """发 JSON-RPC 请求；401 抛 MCPAuthError"""
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        if auth and self.access_token:
            req.add_header("Authorization", f"Bearer {self.access_token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise MCPAuthError("需要授权")
            raise

    def _get_json(self, url):
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())

    def _post_json(self, url, body):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def _save_tokens(self, resp):
        self.access_token = resp["access_token"]
        self.refresh_token = resp.get("refresh_token", self.refresh_token)
        self.token_expires_at = time.time() + resp.get("expires_in", 3600)

    def _make_callback_handler(self, expected_state):
        """构造回调 HTTP handler：截获授权码"""
        class CallbackHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query)
                if query.get("state", [""])[0] != expected_state:
                    self.send_response(400); self.end_headers(); return
                self.server.auth_code = query.get("code", [""])[0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authorization complete, you can close this tab.")
            def log_message(self, *args):
                pass
        return CallbackHandler


class MCPAuthError(Exception):
    pass


def base64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ═════════════ 完整流程演示 ═════════════

def full_flow():
    client = OAuthMcpClient(MCP_SERVER_URL)

    # 阶段一：发现
    client.discover_auth_server()

    # 阶段二：注册
    client.register_client()

    # 阶段三：授权（浏览器）
    client.authorize()

    # 阶段四：换 token
    client.exchange_token()

    # 阶段五：访问 MCP
    client.initialize()
    tools = client.list_tools()
    print(f"[5] 发现工具: {[t['name'] for t in tools]}")
    print(f"[5] 调用结果: {client.call_tool('get_time', {})}")


if __name__ == "__main__":
    full_flow()
