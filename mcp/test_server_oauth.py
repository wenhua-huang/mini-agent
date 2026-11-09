"""
test_server_oauth.py — 带 OAuth 2.1 的模拟 MCP Server（本地测试用）

模拟三个角色：
  - 授权服务器（/.well-known/oauth-authorization-server, /authorize, /token, /register）
  - MCP Server（/mcp，要求 Bearer token）
  - 动态注册（RFC 7591）

真实场景中授权服务器是公司 SSO / Keycloak / Auth0，
这里是简化版演示完整流程。
"""
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 9000
BASE = f"http://localhost:{PORT}"

# 内存状态
clients = {}          # client_id -> client_secret
auth_codes = {}       # code -> client_id（5分钟有效）
tokens = {}           # token -> client_id
refresh_tokens = {}   # refresh_token -> client_id


def make_response(handler, code, obj):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()   # ← 关键：不 flush 客户端会一直等


class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        print(f"[srv] {args[0] % args[1:]}", flush=True)

    # ─────────── 发现授权服务器元数据 ───────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/.well-known/oauth-authorization-server":
            make_response(self, 200, {
                "issuer": BASE,
                "authorization_endpoint": f"{BASE}/authorize",
                "token_endpoint": f"{BASE}/token",
                "registration_endpoint": f"{BASE}/register",
                "scopes_supported": ["mcp:tools"],
                "code_challenge_methods_supported": ["S256"],
            })

        # 授权页：模拟用户点击"同意"后 302 回调
        elif path == "/authorize":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = "authcode-" + str(int(time.time()))
            auth_codes[code] = q["client_id"][0]
            redirect = q["redirect_uri"][0] + f"?code={code}&state={q['state'][0]}"
            self.send_response(302)
            self.send_header("Location", redirect)
            self.end_headers()

        elif path == "/mcp":
            # ═══ MCP Server：要求 Bearer token，没有就 401 ═══
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in tokens:
                self.send_response(401)
                self.send_header("WWW-Authenticate",
                                 'Bearer realm="mcp"')
                self.end_headers()
                return
            # GET 请求无 body，传空 dict
            self._handle_mcp({})

        else:
            make_response(self, 404, {"error": "not found"})

    # ─────────── 注册 / 换 token / MCP 请求 ───────────
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if path == "/register":
            # 动态客户端注册（RFC 7591）
            client_id = "client-" + str(len(clients) + 1)
            secret = "secret-" + str(int(time.time()))
            clients[client_id] = secret
            make_response(self, 201, {
                "client_id": client_id,
                "client_secret": secret,
                "client_id_issued_at": int(time.time()),
            })

        elif path == "/token":
            grant = body.get("grant_type")
            if grant == "authorization_code":
                # 校验 PKCE（简化：不验证 verifier，真实要验证）
                code = body.get("code")
                if code not in auth_codes:
                    make_response(self, 400, {"error": "invalid_grant"})
                    return
                cid = auth_codes.pop(code)
                token = "access-" + str(int(time.time()))
                refresh = "refresh-" + str(int(time.time()))
                tokens[token] = cid
                refresh_tokens[refresh] = cid
                make_response(self, 200, {
                    "access_token": token,
                    "refresh_token": refresh,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                })
            elif grant == "refresh_token":
                rt = body.get("refresh_token")
                if rt not in refresh_tokens:
                    make_response(self, 400, {"error": "invalid_grant"})
                    return
                cid = refresh_tokens[rt]
                token = "access-" + str(int(time.time()))
                tokens[token] = cid
                make_response(self, 200, {
                    "access_token": token,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                })
            else:
                make_response(self, 400, {"error": "unsupported_grant_type"})

        elif path == "/mcp":
            # ═══ MCP Server：要求 Bearer token，没有就 401（OAuth 引导）═══
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or auth[7:] not in tokens:
                self.send_response(401)
                self.send_header("WWW-Authenticate",
                                 'Bearer realm="mcp"')
                self.end_headers()
                return
            self._handle_mcp(body)

        else:
            make_response(self, 404, {"error": "not found"})

    # ─────────── MCP 协议处理（和 stdio 版一样）───────────
    def _handle_mcp(self, msg):
        # body 由 do_POST 读取后传入，这里不再读（避免二次 read 阻塞）
        method = msg.get("method")
        print(f"[srv] mcp method: {method}", flush=True)

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "oauth-test-server", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": "get_time", "description": "获取当前时间",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]}
        elif method == "tools/call":
            import datetime
            result = {
                "content": [{"type": "text",
                             "text": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}],
                "isError": False,
            }
        elif method == "ping":
            result = {}
        else:
            result = {"error": {"code": -32601, "message": f"not found: {method}"}}

        make_response(self, 200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": result})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("localhost", PORT), OAuthHandler)
    print(f"OAuth MCP 测试服务器启动: {BASE}")
    server.serve_forever()
