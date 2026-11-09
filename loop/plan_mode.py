"""
plan_mode.py — 主会话 Plan Mode 状态机（2026-11-02，第八课）

对齐 Claude Code 的 EnterPlanMode / ExitPlanMode：
- Plan Mode 是【主会话】的权限状态，不是子 Agent；子 Agent 不允许切换它。
- plan 模式下：业务文件写/edit、非只读 shell 一律硬挡（approver 放行也没用）；
  唯一例外是系统指定的 plan 文件（计划工件）。
- ExitPlanMode 必须用户批准；批准后切回 execute，写权限交还给原 permission 层。

本模块只做"状态 + 是否拦截"，不直接调模型/弹 UI；审批由调用方传入布尔值，
便于离线测试。
"""
from __future__ import annotations

import os
from typing import Callable, Optional, Tuple

from permission import _is_read_only  # 复用同一条只读命令判定，避免两套标准

Decision = Optional[Tuple[str, ...]]  # None=不插手，交回原权限层；否则 ("allow"|"deny", ...)


class PlanModeState:
    def __init__(self, base_dir: str = "."):
        self.mode = "execute"              # "execute" | "plan"
        self.base_dir = base_dir
        # 计划工件固定路径（类比 CC 的 plan file）
        self.plan_path = os.path.join(base_dir, ".mini_agent", "plans", "current-plan.md")

    # ── EnterPlanMode ────────────────────────────────────
    def enter(self, agent_context: bool = False) -> str:
        # 子 Agent 无权切换主会话模式（对应 CC: cannot be used in agent contexts）
        if agent_context:
            raise RuntimeError("EnterPlanMode 只能在主会话使用，子 Agent 不能切换计划模式。")
        self.mode = "plan"
        return ("Entered plan mode. 现在只能只读探索并把方案写入计划文件，"
                "不能改业务代码；准备好后用 exit_plan_mode 请求用户批准。")

    # ── ExitPlanMode ─────────────────────────────────────
    def exit(self, approved: bool) -> Tuple[str, ...]:
        if self.mode != "plan":
            return ("deny", "当前不在 plan 模式，无需退出。")
        plan = self.read_plan()
        if not plan.strip():
            raise ValueError("计划文件为空：必须先把方案写进计划文件，再请求批准。")
        if not approved:
            return ("deny", "用户未批准计划，继续停留在 plan 模式。")
        self.mode = "execute"
        return ("allow", plan)

    def read_plan(self) -> str:
        try:
            with open(self.plan_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    # ── 业务写操作闸门（edit_file / write_file）─────────
    def guard_write(self, tool: str, args: dict, approver: Callable) -> Decision:
        if self.mode != "plan":
            return None  # execute 模式不插手，交回 permission.py
        # 唯一允许写的是计划工件本身
        if os.path.abspath(args.get("path", "")) == os.path.abspath(self.plan_path):
            return ("allow",)
        return ("deny",
                "当前处于 plan 模式：禁止修改/新建业务文件。请先把方案写入计划文件，"
                "用 exit_plan_mode 取得用户批准后再动手。")

    # ── shell 闸门：plan 模式只允许只读命令 ─────────────
    def guard_shell(self, command: str, approver: Callable) -> Decision:
        if self.mode != "plan":
            return None  # execute 模式不插手
        if _is_read_only(command):
            return ("allow",)
        return ("deny",
                "当前处于 plan 模式：只能执行只读命令（ls/grep/git status 等），"
                "禁止任何改动系统状态的命令。")
