"""
permission.py — 权限守门人（对应 Claude Code 第 13 课）

每个工具执行前先过 check_permission，返回三种决策之一：
  ("allow",)       直接放行（读文件等无害操作）
  ("deny", reason) 直接拒绝（黑名单硬规则）
  ("ask", prompt)  需要人工确认（approver 返回 True/False）

设计要点：
  1. fail-closed：没命中明确规则的写/执行操作默认 ask，而不是默认放行
  2. approver 可注入：真实运行传 input() 交互函数，测试传写死的假 approver
  3. deny 不抛异常：由调用方把拒绝原因当 tool_result 回灌给模型（拒绝也是普通消息）
"""
import re
from typing import Callable, Optional, Tuple

from safety import (
    DEFAULT_TRUSTED_DOMAINS, protected_security_file,
    sensitive_path_risk, shell_risk,
)

Decision = Tuple[str, ...]  # ("allow",) / ("deny", msg) / ("ask", prompt)

# 硬黑名单：出现就直接 deny，问都不问（正则匹配命令字符串）
DENY_PATTERNS = [
    (r"\brm\s+(-\w*f\w*r|-r\w*f|-rf|-fr)\b", "递归强制删除被禁止"),
    (r"\brm\s+-rf?\s+/\b", "禁止删除根目录"),
    (r":\(\)\s*\{.*\|.*&\s*\}\s*;", "检测到 fork bomb"),
    (r"\bmkfs\.", "禁止格式化磁盘"),
    (r">\s*/dev/sd[a-z]", "禁止直接写块设备"),
]

# shell 只读命令白名单
SHELL_ALLOW_PREFIXES = (
    "ls", "pwd", "cat", "head", "tail", "find", "grep", "rg",
    "wc", "git status", "git log", "git diff", "echo", "which", "file",
)

# 一旦出现这些符号，命令可能有写入/替换副作用，不能自动放行，必须 ask
SHELL_DANGEROUS_TOKENS = (">", ">>", "`", "$(", "${", "&>")

# 「只读」命令自带的破坏/执行/写文件开关：带了就不再算只读（白名单认词，攻击用词的参数）
_READ_ONLY_FORBIDDEN = [
    (re.compile(r"^find\b"), re.compile(
        r"\s-(?:delete|exec|execdir|ok|okdir|fprint\w*|fprintf|fls)\b")),
    (re.compile(r"^git\s+diff\b"), re.compile(r"--output(?:=|\s)")),
    (re.compile(r"^(?:grep|rg|cat|head|tail|ls)\b"), re.compile(r"--?(?:output|dump|save)(?:=|\s)")),
]


def _is_read_only(cmd: str) -> bool:
    """整条命令必须：无重定向/命令替换，按 | & ; 换行 拆出的每一段都在只读白名单，
    且只读命令没有携带 -delete/-exec/--output 等破坏开关。"""
    if any(tok in cmd for tok in SHELL_DANGEROUS_TOKENS):
        return False
    for seg in re.split(r"[|&;\n]", cmd):   # 换行也是 shell 命令分隔符
        seg = seg.strip()
        if not seg:
            return False
        if not any(seg == p or seg.startswith(p + " ") for p in SHELL_ALLOW_PREFIXES):
            return False
        for cmd_re, bad_re in _READ_ONLY_FORBIDDEN:
            if cmd_re.search(seg) and bad_re.search(seg):
                return False
    return True

Approver = Callable[[str, dict], bool]  # (工具名, 参数) -> 是否批准


def interactive_approver(tool: str, args: dict) -> bool:
    """真实运行时用：在终端问人。"""
    if tool == "run_shell":
        detail = f"执行命令: {args.get('command')}"
    elif tool == "edit_file":
        detail = f"修改文件: {args.get('path')}\n      替换为: {str(args.get('new_string'))[:80]}"
    elif tool == "read_file":
        detail = f"读取文件: {args.get('path')}"
    else:
        detail = f"{tool}({args})"
    answer = input(f"\n⚠️  权限确认 | {detail}\n   允许执行吗？[y/N] ").strip().lower()
    return answer in ("y", "yes")


