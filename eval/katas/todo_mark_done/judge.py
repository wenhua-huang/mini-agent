"""judge.py — todo_mark_done 的客观判分脚本（不依赖 pytest）。

在被测副本目录里以子进程运行：
    python judge.py
退出码 0 = Agent 真的修好了；非 0 = 未修好（assert 失败/缺方法/异常）。
判分只看世界终态（文件里的代码行为），不看 Agent 自己说了什么。
"""
import os
import sys

# 始终在「当前工作目录」加载被测的 todo.py，确保测的是副本里被 Agent 改过的版本
sys.path.insert(0, os.getcwd())

# 清掉可能的残留，保证判分从干净状态开始
if os.path.exists("judge_todos.json"):
    os.remove("judge_todos.json")

import todo  # noqa: E402

store = todo.TodoStore("judge_todos.json")
item = store.add("写评测回放集")

# 1. 方法必须存在（缺陷起点没有它）
assert hasattr(store, "mark_done"), "缺少 mark_done 方法"

# 2. 调用后内存状态正确
store.mark_done(item["id"])
assert store.items[0]["done"] is True, "mark_done 未把 done 置为 True"

# 3. 变更必须持久化：重新加载同一文件仍是完成态（FAIL_TO_PASS 的核心）
store2 = todo.TodoStore("judge_todos.json")
assert store2.list()[0]["done"] is True, "完成状态没有落盘持久化"

# 4. 非法 id 必须 fail-loud，而不是静默吞掉
raised = False
try:
    store.mark_done(999)
except ValueError:
    raised = True
assert raised, "item_id 不存在时应抛 ValueError"

# 5. 既有行为不能被改坏（PASS_TO_PASS：add/list 仍正常）
before = len(store2.list())
new_item = store2.add("第二个")
assert new_item["id"] == before + 1 and new_item["done"] is False, "add 的既有行为被破坏"

print("JUDGE PASS: mark_done 正确且持久化，非法 id 报错，既有功能无回归")
sys.exit(0)
