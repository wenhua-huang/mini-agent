"""
demo_worker.py — 第 3 步：独立进程的任务 worker（真实崩溃演示用）。

子命令：
  submit  提交任务，打印 task_id
  run     claim 一个任务并执行；--crash "<idem_key>:BEFORE_TOOL|AFTER_TOOL"
          命中崩溃夹缝时 os._exit(1) 真杀进程（不 flush、不跑 atexit，模拟宕机）
  expire  把某任务租约改到过去（演示加速：真实系统里等 30 秒心跳超时自然发生）
  status  打印任务状态与事件流

副作用外置到文件：sms_gateway.log / stock_query.log，
进程死亡后内存计数器归零也不影响观察——这就是「外部系统」的最小模型。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runtime as rt
from recoverable_loop import run_recoverable, SimulatedCrash, BEFORE_TOOL, AFTER_TOOL


def make_planner():
    """与 test_recoverable_loop 同一套脚本化决策：send_sms → query_stock → 终答。"""
    def planner(messages):
        done = sum(1 for m in messages if m["role"] == "tool")
        if done == 0:
            return {"type": "tool", "name": "send_sms", "args": {"phone": "138****0001"}}
        if done == 1:
            return {"type": "tool", "name": "query_stock", "args": {"sku": "A1"}}
        return {"type": "final", "content": "短信已发，库存 12 件，任务完成"}
    return planner


def make_tools(workdir):
    gateway = os.path.join(workdir, "sms_gateway.log")
    stock_log = os.path.join(workdir, "stock_query.log")

    def send_sms(args):
        # 真实副作用：append 一行到「网关」。重复调用会留下多行，无法悄悄抹掉
        with open(gateway, "a", encoding="utf-8") as f:
            f.write(f"SMS sent to {args['phone']} from pid={os.getpid()}\n")
        return {"sms_id": "SMS-1001", "status": "sent"}

    def query_stock(args):
        with open(stock_log, "a", encoding="utf-8") as f:
            f.write(f"stock query sku={args['sku']} from pid={os.getpid()}\n")
        return {"sku": args["sku"], "qty": 12}

    return {"send_sms": send_sms, "query_stock": query_stock}


def cmd_submit(conn, args):
    task_id = rt.submit_task(conn, args.input)
    print(task_id, flush=True)


def cmd_run(conn, args):
    task_id, version = rt.claim_task(conn, args.worker, lease_seconds=30)
    if task_id is None:
        print("NO_TASK", flush=True)
        sys.exit(2)
    print(f"claimed task={task_id} fencing_version={version} pid={os.getpid()}", flush=True)

    crash_points = {}
    if args.crash:
        idem_key, point = args.crash.rsplit(":", 1)  # 幂等键自身含冒号，按最后一个切
        assert point in (BEFORE_TOOL, AFTER_TOOL)
        crash_points[idem_key] = point

    try:
        outcome = run_recoverable(
            conn, task_id, args.worker, version,
            make_planner(), make_tools(os.path.dirname(args.db)),
            crash_points=crash_points,
        )
    except SimulatedCrash as exc:
        # 真杀进程：不走 finally/atexit，内存状态全丢，已 commit 的 SQLite 数据留在磁盘
        print(f"SIMULATED CRASH: {exc} → os._exit(1), pid={os.getpid()}", flush=True)
        sys.stdout.flush()
        os._exit(1)
    print(f"outcome={outcome} final_status={rt.get_task(conn, task_id)['status']}",
          flush=True)


def cmd_expire(conn, args):
    import time
    with conn:
        conn.execute("UPDATE tasks SET lease_expire_at=? WHERE task_id=?",
                     (time.time() - 1, args.task_id))
    print("lease expired", flush=True)


def cmd_status(conn, args):
    row = rt.get_task(conn, args.task_id)
    print(json.dumps({k: row[k] for k in
                      ("task_id", "status", "cancel_requested", "lease_owner", "version")},
                     ensure_ascii=False, indent=2), flush=True)
    for e in rt.replay_events(conn, args.task_id):
        print(f"  seq={e['seq']} {e['event_type']} {json.dumps(e['payload'], ensure_ascii=False)}",
              flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("submit")
    p.add_argument("--input", required=True)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("run")
    p.add_argument("--worker", required=True)
    p.add_argument("--crash", default="")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("expire")
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_expire)

    p = sub.add_parser("status")
    p.add_argument("--task-id", required=True)
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    conn = rt.connect(args.db)
    args.func(conn, args)


if __name__ == "__main__":
    main()