def check_permission(
    tool: str,
    args: dict,
    approver: Approver,
    cwd: str = ".",
    trusted_domains: Optional[set] = None,
    plan_state=None,
) -> Decision:
    if trusted_domains is None:
        trusted_domains = set(DEFAULT_TRUSTED_DOMAINS)

    # ── Plan Mode 状态切换工具（第八课）──────────────────
    # enter_plan_mode：纯状态切换，本身无副作用，始终允许。
    if tool == "enter_plan_mode":
        return ("allow",)

    # write_plan_file：只有在 plan 模式才允许写计划工件。
    if tool == "write_plan_file":
        if plan_state is None or plan_state.mode != "plan":
            return ("deny", "当前不在 plan 模式，不能写计划文件。")
        return ("allow",)

    # exit_plan_mode：必须用户批准；批准后 PlanModeState 切回 execute。
    if tool == "exit_plan_mode":
        if plan_state is None or plan_state.mode != "plan":
            return ("deny", "当前不在 plan 模式，无需退出。")
        if not approver(tool, args):
            return ("deny", "用户未批准计划，继续停留在 plan 模式。")
        result = plan_state.exit(approved=True)
        return result

    # read_file / search_in_file 通常只读 → 放行；但敏感本地存储只读也要确认
    if tool in ("read_file", "search_in_file"):
        risk = sensitive_path_risk(args.get("path", ""), cwd)
        if risk is not None:
            approved = approver(tool, args)
            if approved:
                return ("allow",)
            return ("deny", f"{risk[1]}，用户未授权。")
        return ("allow",)

    # dispatch_worker：派一个注册表里声明的工人。派单本身可放行——
    # 工人的工具表由 worker_type 硬收窄；ops 类型虽带 run_shell，其每次执行
    # 仍在工人循环内走上面同一个 run_shell 闸门（沙箱+审批）。
    if tool == "dispatch_worker":
        from worker_types import get_worker_type, DEFAULT_WORKER_TYPE
        wtype = args.get("worker_type") or DEFAULT_WORKER_TYPE
        if not str(args.get("task", "")).strip():
            return ("deny", "dispatch_worker 缺少 task。")
        if get_worker_type(wtype) is None:
            return ("deny", f"dispatch_worker 未知 worker_type={wtype!r}。")
        return ("allow",)

    # dispatch_workers：非空数组；每项是字符串或 {task, worker_type}，类型必须已注册。
    if tool == "dispatch_workers":
        from worker_types import get_worker_type, DEFAULT_WORKER_TYPE
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return ("deny", "dispatch_workers 需要非空的任务数组。")
        for t in tasks:
            if isinstance(t, str):
                text, wtype = t, DEFAULT_WORKER_TYPE
            elif isinstance(t, dict):
                text, wtype = t.get("task", ""), t.get("worker_type") or DEFAULT_WORKER_TYPE
            else:
                return ("deny", "dispatch_workers 的任务项必须是字符串或对象。")
            if not str(text).strip():
                return ("deny", "dispatch_workers 存在空任务。")
            if get_worker_type(wtype) is None:
                return ("deny", f"dispatch_workers 未知 worker_type={wtype!r}。")
        return ("allow",)

    # edit_file / write_file 是写操作：本地可逆但会改动用户文件，必须确认。
    # 但安全闸门自身硬拒绝自改——防 disarm（先把安检改成全放行再为所欲为），
    # 即使 approver 点 y 也不放行。
    if tool in ("edit_file", "write_file"):
        # Plan Mode 第一道：计划阶段除 plan 文件外，业务写入一律硬挡。
        if plan_state is not None:
            plan_decision = plan_state.guard_write(tool, args, approver)
            if plan_decision is not None:
                return plan_decision

        protected = protected_security_file(args.get("path", ""), cwd)
        if protected is not None:
            return ("deny",
                    f"安全关键文件 {protected} 禁止由 Agent 自行修改（防缴械攻击）。"
                    "如需调整权限策略，请由用户在 Agent 之外手动编辑。")
        approved = approver(tool, args)
        if approved:
            return ("allow",)
        return ("deny", "用户拒绝了该文件修改。请不要重试，或换一种方案。")

    if tool == "run_shell":
        cmd = args.get("command", "")

        # Plan Mode：只【收紧】不放松——非只读命令在黑名单/审批之前硬挡；
        # 只读命令仍继续走下面 shell_risk/白名单流程（敏感文件读取照样要问）。
        if plan_state is not None:
            plan_shell = plan_state.guard_shell(cmd, approver)
            if plan_shell is not None and plan_shell[0] == "deny":
                return plan_shell

        # 第一关：黑名单硬拒绝
        for pattern, reason in DENY_PATTERNS:
            if re.search(pattern, cmd):
                return ("deny", f"权限策略拒绝：{reason}。请换一种安全的方式。")

        # 第 1.5 关：不可信数据防护——编码执行（解码审/解不开 deny）、
        # 陌生出站域名、敏感文件读取（这些即使是「只读」命令也不能自动放行）
        risk = shell_risk(cmd, trusted_domains)
        if risk is not None:
            level, why = risk
            if level == "deny":
                return ("deny", f"权限策略拒绝：{why}")
            approved = approver(tool, args)
            if approved:
                return ("allow",)
            return ("deny", f"用户未授权：{why}。请换一种方案，不要重试同一命令。")

        # 第二关：整条命令确认为只读，才自动放行（防重定向/替换绕过）
        if _is_read_only(cmd):
            return ("allow",)

        # 第三关：其余 shell 操作问用户（fail-closed：默认不信任）
        approved = approver(tool, args)
        if approved:
            return ("allow",)
        return ("deny", "用户拒绝了该命令的执行。请换一种方案，不要重试同一命令。")

    # 未登记的工具：fail-closed，默认拒绝
    return ("deny", f"未知工具 {tool}，默认拒绝。")
