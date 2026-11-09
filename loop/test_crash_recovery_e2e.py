"""
test_crash_recovery_e2e.py — 第 3 步：真进程崩溃端到端测试。

与前两步的本质区别：崩溃不再是 raise 异常（内存还在同进程里），
而是 demo_worker.py 子进程 os._exit(1) 真死——句柄不 flush、atexit 不跑、
内存全丢，只有 SQLite 已 commit 的数据和网关日志留在磁盘。
再由第二个独立进程接管同一任务。

验证：
  A. AFTER_TOOL 崩溃：网关日志 1 行 → 重启接管 → 日志仍 1 行（幂等跨进程生效）
  B. BEFORE_TOOL 崩溃：网关日志 0 行 → 重启接管 → 日志 1 行（无坑则真实执行一次）
  C. 两次崩溃恢复后任务都 SUCCEEDED，事件流配对完整、fencing 代次递增
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import runtime as rt
PY = os.path.abspath(os.path.join(HERE, "..", ".venv", "bin", "python"))
WORKER = os.path.join(HERE, "demo_worker.py")

PASS, FAIL = 0, 0
def expect(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1; print("✓", msg)
    else:
        FAIL += 1; print("✗", msg)


def run_cli(workdir, db, *cli_args):
    """起独立进程跑 worker CLI，返回 (returncode, stdout)。"""
    proc = subprocess.run(
        [PY, WORKER, "--db", db, *cli_args],
        cwd=workdir, capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout.strip()


def count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return len([l for l in f if l.strip()])


def scenario(name, crash_point, sms_after_crash, sms_after_recovery):
    print(f"\n──── 场景 {name}：崩溃夹缝 {crash_point} ────")
    workdir = tempfile.mkdtemp(prefix=f"e2e_{name}_")
    db = os.path.join(workdir, "tasks.db")
    sms_log = os.path.join(workdir, "sms_gateway.log")
    stock_log = os.path.join(workdir, "stock_query.log")

    # 1) 独立进程提交
    code, out = run_cli(workdir, db, "submit", "--input", "发短信并查库存")
    task_id = out
    expect(code == 0 and len(task_id) == 32, f"[{name}] submit 成功，task_id={task_id[:8]}…")

    # 2) 独立进程 worker-1 开跑并真死
    code, out = run_cli(workdir, db, "run", "--worker", "worker-1",
                        "--crash", f"call-0:send_sms:{crash_point}")
    expect(code == 1 and "SIMULATED CRASH" in out,
           f"[{name}] worker-1 进程以退出码 1 真死（os._exit），输出：{out[:80]}")
    expect(count_lines(sms_log) == sms_after_crash,
           f"[{name}] 崩溃现场短信网关行数={count_lines(sms_log)}（预期 {sms_after_crash}）")

    # 3) 独立进程视角确认：任务卡在 RUNNING、租约还挂在死了的 worker-1 上
    code, status_out = run_cli(workdir, db, "status", "--task-id", task_id)
    expect('"status": "RUNNING"' in status_out and "worker-1" in status_out,
           f"[{name}] 崩溃后任务仍 RUNNING 且 owner=worker-1（僵尸租约）")

    # 4) 租约过期（演示加速；真实系统等心跳超时 30s 自然发生）
    code, _ = run_cli(workdir, db, "expire", "--task-id", task_id)
    expect(code == 0, f"[{name}] 租约置过期")

    # 5) 全新进程 worker-2 接管续跑
    code, out = run_cli(workdir, db, "run", "--worker", "worker-2")
    expect(code == 0 and "outcome=succeeded" in out,
           f"[{name}] worker-2 新进程接管并跑到 succeeded：{out}")
    expect(count_lines(sms_log) == sms_after_recovery,
           f"[{name}] ★ 恢复后短信网关总行数={count_lines(sms_log)}（预期 {sms_after_recovery}）")
    expect(count_lines(stock_log) == 1,
           f"[{name}] 库存查询在恢复后真实执行 1 次（{count_lines(stock_log)} 行）")

    # 6) 终态与事件流审计
    code, status_out = run_cli(workdir, db, "status", "--task-id", task_id)
    expect('"status": "SUCCEEDED"' in status_out, f"[{name}] 终态 SUCCEEDED")
    expect('"version": 3' in status_out,
           f"[{name}] fencing version=3（领取 2、接管 3，两代 worker 有据可查）")
    seq_lines = [l for l in status_out.splitlines() if l.strip().startswith("seq=")]
    types = [l.split()[1] for l in seq_lines]
    # 物理事件流是 append-only 审计账：崩溃前的孤儿 tool_call 保留取证，共 7 条
    expect(types == ["user", "tool_call", "tool_call", "tool_result",
                     "tool_call", "tool_result", "assistant"],
           f"[{name}] 物理事件流保留崩溃前孤儿 tool_call（不可变审计）：{types}")
    # 逻辑视图经 rebuild 截掉孤儿后，配对完整、可直接喂模型，共 6 条
    from recoverable_loop import rebuild_messages
    audit_conn = rt.connect(db)
    logical = rebuild_messages(rt.replay_events(audit_conn, task_id))
    audit_conn.close()
    logical_roles = [m["role"] for m in logical]
    expect(logical_roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"],
           f"[{name}] rebuild 逻辑视图配对完整：{logical_roles}")
    # 若有重放，tool_result 必须带 replayed 标记
    if crash_point == "AFTER_TOOL":
        expect('"replayed": true' in status_out,
               f"[{name}] 第一步结果标记 replayed=true（走缓存，未真实重发）")


scenario("A-AND", "AFTER_TOOL", sms_after_crash=1, sms_after_recovery=1)
scenario("B-BEFORE", "BEFORE_TOOL", sms_after_crash=0, sms_after_recovery=1)

print(f"\n{'='*55}\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
