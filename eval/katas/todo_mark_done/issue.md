# 任务：给待办加上「标记完成」能力

这个小项目 `todo.py` 现在只能 `add` 添加和 `list` 列出待办，数据持久化在 JSON 文件里。
请在 `TodoStore` 类中补齐一个方法：

- 方法名 `mark_done(self, item_id)`：
  1. 把对应 id 待办的 `done` 字段置为 `True`；
  2. 调用已有的 `_save()` 把变更落盘，保证重新 new 一个 `TodoStore` 加载同一文件后，该待办仍然是完成状态；
  3. 如果传入的 `item_id` 不存在，抛出 `ValueError`。

约束：
- 不要改动 `add` / `list` / `_save` / `__init__` 已有的行为；
- 直接修改 `todo.py` 并真正落盘，不要只在回答里贴代码。
