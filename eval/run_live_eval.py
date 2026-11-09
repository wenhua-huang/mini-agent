"""run_live_eval.py — 用真 qwen-plus 跑回放评测（会真实消耗 token，需 loop/.env）。

与离线 test_runner.py 的区别：
  - 离线：假 agent，测 runner 机制，零 token、可进回归；
  - 本脚本：把 mini_agent.run_agent 包成 runner 要的 agent_fn，真模型动手改副本。

安全护栏：
  - 每个试验在独立临时副本里进行，setup 原件不被碰；
  - 评测 approver 只在「该副本目录内」放行读写工具，shell 交回权限层只读白名单；
  - 每次试验单独落一份 trace.jsonl，结果汇总写 eval/results/，便于跨版本对比。

用法：
  ../.venv/bin/python run_live_eval.py                 # 每个 kata 跑 3 次
  ../.venv/bin/python run_live_eval.py --times 5 --kata todo_mark_done
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOOP_DIR = os.path.join(os.path.dirname(HERE), "loop")
sys.path.insert(0, LOOP_DIR)
sys.path.insert(0, HERE)

import runner  # noqa: E402

# 仅在真正运行 live 时才 import，避免离线测试因缺 key 而失败
import mini_agent as ma  # noqa: E402


# 评测专用审批：隔离副本内放行读写，shell 不自动批（只读白名单仍由 permission 层决定）
_EVAL_WRITABLE = {"read_file", "search_in_file", "edit_file", "write_file"}


def eval_approver(tool, args):
    return tool in _EVAL_WRITABLE


def make_live_agent():
    """返回符合 runner 协议的 agent_fn(task, cwd) -> answer。"""
    def agent_fn(task, cwd):
        trace_path = os.path.join(cwd, "trace.jsonl")
        return ma.run_agent(
            task,
            cwd=cwd,
            approver=eval_approver,
            trace_path=trace_path,
        )
    return agent_fn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--times", type=int, default=3, help="每个 kata 重复次数（默认 3）")
    ap.add_argument("--kata", default="", help="只跑指定 kata，缺省跑全部")
    args = ap.parse_args()

    # 评测任务通常需要多轮读写，适当放宽但仍有硬上限，防失控
    ma.MAX_TURNS = 10
    ma.MAX_TOTAL_TOKENS = 40000

    katas = runner.discover_katas(HERE)
    if args.kata:
        katas = [k for k in katas if k.name == args.kata]
    if not katas:
        print("没有匹配的 kata")
        sys.exit(2)

    agent_fn = make_live_agent()
    work_root = os.path.join(HERE, ".eval_work")
    reports = []
    for kata in katas:
        print("\n" + "=" * 64)
        print("kata:", kata.name, " 重复", args.times, "次")
        t0 = time.time()
        rep = runner.run_repeated(kata, agent_fn, times=args.times,
                                  work_root=work_root, python=sys.executable)
        rep_elapsed = round(time.time() - t0, 1)
        reports.append(rep)
        for i, t in enumerate(rep.trials, 1):
            tag = "PASS" if t.passed else "FAIL"
            extra = (" | 异常: " + t.error) if t.error else ""
            print("  试验%d %s%s" % (i, tag, extra))
        print("  => pass@1 = %.2f（%d/%d）用时 %ss"
              % (rep.pass_at_1, rep.passed, rep.total, rep_elapsed))

    summary = runner.summarize(reports)
    print("\n" + "=" * 64)
    print("汇总：")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # 落盘，文件名带时间戳，方便跨版本/跨策略对比（这正是「可量化回归」的产物）
    results_dir = os.path.join(HERE, "results")
    os.makedirs(results_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(results_dir, "eval_%s.json" % stamp)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": stamp,
            "model": ma.MODEL,
            "times_per_kata": args.times,
            "summary": summary,
            "trials": [
                {
                    "kata": r.kata,
                    "passed": t.passed,
                    "error": t.error,
                    "judge_output": t.judge_output,
                    "copy_dir": t.copy_dir,
                }
                for r in reports for t in r.trials
            ],
        }, f, ensure_ascii=False, indent=2)
    print("\n结果已落盘：", out)


if __name__ == "__main__":
    main()
