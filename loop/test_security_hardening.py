"""
test_security_hardening.py — 权限闸绕过修复的回归测试（2026-11-01）

红测来源：第四题对抗中实跑确认的绕过：
  find -delete/-exec/-fprint、git diff --output、换行偷渡、glob 展开读私钥、
  edit_file 改 permission.py 的缴械攻击。
约定：approver 一律拒绝——任何 ask 都会变 deny；只有策略自动 allow 才算绕过。
"""
import permission as P

NO = lambda t, a: False    # 最警觉用户：所有询问都拒绝
YES = lambda t, a: True    # 松懈用户：硬保护必须在这个情况下也拦得住


def decision(tool, args, approver=NO, cwd="/tmp"):
    return P.check_permission(tool, args, approver, cwd=cwd)


def expect(cond, msg):
    assert cond, msg
    print("✓", msg)


# ── 1. 只读命令的破坏开关：绝不能自动放行 ──────────────────────────
bypass_cmds = [
    "find . -name '*.tmp' -delete",
    "find . -name x -exec rm {} +",
    "find . -name x -exec rm {} \\;",
    "find . -name x -fprint /tmp/o.txt",
    "git diff --output=/tmp/p.txt HEAD",
]
for cmd in bypass_cmds:
    d = decision("run_shell", {"command": cmd})
    expect(d[0] != "allow", f"不自动放行：{cmd}  => {d[0]}")

# ── 2. 换行是命令分隔符，不能借 cat 白名单偷渡 ─────────────────────
d = decision("run_shell", {"command": "cat note.txt\nrm secret.txt"})
expect(d[0] != "allow", "换行夹带 rm 不自动放行")

# ── 3. glob/变量展开前的敏感路径：宁可问人，不放行 ─────────────────
for cmd in ["cat ~/.s*h/i*a", 'cat "$HOME"/.ssh/i*', "cat ~/.*/id_rsa"]:
    d = decision("run_shell", {"command": cmd})
    expect(d[0] != "allow", f"通配符可能展开到敏感存储：{cmd}  => {d[0]}")

# ── 4. 正常只读操作仍然零打扰自动放行（修复不能把白名单全打死）─────
for cmd in ["cat note.txt", "ls -la", "git status", "git log -5",
            "find . -name x", "grep ERROR app.log", "pwd"]:
    d = decision("run_shell", {"command": cmd})
    expect(d[0] == "allow", f"正常只读仍放行：{cmd}")

# ── 5. 缴械攻击：即使松懈 approver 全点 y，安全闸门文件也不能自改 ──
for path in ["/tmp/proj/loop/permission.py", "/tmp/proj/loop/safety.py",
             "./permission.py", "./safety.py"]:
    d = decision("edit_file", {"path": path, "old_string": "x", "new_string": "y"},
                 approver=YES, cwd="/tmp/proj/loop")
    expect(d[0] == "deny", f"硬拒绝修改安全闸门文件（即使用户点y）：{path} => {d[0]}")

# 受保护文件连 write_file 也拒
d = decision("write_file", {"path": "safety.py", "content": "pass"},
             approver=YES, cwd="/tmp/proj/loop")
expect(d[0] == "deny", "write_file 覆盖 safety.py 也被硬拒")

# ── 6. 普通业务文件修改不受影响（仍走 ask）────────────────────────
d = decision("edit_file", {"path": "notes/a.md", "old_string": "x", "new_string": "y"},
             approver=NO, cwd="/tmp/proj/loop")
expect(d[0] == "deny", "普通文件：用户拒绝时 deny（流程不变）")
d = decision("edit_file", {"path": "notes/a.md", "old_string": "x", "new_string": "y"},
             approver=YES, cwd="/tmp/proj/loop")
expect(d[0] == "allow", "普通文件：用户批准时 allow（没被硬保护误伤）")

print("\n全部通过")
