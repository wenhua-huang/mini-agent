"""
test_context_tools.py — 上下文工程实战测试：50 万行大日志永不整份进窗口

验证：
  1. search_in_file 在 50 万行里只回行号+命中行（受 max_matches 限制），体积可控
  2. read_file(offset, limit) 只回指定窗口，带行号
  3. 典型链路：先搜 ERROR 定位 → 再按行号精读上下文
  4. 命中数超上限时只回前 N 条并提示收窄，不把洪水灌进上下文
"""
import os
import tempfile

import mini_agent as ma

LOG = os.path.join(tempfile.gettempdir(), "huge_app.log")

# ── 造一个 50 万行的假日志，在已知行埋一个唯一错误 ─────────────────
N = 500_000
BUG_LINE = 412_333
with open(LOG, "w", encoding="utf-8") as f:
    for i in range(1, N + 1):
        if i == BUG_LINE:
            f.write(f"{i}: 2026-10-31 ERROR Database deadlock detected txn=8821\n")
        elif i % 1000 == 0:
            f.write(f"{i}: 2026-10-31 WARN slow query 300ms\n")
        else:
            f.write(f"{i}: 2026-10-31 INFO request handled ok\n")

size_mb = os.path.getsize(LOG) / 1024 / 1024
print(f"[准备] 假日志 {N} 行，{size_mb:.1f} MB")


def expect(cond, msg):
    assert cond, msg
    print("✓", msg)


# ── 1. 搜索唯一错误：只回 1 条，且带正确行号，返回体极小 ───────────
res = ma.search_in_file(LOG, "Database deadlock", cwd="/tmp")
expect(f"命中 1 处" in res, "唯一错误被定位")
expect(str(BUG_LINE) in res, f"返回里包含正确行号 {BUG_LINE}")
expect("ERROR" in res, "返回命中行内容")
expect(len(res) < 2000, f"搜索结果体积极小（{len(res)} 字符），不是整文件")

# ── 2. 常见词 WARN 命中 500 条：默认只回前 50 条并提示收窄 ─────────
res = ma.search_in_file(LOG, "WARN", cwd="/tmp")
expect("命中 500 处" in res, "统计出全部命中数（500）")
expect("仅展示前 50 处" in res, "超过上限只展示前 50 条，防止淹没上下文")
expect(res.count("--") <= 50, "分隔符数量不超过上限")

# ── 3. 用行号精读错误前后窗口：只读 11 行 ─────────────────────────
window = ma.read_file(LOG, offset=BUG_LINE - 5, limit=11, cwd="/tmp")
lines = [l for l in window.splitlines() if not l.startswith("...(")]
expect(len(lines) == 11, f"精读窗口恰好 11 行，实际 {len(lines)}")
expect(lines[0].startswith(f"{BUG_LINE-5}|"), "窗口起始行号正确")
expect(any("deadlock" in l for l in lines), "窗口内包含目标错误行")
expect(len(window) < 2000, f"精读窗口体积 {len(window)} 字符，远小于整文件")

# ── 4. 无匹配与坏正则不崩 ─────────────────────────────────────────
expect("未匹配" in ma.search_in_file(LOG, "ZZZ_NOT_EXIST_ZZZ", cwd="/tmp"),
       "无匹配给出明确反馈")
expect("非法正则" in ma.search_in_file(LOG, "([unclosed", cwd="/tmp"),
       "非法正则被捕获为错误文本，不炸循环")

# ── 5. read_file 默认只读前 200 行，且有截断提示引导定位 ───────────
head = ma.read_file(LOG, cwd="/tmp")
expect(head.count("\n") < 202 and "1|" in head, "默认窗口约 200 行，不整读")

# ── 对比：整文件有多大 vs 进上下文的有多大 ─────────────────────────
print(f"\n[对比] 文件 {size_mb:.1f} MB；"
      f"搜索回 {len(ma.search_in_file(LOG, 'Database deadlock', cwd='/tmp'))} 字符；"
      f"精读窗口 {len(window)} 字符。进上下文的只有定位到的小片段。")
os.remove(LOG)
print("\n全部通过")
