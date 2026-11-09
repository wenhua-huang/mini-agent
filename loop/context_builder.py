"""
context_builder.py — 上下文构建器（对应 Claude Code 第 12 课）

每次调用模型前，把「环境客观事实 + 项目家规」收集好拼进 system 消息：
  - 系统上下文：cwd、git 分支/状态/最近提交（客观事实）
  - 用户上下文：CLAUDE.md / AGENTS.md 内容 + 当前日期（项目家规）

三个手法对应源码：
  1. ThreadPoolExecutor 并行跑 git 命令  ≈ Promise.all（耗时取决于最慢一条）
  2. 截断 + 补救提示                     ≈ MAX_STATUS_CHARS 的 truncate 策略
  3. functools.lru_cache                 ≈ lodash memoize（会话内只算一次）
"""
import functools
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Optional

MAX_GIT_CHARS = 2000  # 对应源码常量，省 token


def _run(cmd: list[str], cwd: Path) -> str:
    try:
        out = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _truncate(text: str, limit: int, hint: str) -> str:
    """截断不是干切：告诉模型信息被切了、该怎么拿完整内容。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(已截断，超过 {limit} 字符。需要完整信息请用 run_shell 自己跑对应命令)\n{hint}"


@functools.lru_cache(maxsize=1)
def _git_status(cwd_str: str) -> Optional[str]:
    """会话内缓存（≈memoize）。lru_cache 要求参数可哈希，所以传字符串路径。"""
    cwd = Path(cwd_str)
    if not _run(["git", "rev-parse", "--is-inside-work-tree"], cwd):
        return None  # 不是 git 仓库

    # 并行跑 4 条命令，总耗时≈最慢的一条，而不是四条之和
    jobs = {
        "branch": ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        "status": ["git", "status", "--short"],
        "log": ["git", "log", "--oneline", "-n", "5"],
        "user": ["git", "config", "user.name"],
    }
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = dict(zip(jobs, pool.map(lambda kv: _run(kv[1], cwd), jobs.items())))

    return "\n".join([
        f"当前分支: {results['branch']}",
        f"Git 用户: {results['user']}" if results["user"] else "",
        f"工作区状态:\n{_truncate(results['status'] or '(干净)', MAX_GIT_CHARS, '')}",
        f"最近 5 条提交:\n{results['log'] or '(无)'}",
        "（这是对话开始时的 git 快照，对话中不会自动更新）",
    ]).strip()


@functools.lru_cache(maxsize=8)
def _project_rules(cwd_str: str) -> Optional[str]:
    """找项目家规文件：CLAUDE.md 或 AGENTS.md，找到哪个用哪个。"""
    cwd = Path(cwd_str)
    for name in ("CLAUDE.md", "AGENTS.md"):
        f = cwd / name
        if f.exists():
            return f"## {name}（项目给 AI 的家规）\n{f.read_text(encoding='utf-8', errors='replace')[:4000]}"
    return None


def build_context_block(cwd: "str | Path" = ".") -> str:
    """组装成一段可直接拼到 system prompt 后面的上下文文本。"""
    cwd = Path(cwd).resolve()
    parts = [f"当前工作目录: {cwd}", f"今天日期: {date.today().isoformat()}"]

    git = _git_status(str(cwd))
    if git:
        parts.append("## Git 状态（系统上下文）\n" + git)

    rules = _project_rules(str(cwd))
    if rules:
        parts.append(rules)

    return "\n\n".join(parts)


if __name__ == "__main__":
    # 直接跑看效果：默认拿 claude-code-learning 这个 git 仓库做试验
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "../claude-code-learning"
    print(build_context_block(target))
