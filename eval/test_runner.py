"""test_runner.py — 回放评测 runner 的离线机制测试（不烧真 token、不联网）。

设计要点：runner 只依赖一个 agent_fn(task, cwd) 回调。
  - 离线这里传「假 agent」：直接改文件的函数 / 什么都不干的函数 / 不稳定函数；
  - 真跑时再传包了 mini_agent.run_agent 的 live 回调（见 run_live_eval.py）。
判分一律走 kata 自带的 judge.py 子进程，退出码 0=通过。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import runner  # noqa: E402

_passed = 0
_failed = 0


def expect(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print("PASS:", msg)
    else:
        _failed += 1
        print("FAIL:", msg)


EVAL_DIR = HERE
VENV_PY = sys.executable  # 判分子进程用同一个解释器


# ── 1. 发现 kata ──────────────────────────────────────────────
katas = runner.discover_katas(EVAL_DIR)
expect(len(katas) >= 1, "discover 至少发现一个 kata")
todo_kata = next((k for k in katas if k.name == "todo_mark_done"), None)
expect(todo_kata is not None, "发现 todo_mark_done kata")
expect(os.path.isfile(todo_kata.issue_path), "kata 带 issue.md")
expect(os.path.isdir(todo_kata.setup_dir), "kata 带 setup/ 缺陷项目目录")
expect(os.path.isfile(todo_kata.judge_path), "kata 带 judge.py 判分脚本")


# ── 2. 干净副本隔离：改副本不污染 setup，两次副本互不可见 ───────
work = tempfile.mkdtemp(prefix="eval_test_")
try:
    copy_a = runner.fresh_copy(todo_kata, os.path.join(work, "a"))
    copy_b = runner.fresh_copy(todo_kata, os.path.join(work, "b"))
    expect(copy_a != copy_b and os.path.isdir(copy_a) and os.path.isdir(copy_b),
           "fresh_copy 每次生成独立副本目录")

    # 在副本 A 里制造改动（追加一行），setup 原件和副本 B 都不应受影响
    target_a = os.path.join(copy_a, "todo.py")
    with open(target_a, "a", encoding="utf-8") as f:
        f.write("\n# contaminated by copy A\n")
    with open(target_a, encoding="utf-8") as f:
        a_text = f.read()
    with open(os.path.join(todo_kata.setup_dir, "todo.py"), encoding="utf-8") as f:
        setup_text = f.read()
    with open(os.path.join(copy_b, "todo.py"), encoding="utf-8") as f:
        b_text = f.read()
    expect("contaminated by copy A" in a_text, "副本 A 的改动确实写入了 A")
    expect("contaminated by copy A" not in setup_text, "副本改动不污染 setup 原件（回放可重复的前提）")
    expect("contaminated by copy A" not in b_text, "副本 A 的改动对副本 B 不可见（试验间隔离）")

    # ── 3. 判分：什么都不干的 agent 必须判红 ───────────────────
    def idle_agent(task, cwd):
        return "我懒得改"  # 只动口，不落盘

    idle_result = runner.run_once(todo_kata, idle_agent, work_root=work, python=VENV_PY)
    expect(idle_result.passed is False, "空操作 agent 被判失败（judge 红）")
    expect(idle_result.error is not None or idle_result.judge_output, "失败结果带判分依据，不是黑箱")

    # ── 4. 判分：正确修复的假 agent 必须判绿 ───────────────────
    def good_agent(task, cwd):
        path = os.path.join(cwd, "todo.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        src += (
            "\n    def mark_done(self, item_id):\n"
            "        for item in self.items:\n"
            "            if item['id'] == item_id:\n"
            "                item['done'] = True\n"
            "                self._save()\n"
            "                return\n"
            "        raise ValueError(item_id)\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        return "已补齐 mark_done"

    good_result = runner.run_once(todo_kata, good_agent, work_root=work, python=VENV_PY)
    expect(good_result.passed is True, "正确修复的 agent 被判通过（judge 绿）")

    # ── 5. 重复回放 + pass@1 聚合：不稳定 agent 的成败计数 ──────
    calls = {"n": 0}

    def flaky_agent(task, cwd):
        # 前两次失败（空操作），第三次起正确修复——模拟模型随机性
        calls["n"] += 1
        if calls["n"] <= 2:
            return "这次没改对"
        return good_agent(task, cwd)

    rep = runner.run_repeated(todo_kata, flaky_agent, times=5,
                              work_root=work, python=VENV_PY)
    expect(rep.total == 5, "run_repeated 跑满指定次数")
    expect(rep.passed == 3 and rep.failed == 2, "成败计数正确（3 过 2 挂）")
    expect(abs(rep.pass_at_1 - 0.6) < 1e-9, "pass@1 = 通过次数/总次数 = 0.6")
    expect(len(rep.trials) == 5, "每次试验都留了明细记录")

    # ── 6. 汇总结构：多 kata 时能出一张总表 ────────────────────
    summary = runner.summarize([rep])
    expect(summary["total_trials"] == 5, "汇总含总试验数")
    expect(summary["total_passed"] == 3, "汇总含总通过数")
    expect(abs(summary["overall_pass_at_1"] - 0.6) < 1e-9, "汇总含整体 pass@1")
    expect("per_kata" in summary and len(summary["per_kata"]) == 1, "汇总按 kata 分列")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\n%d 通过 / %d 失败" % (_passed, _failed))
sys.exit(0 if _failed == 0 else 1)
