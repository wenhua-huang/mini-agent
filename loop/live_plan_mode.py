"""
live_plan_mode.py — 真实模型联调：qwen-plus 会不会自发走 enter_plan_mode → 写计划 → exit → 执行？

安全护栏：
- cwd 是独立临时目录，里面只有一个故意写得简陋的 todo.py。
- 自动审批人：exit_plan_mode 放行（模拟用户签字）；edit_file/write_file 仅当目标在
  临时目录内才放行；run_shell 一律拒绝（联调不需要真跑命令）；其余拒绝。
- 用 monkeypatch 记录【每一次】工具调用与权限决策，打印完整轨迹。
任务描述刻意不出现"先规划/进 plan mode"等字眼，避免我们替模型做决定。
"""
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mini_agent as ma
from plan_mode import PlanModeState

# ── 放宽主循环预算（走完整 enter→调研→计划→批准→执行 可能超过默认 6 轮）──
ma.MAX_TURNS = 14
ma.MAX_TOTAL_TOKENS = 60000

# ── 造一个真实小项目：简陋待办，缺"完成标记 + 按状态过滤" ──
work = tempfile.mkdtemp(prefix="live_plan_")
with open(os.path.join(work, "todo.py"), "w", encoding="utf-8") as f:
    f.write(
        '"""极简待办，只有增和列。"""\n'
        "todos = []\n\n"
        "def add_todo(title):\n"
        "    todos.append(title)\n\n"
        "def list_todos():\n"
        "    return list(todos)\n"
    )
with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as f:
    f.write("# todo\n一个演示用的极简待办小工具。\n")

print("临时项目目录：", work)

# ── 轨迹记录：包一层 check_permission ──
trace = []
_orig_check = ma.check_permission
def traced_check(tool, args, approver, cwd=".", trusted_domains=None, plan_state=None):
    d = _orig_check(tool, args, approver, cwd=cwd,
                    trusted_domains=trusted_domains, plan_state=plan_state)
    trace.append((tool, d[0], plan_state.mode if plan_state else "?",
                  str(args)[:60].replace("\n", " ")))
    return d
ma.check_permission = traced_check

# ── 安全自动审批人 ──
def auto_approver(tool, args):
    if tool == "exit_plan_mode":
        return True  # 模拟用户审阅后签字
    if tool in ("edit_file", "write_file"):
        p = os.path.abspath(os.path.join(work, args.get("path", "")))
        return p.startswith(os.path.abspath(work) + os.sep)
    # run_shell / 读敏感文件 / 其他：一律不批
    return False

state = PlanModeState(base_dir=work)

task = ("这个 todo.py 现在只能添加和列出待办。我需要能把某条待办标记为完成，"
        "并且能只看待完成的、或只看已完成的。帮我把这个功能做出来。")

print("任务：", task, "\n" + "=" * 60)

try:
    answer = ma.run_agent(task, cwd=work, approver=auto_approver, plan_state=state)
except Exception:
    print("\n[联调过程中抛异常]")
    traceback.print_exc()
    answer = "(异常)"

print("=" * 60)
print("\n【工具调用轨迹】（工具 / 权限决策 / 当时模式 / 参数摘要）")
for i, (tool, decision, mode, arg) in enumerate(trace, 1):
    print(f"{i:2d}. [{mode:7s}] {tool:18s} -> {decision:5s}  {arg}")

seq = [t[0] for t in trace]
print("\n【关键判定】")
print("是否调用 enter_plan_mode :", "enter_plan_mode" in seq)
enter_i = seq.index("enter_plan_mode") if "enter_plan_mode" in seq else -1
writeplan_i = seq.index("write_plan_file") if "write_plan_file" in seq else -1
exit_i = seq.index("exit_plan_mode") if "exit_plan_mode" in seq else -1
edits = [i for i, n in enumerate(seq) if n in ("edit_file", "write_file")]
print(f"次序  enter={enter_i}  write_plan={writeplan_i}  exit={exit_i}  写操作位置={edits}")
ordered = -1 < enter_i < writeplan_i < exit_i
plan_blocks_edit = all(i > exit_i for i in edits) if edits else False
print("enter→写计划→exit 顺序正确 :", ordered)
print("所有业务写操作都在签字之后 :", plan_blocks_edit)

print("\n【计划工件】")
if os.path.exists(state.plan_path):
    print(open(state.plan_path, encoding="utf-8").read())
else:
    print("（未生成计划文件）")

print("【最终 todo.py】")
print(open(os.path.join(work, "todo.py"), encoding="utf-8").read())
print("\n最终模式:", state.mode)
