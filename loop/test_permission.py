"""权限层离线单测：不碰真实模型，只验证三档决策。"""
from permission import check_permission


def always_yes(tool, args): return True
def always_no(tool, args): return False


def expect(cond, msg):
    assert cond, msg
    print("✓", msg)


# 1. 只读工具直接放行
expect(check_permission("read_file", {"path": "x"}, always_no)[0] == "allow",
       "read_file 总是 allow（只读，连 approver 都不问）")

# 2. 只读 shell 白名单放行（即使 approver 永远说不，也不该问它）
asked = []
def spy_no(tool, args):
    asked.append(args["command"])
    return False
expect(check_permission("run_shell", {"command": "ls -la"}, spy_no)[0] == "allow",
       "ls 白名单自动 allow，不打扰用户")
expect(not asked, "白名单命令没有调用 approver")
expect(check_permission("run_shell", {"command": "git status --short"}, always_no)[0] == "allow",
       "git status 自动 allow")

# 3. 黑名单硬拒绝（即使用户一直点同意也必须拦）
d = check_permission("run_shell", {"command": "rm -rf /"}, always_yes)
expect(d[0] == "deny" and "递归强制删除" in d[1] or "根目录" in d[1],
       "rm -rf / 即使 approver=yes 也 deny（硬黑名单优先于人）")
expect(check_permission("run_shell", {"command": "sudo rm -fr ."}, always_yes)[0] == "deny",
       "rm -fr 变体被拦")
expect(check_permission("run_shell", {"command": "mkfs.ext4 /dev/sda1"}, always_yes)[0] == "deny",
       "mkfs 被拦")

# 4. 非白名单命令 → ask；approver 说否 → deny，说是 → allow
expect(check_permission("run_shell", {"command": "npm install lodash"}, always_no)[0] == "deny",
       "npm install 用户拒绝 → deny")
expect(check_permission("run_shell", {"command": "npm install lodash"}, always_yes)[0] == "allow",
       "npm install 用户批准 → allow")

# 5. 管道/链接命令不能被白名单绕过
expect(check_permission("run_shell", {"command": "ls && rm -rf ."}, always_yes)[0] == "deny",
       "白名单前缀 + 黑名单后半段仍被拦（黑名单先判）")

# 5b. 重定向/命令替换不能借只读命令名蒙混过关（真实绕过漏洞回归）
expect(check_permission("run_shell", {"command": "echo 'hi' > hello.txt"}, always_no)[0] == "deny",
       "echo 重定向写文件：有 > 不算只读，approver 拒绝 → deny")
expect(check_permission("run_shell", {"command": "echo 'hi' > hello.txt"}, always_yes)[0] == "allow",
       "echo 重定向写文件：approver 明确批准才 allow")
expect(check_permission("run_shell", {"command": "cat $(touch z)"}, always_no)[0] == "deny",
       "命令替换 $(...) 不算只读 → ask → 拒绝")
expect(check_permission("run_shell", {"command": "cat notes.md | grep todo"}, always_no)[0] == "allow",
       "纯只读管道 cat|grep 自动放行")
expect(check_permission("run_shell", {"command": "grep a x.txt && rm -rf y"}, always_no)[0] == "deny",
       "只读 && 黑名单：黑名单先判 deny")

# 6. 未登记工具 fail-closed
expect(check_permission("send_missile", {"x": 1}, always_yes)[0] == "deny",
       "未知工具默认 deny（fail-closed）")

print("\n全部通过")
