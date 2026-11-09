"""极简待办：支持添加和列出，持久化到 todos.json。

注意：这是一个「有缺陷」的起点——目前缺少「标记完成」能力，
评测时 Agent 需要补齐它（见 issue.md）。
"""
import json
import os


class TodoStore:
    def __init__(self, path: str = "todos.json"):
        self.path = path
        self.items = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.items = json.load(f)

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.items, f, ensure_ascii=False)

    def add(self, title: str) -> dict:
        item = {"id": len(self.items) + 1, "title": title, "done": False}
        self.items.append(item)
        self._save()
        return item

    def list(self) -> list:
        return self.items
