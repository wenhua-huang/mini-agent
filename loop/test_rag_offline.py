"""离线测 RAG 的纯本地逻辑（不调用任何 API）：余弦相似度 + Top-K 检索排序。
向量是注入的假向量，embed 被 monkeypatch 掉。"""
import rag_demo as rag

PASS = 0
def check(name, cond):
    global PASS
    assert cond, f"失败: {name}"
    PASS += 1
    print("✓", name)

# 1) 余弦：同向=1，正交=0，反向=-1，自比=1
TOL = 1e-7  # 与 cosine 的 1e-9 平滑项匹配
check("同向向量余弦≈1", abs(rag.cosine([1, 2, 3], [2, 4, 6]) - 1.0) < TOL)
check("正交向量余弦≈0", abs(rag.cosine([1, 0], [0, 1])) < TOL)
check("反向向量余弦≈-1", abs(rag.cosine([1, 0], [-1, 0]) + 1.0) < TOL)
check("自比≈1", abs(rag.cosine([0.3, -0.7], [0.3, -0.7]) - 1.0) < TOL)
check("零向量不除零", rag.cosine([0, 0], [1, 1]) == 0.0)

# 2) Top-K 检索：用假向量，问题只与 docs[1] 同向 → 必须排第一
docs = [
    {"id": "a", "text": "无关内容A", "vec": [1, 0, 0]},
    {"id": "b", "text": "命中内容B", "vec": [0, 1, 0]},
    {"id": "c", "text": "无关内容C", "vec": [0, 0, 1]},
]
orig_embed = rag.embed
rag.embed = lambda texts: [[0, 1, 0]] if texts == ["命中"] else [[0, 0, 0]]
try:
    hits = rag.retrieve("命中", docs, k=2)
finally:
    rag.embed = orig_embed
ids = [d["id"] for _, d in hits]
check("Top-K 返回 k 条", len(hits) == 2)
check("最相关片段排第一", ids[0] == "b")
check("命中相似度≈1", abs(hits[0][0] - 1.0) < TOL)

# 3) build_index 给每篇挂上向量（embed 打桩，验证逐篇赋值与顺序）
raw = [{"id": "x", "text": "t1"}, {"id": "y", "text": "t2"}]
rag.embed = lambda texts: [[0.1, 0.2] for _ in texts]
idx = rag.build_index(raw)
rag.embed = orig_embed
check("建库后每篇都有向量", all("vec" in d for d in idx))
check("建库保持文档顺序", [d["id"] for d in idx] == ["x", "y"])

print(f"\n[RAG 离线测试通过] {PASS} 条断言（全程未联网、未耗 token）")
