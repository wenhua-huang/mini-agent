"""
sandbox.py — OS 能力隔离（真正的安全边界，2026-11-01）

这是第四题对抗后的「架构层」修复，区别于 permission/safety 里的正则（那只是降噪层）。
思路对齐 Claude Code 2.1.88（源码证据）：
  - 它保留 Bash 字符串工具（开发者需要任意 shell），没有走纯 argv 删 shell 的极端路线；
  - shouldUseSandbox.ts 注释明确「excludedCommands 不是安全边界，沙箱权限系统才是」；
  - 真墙是 OS 沙箱：macOS seatbelt(sandbox-exec)、Linux bubblewrap+seccomp，
    可配网络域名白名单、文件读写路径隔离、failIfUnavailable。

本模块在 macOS 用 seatbelt(sandbox-exec) 实现最小版：
  - 默认 deny network*         ：Agent 默认断网，外发通道在 OS 层不存在
  - 只允许写 cwd + 系统临时目录  ：cwd 外（含家目录 ~/.ssh 等）只读/不可写
  - 敏感存储 deny file-read*   ：cat ~/.ssh/id_rsa 即使绕过正则，也被内核拒绝
  - fail-closed：请求沙箱但平台不支持 / profile 起不来 → 抛错拒绝执行，绝不静默裸跑

外层用 argv 列表调 sandbox-exec（shell=False），命令字符串只作为受控 sh -c 的单个参数，
既保留 shell 表达力，又不让命令本身注入到外层进程的参数解析里。
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class SandboxUnavailable(RuntimeError):
    """请求了强制沙箱，但当前平台/环境无法提供隔离（fail-closed 时拒绝执行）。"""


def _safe_realpath(p: str) -> str:
    """realpath 并拒绝可能用于 seatbelt profile 注入的危险字符。"""
    rp = os.path.realpath(os.path.expanduser(p))
    if any(ch in rp for ch in '()"\\\n\r'):
        raise SandboxUnavailable(f"路径含无法安全纳入沙箱策略的字符：{p}")
    return rp


# 默认在 OS 层禁止读取的敏感存储（比正则可靠：通配符展开后仍被内核拦）
DEFAULT_DENY_READ = [
    "~/.ssh",
    "~/.aws/credentials",
    "~/.netrc",
    "~/.gnupg",
]


def is_supported() -> bool:
    return platform.system() == "Darwin" and os.path.exists(SANDBOX_EXEC)


def build_seatbelt_profile(
    cwd: str,
    allow_network: bool = False,
    extra_writable: list = None,
    deny_read: list = None,
) -> str:
    """生成 seatbelt (TinyScheme) profile 文本。默认：断网 + 仅可写 cwd/tmp + 敏感路径禁读。"""
    writable = [_safe_realpath(cwd), _safe_realpath(tempfile.gettempdir()), "/dev"]
    for d in extra_writable or []:
        writable.append(_safe_realpath(d))
    # 去重保序
    writable = list(dict.fromkeys(writable))

    denies_read = [_safe_realpath(p) for p in (deny_read if deny_read is not None else DEFAULT_DENY_READ)
                   if os.path.exists(os.path.expanduser(p)) or p.startswith("~")]

    parts = ["(version 1)", "(allow default)"]
    if not allow_network:
        parts.append("(deny network*)")
    # 默认禁止一切文件写，再定点放行 cwd / tmp / /dev
    parts.append("(deny file-write*)")
    for w in writable:
        parts.append(f'(allow file-write* (subpath "{w}"))')
    # 敏感存储在 OS 层禁止读（cat/glob/变量展开都救不了）
    for r in dict.fromkeys(denies_read):
        parts.append(f'(deny file-read* (subpath "{r}"))')
    return "\n".join(join for join in parts if join)


def sandboxed_argv(command: str, cwd: str, allow_network: bool = False,
                   extra_writable: list = None, deny_read: list = None) -> list:
    """构造 argv：sandbox-exec -p <profile> /bin/sh -c <command>。外层 shell=False。"""
    if not is_supported():
        raise SandboxUnavailable(
            "OS 沙箱仅在 macOS(seatbelt) 实现；当前平台不支持，fail-closed 拒绝裸跑。"
            "Linux 应改用 bubblewrap+seccomp。")
    profile = build_seatbelt_profile(cwd, allow_network, extra_writable, deny_read)
    return [SANDBOX_EXEC, "-p", profile, "/bin/sh", "-c", command]


def run_sandboxed(command: str, cwd: str = ".", timeout: int = 30,
                  allow_network: bool = False, extra_writable: list = None,
                  deny_read: list = None) -> subprocess.CompletedProcess:
    """在 OS 沙箱内执行 shell 命令。沙箱起不来直接抛错（fail-closed），不退化为裸跑。"""
    argv = sandboxed_argv(command, cwd, allow_network, extra_writable, deny_read)
    return subprocess.run(
        argv,
        capture_output=True, text=True, timeout=timeout,
        cwd=cwd, shell=False,                 # 外层绝不经 shell
        env={**os.environ, "PYTHONPATH": ""},  # 避免宿主环境污染沙箱内解释器
    )
