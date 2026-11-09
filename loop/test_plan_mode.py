"""
test_plan_mode.py — 第八课：主会话 Plan Mode 状态机与审批闸门（离线）

对齐 Claude Code：
- EnterPlanMode 只允许主会话使用；进入后业务写入/非只读 shell 被拒绝。
- 计划阶段允许写唯一 plan 文件（计划工件），但不能改业务文件。
- ExitPlanMode 必须由用户批准；批准后切回 execute，才能写业务代码。
- 子 Agent 上下文不允许切换主会话模式。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plan_mode import PlanModeState
from permission import check_permission

PASS = 0
def expect(cond, label):
    global PASS
    assert cond, "失败：" + label
    PASS += 1
    print("✓", label)


def deny_all(tool, args):
    return False


def allow_all(tool, args):
    return True


work = tempfile.mkdtemp(prefix="plan_mode_")
plan_path = os.path.join(work, ".mini_agent", "plans", "current-plan.md")

st = PlanModeState(base_dir=work)
expect(st.mode == "execute", "初始是 execute 模式，不影响正常写代码流程")
expect(st.plan_path == plan_path, "计划文件路径由状态机统一指定")

# execute 模式：PlanModeState 不插手，交回原 permission.py 审批流程
d = st.guard_write("edit_file", {"path": os.path.join(work, "app.py")}, deny_all)
expect(d is None, "execute 模式下 PlanModeState 不拦截写操作，交回原权限层")

# 进入 Plan Mode：主会话动作
msg = st.enter(agent_context=False)
expect(st.mode == "plan" and "plan" in msg.lower(), "EnterPlanMode 切到 plan 模式")

# 子 Agent 不能进入/切换主会话模式
try:
    st.enter(agent_context=True)
    expect(False, "子 Agent 调用 EnterPlanMode 必须报错")
except RuntimeError:
    expect(True, "子 Agent 调用 EnterPlanMode 被拒绝，不能改主会话状态")

# Plan 阶段：业务文件写入被硬挡，即使 approver 全部允许也不行
d = st.guard_write("edit_file", {"path": os.path.join(work, "app.py")}, allow_all)
expect(d[0] == "deny" and "plan" in d[1], "plan 模式禁止改业务文件，approver 放行也不能绕过")

d = st.guard_write("write_file", {"path": os.path.join(work, "src", "x.py")}, allow_all)
expect(d[0] == "deny", "plan 模式禁止 write_file 新建业务文件")

# 但唯一计划文件可以写（它是计划工件，不是业务代码）
d = st.guard_write("write_file", {"path": st.plan_path}, deny_all)
expect(d[0] == "allow", "plan 模式允许写唯一 plan 文件，且无需业务写入审批")

# Plan 阶段：只读 shell 放行；非只读 shell 拒绝，不问人（状态机先挡）
expect(st.guard_shell("ls -la", deny_all)[0] == "allow", "plan 模式允许只读 shell：ls")
expect(st.guard_shell("git status", deny_all)[0] == "allow", "plan 模式允许只读 shell：git status")
d = st.guard_shell("touch x.txt", allow_all)
expect(d[0] == "deny" and "plan" in d[1], "plan 模式禁止非只读 shell，approver 放行也没用")
d = st.guard_shell("echo hi > note.txt", allow_all)
expect(d[0] == "deny", "plan 模式禁止重定向写文件")

# 没写计划就 Exit：拒绝
try:
    st.exit(approved=False)
    expect(False, "空计划不能提交审批")
except ValueError:
    expect(True, "空计划不能 ExitPlanMode")

# 写入计划工件
os.makedirs(os.path.dirname(st.plan_path), exist_ok=True)
with open(st.plan_path, "w", encoding="utf-8") as f:
    f.write("# 计划\n1. 改 app.py\n2. 跑测试\n")

# 用户不批准：保持 plan，不允许执行
d = st.exit(approved=False)
expect(d[0] == "deny" and st.mode == "plan", "用户拒绝计划：仍停留 plan 模式，不开工")

# 用户批准：切到 execute，返回计划内容
d = st.exit(approved=True)
expect(d[0] == "allow" and st.mode == "execute", "用户批准计划：切回 execute 模式")
expect("改 app.py" in d[1], "ExitPlanMode 返回计划内容给主 Agent 执行")

# 回到 execute 后，状态机放行（None）；随后原权限层 approver allow 即恢复业务文件修改
d = st.guard_write("edit_file", {"path": os.path.join(work, "app.py")}, allow_all)
expect(d is None, "批准后状态机不再拦截业务文件修改")
d = check_permission("edit_file", {"path": os.path.join(work, "app.py")},
                     approver=allow_all, cwd=work)
expect(d[0] == "allow", "批准后 execute 模式经原权限层恢复业务文件修改能力")

# execute 下 guard_shell 不决策（返回 None），继续走 permission.py 原有 shell 逻辑
expect(st.guard_shell("touch x.txt", deny_all) is None,
       "execute 模式 PlanModeState 不拦截 shell，交回原权限层")

# ── 权限层把三个 Plan Mode 工具纳入显式闸门 ──
d = check_permission("enter_plan_mode", {}, approver=deny_all, cwd=work)
expect(d[0] == "allow", "enter_plan_mode 是主会话状态切换，本身允许调用")

d = check_permission("write_plan_file", {"plan": "# 方案"}, approver=deny_all, cwd=work)
expect(d[0] == "deny", "execute 模式不能写计划文件（还没进入 plan）")

st2 = PlanModeState(base_dir=work)
st2.enter(agent_context=False)
d = check_permission("write_plan_file", {"plan": "# 方案"}, approver=deny_all,
                     cwd=work, plan_state=st2)
expect(d[0] == "allow", "plan 模式写计划文件允许，且不走业务文件审批")

d = check_permission("exit_plan_mode", {}, approver=deny_all, cwd=work, plan_state=st2)
expect(d[0] == "deny", "exit_plan_mode 需要用户批准；拒绝则不开工")

# 先落一个计划，再由 allow approver 批准退出
os.makedirs(os.path.dirname(st2.plan_path), exist_ok=True)
with open(st2.plan_path, "w", encoding="utf-8") as f:
    f.write("# 可执行计划\n")
d = check_permission("exit_plan_mode", {}, approver=allow_all, cwd=work, plan_state=st2)
expect(d[0] == "allow" and st2.mode == "execute", "批准 exit_plan_mode 后回到 execute")

print(f"\n[测试通过] Plan Mode 状态机：{PASS} 条断言全绿")
