"""
test_sandbox_os.py — OS 能力隔离的真实验证（macOS seatbelt）

与前面字符串层测试不同：这里命令真的执行，但全部关在沙箱里：
  - 不起外网，只连本机线程内 HTTP 服务
  - 不碰真实 ~/.ssh，用临时假敏感目录验证 file-read deny
  - 写测试只在临时目录内，验证写家目录/区外被内核拒绝
"""
import http.server
import os
import socketserver
import tempfile
import threading

import sandbox as SB


def expect(cond, msg):
    assert cond, msg
    print("✓", msg)


if not SB.is_supported():
    print("跳过：当前平台非 macOS 或无 sandbox-exec")
    raise SystemExit(0)

work = tempfile.mkdtemp(prefix="sbox_work_")
fake_home = tempfile.mkdtemp(prefix="sbox_home_")
# 越界写目标放在真实家目录下（系统 tmp 按设计是可写的，不算越界）
outside_dir = os.path.join(os.path.expanduser("~"), ".sbox_outside_probe")
os.makedirs(outside_dir, exist_ok=True)
fake_secret_dir = os.path.join(fake_home, ".ssh")
os.makedirs(fake_secret_dir)
secret_path = os.path.join(fake_secret_dir, "id_rsa")
with open(secret_path, "w") as f:
    f.write("FAKE-PRIVATE-KEY-NOT-REAL")

# ── 1. 起一个本机 HTTP 服务（线程内，不外网）────────────────────────
PORT = 8923
Handler = http.server.SimpleHTTPRequestHandler
httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()

def run(cmd, **kw):
    p = SB.run_sandboxed(cmd, cwd=work, timeout=10, **kw)
    return p

# ── 2. 默认断网：沙箱内连本机服务必须失败 ──────────────────────────
p = run(f"/usr/bin/curl -s -m 3 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{PORT}/")
expect(p.returncode != 0 and "000" in (p.stdout or ""),
       f"默认 deny network：沙箱内 curl 连不上（rc={p.returncode}, out={p.stdout!r}）")

# 显式放行网络后能连（证明不是 curl/环境坏了，而是隔离在起作用）
p = run(f"/usr/bin/curl -s -m 3 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{PORT}/",
        allow_network=True)
expect(p.returncode == 0 and "200" in (p.stdout or ""),
       f"allow_network=True 时可连（rc={p.returncode}, out={p.stdout!r}），反证上面是隔离生效")

# ── 3. 文件写：只能写 cwd，写区外和家目录都被内核拒 ────────────────
p = run("echo hello > inside.txt && cat inside.txt")
expect(p.returncode == 0 and "hello" in p.stdout, "沙箱内可写工作目录（正常工作不受影响）")

esc = os.path.join(outside_dir, "escape.txt")
p = run(f"echo x > {esc}")
expect(p.returncode != 0 and not os.path.exists(esc),
       "写工作目录之外（家目录探测区）被 OS 拒绝，文件未产生")

home_esc = os.path.join(os.path.expanduser("~"), ".sbox_os_test_escape")
p = run(f"echo x > {home_esc}")
expect(p.returncode != 0 and not os.path.exists(home_esc),
       "写用户家目录被 OS 拒绝（即使正则被绕过也改不到用户文件）")

# ── 4. 敏感路径 OS 层禁读：用假 .ssh，通配符展开也救不了 ───────────
p = run(f"cat {secret_path}", deny_read=[fake_secret_dir])
expect(p.returncode != 0 and "FAKE-PRIVATE-KEY" not in (p.stdout or ""),
       "直接 cat 假私钥被 file-read deny 拦，内容未进入输出")

p = run(f"cat {fake_secret_dir}/i*", deny_read=[fake_secret_dir])
expect(p.returncode != 0 and "FAKE-PRIVATE-KEY" not in (p.stdout or ""),
       "通配符 i* 展开后仍被内核拦（正则层的 glob 绕过在 OS 层无效）")

# ── 5. 外层 shell=False：命令里的 shell 元字符只在受控 sh -c 内生效，
#       管道等正常能力保留（可用性没被砍）──────────────────────────
p = run("echo abc | tr a-z A-Z")
expect(p.returncode == 0 and "ABC" in p.stdout, "沙箱内仍可用管道（保留 shell 表达力）")

argv = SB.sandboxed_argv("echo hi; rm -f x", cwd=work)
expect(argv[0] == SB.SANDBOX_EXEC and argv[-1] == "echo hi; rm -f x" and argv[-2] == "-c",
       "命令整体作为 sh -c 的单个 argv 元素，外层不经过 shell 解析")

# ── 6. fail-closed：路径含 profile 注入字符时拒绝生成策略 ──────────
try:
    SB.build_seatbelt_profile(cwd=work, extra_writable=['/tmp/x")(allow network*)'])
    raise AssertionError("含注入字符的路径应被拒")
except SB.SandboxUnavailable:
    expect(True, "可疑路径字符 fail-closed，拒绝注入 seatbelt profile")

httpd.shutdown()

# 清理在家目录建的探测目录（沙箱外清理）
import shutil
shutil.rmtree(outside_dir, ignore_errors=True)
print("\n全部通过")
