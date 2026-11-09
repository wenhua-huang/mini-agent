"""runner.py — 微缩 issue 回放评测的离线引擎（不绑定具体 Agent，不联网）。

核心抽象：agent_fn(task: str, cwd: str) -> str
  离线测试传假 agent；真跑时由 run_live_eval.py 包一层 mini_agent.run_agent。
判分只认 kata 自带 judge.py 的子进程退出码：0=通过，其余=失败。
每次试验都在独立的干净副本里进行，setup 原件永不被改动——这是「可重复回放」的地基。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Kata:
    name: str
    root: str
    issue_path: str
    setup_dir: str
    judge_path: str

    def read_issue(self) -> str:
        with open(self.issue_path, encoding="utf-8") as f:
            return f.read()


def discover_katas(eval_dir: str):
    """扫描 eval_dir/katas/*，凡同时含 issue.md、setup/、judge.py 的目录即为一个 kata。"""
    katas_root = os.path.join(eval_dir, "katas")
    found = []
    if not os.path.isdir(katas_root):
        return found
    for name in sorted(os.listdir(katas_root)):
        root = os.path.join(katas_root, name)
        if not os.path.isdir(root):
            continue
        issue_path = os.path.join(root, "issue.md")
        setup_dir = os.path.join(root, "setup")
        judge_path = os.path.join(root, "judge.py")
        if os.path.isfile(issue_path) and os.path.isdir(setup_dir) and os.path.isfile(judge_path):
            found.append(Kata(name, root, issue_path, setup_dir, judge_path))
    return found


def fresh_copy(kata: Kata, dest: str) -> str:
    """把 setup 内容复制到全新目录 dest，并放入 judge.py。返回 dest。

    用 copytree（目标必须不存在），保证副本不带任何上一次试验的残留。
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(kata.setup_dir, dest)
    shutil.copy2(kata.judge_path, os.path.join(dest, "judge.py"))
    return dest


@dataclass
class TrialResult:
    passed: bool
    judge_output: str = ""
    error: str = ""          # agent_fn 自身抛异常时的文本，正常为空
    answer: str = ""         # agent 的最终口述（仅留档，不参与判分）
    copy_dir: str = ""


def run_once(kata: Kata, agent_fn, work_root: str = None,
             python: str = sys.executable) -> TrialResult:
    """跑一次试验：干净副本 → agent 动手 → judge 子进程客观判分。"""
    work_root = work_root or tempfile.gettempdir()
    os.makedirs(work_root, exist_ok=True)
    trial_dir = os.path.join(work_root, "trial_" + uuid.uuid4().hex[:12])
    copy_dir = fresh_copy(kata, trial_dir)

    answer = ""
    agent_error = ""
    try:
        answer = agent_fn(kata.read_issue(), copy_dir)
    except Exception as exc:  # agent 崩了算这次试验失败，但 runner 不崩，继续判分/记录
        agent_error = "%s: %s" % (type(exc).__name__, exc)

    # 判分：在副本目录里跑 judge.py，只看退出码
    proc = subprocess.run(
        [python, "judge.py"],
        cwd=copy_dir,
        capture_output=True,
        text=True,
        timeout=120,
    )
    judge_output = (proc.stdout or "") + (proc.stderr or "")
    return TrialResult(
        passed=(proc.returncode == 0),
        judge_output=judge_output.strip(),
        error=agent_error,
        answer=str(answer),
        copy_dir=copy_dir,
    )


@dataclass
class RepeatedReport:
    kata: str
    total: int
    passed: int
    failed: int
    pass_at_1: float
    trials: list = field(default_factory=list)


def run_repeated(kata: Kata, agent_fn, times: int = 5,
                 work_root: str = None, python: str = sys.executable) -> RepeatedReport:
    """同一 kata 用同一 agent 重复 times 次，统计一次成功率 pass@1。"""
    trials = []
    for _ in range(times):
        trials.append(run_once(kata, agent_fn, work_root=work_root, python=python))
    passed = sum(1 for t in trials if t.passed)
    total = len(trials)
    return RepeatedReport(
        kata=kata.name,
        total=total,
        passed=passed,
        failed=total - passed,
        pass_at_1=(passed / total if total else 0.0),
        trials=trials,
    )


def summarize(reports) -> dict:
    """把多个 kata 的重复报告聚合成一张总表。"""
    total_trials = sum(r.total for r in reports)
    total_passed = sum(r.passed for r in reports)
    return {
        "total_trials": total_trials,
        "total_passed": total_passed,
        "total_failed": total_trials - total_passed,
        "overall_pass_at_1": (total_passed / total_trials if total_trials else 0.0),
        "per_kata": [
            {
                "kata": r.kata,
                "total": r.total,
                "passed": r.passed,
                "failed": r.failed,
                "pass_at_1": r.pass_at_1,
            }
            for r in reports
        ],
    }
