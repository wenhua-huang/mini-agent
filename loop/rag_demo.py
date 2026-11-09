"""
rag_demo.py — 最小可运行 RAG（检索增强生成），2026-11-02
为面试「埃夫特 AI 平台后端」JD 的 RAG/Embedding/Vector Search 要求补的端到端动手实践。

一条链路讲清 RAG 在干什么：
  文档 → 切块(chunk) → embedding 向量化 → 存进“向量库”(这里用内存 list)
  用户提问 → 问题向量化 → 余弦相似度取 Top-K → 只把最相关片段拼进 prompt → 模型作答

为什么需要 RAG：模型没读过你的私有文档、且知识会过时；RAG 让回答“ grounded ”在检索到的
真实材料上，比把所有文档塞进上下文省 token，也比让模型瞎编（幻觉）可靠。

零额外依赖：embedding 与生成走百炼 OpenAI 兼容接口；余弦相似度用标准库实现（文档少，无需 numpy/FAISS）。
运行：../.venv/bin/python rag_demo.py
"""
import math
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent / ".env")
client = OpenAI(api_key=os.getenv("DASHSCOPE_API_KEY") or "offline-no-network",
                base_url=os.getenv("DASHSCOPE_BASE_URL") or "http://offline.invalid")

EMBED_MODEL = "text-embedding-v3"   # 百炼文本向量模型
CHAT_MODEL = "qwen-plus"
TOP_K = 2

# ── 1. 私有知识库（真实生产里这些来自文档/表格/网页，这里用 mini-agent 知识当语料）──
DOCS = [
    {"id": "sandbox-1",
     "text": "mini-agent 在 macOS 上用 Seatbelt(sandbox-exec) 做进程级能力隔离：默认断网、"
             "仅工作目录和临时目录可写、敏感路径如 ~/.ssh 在内核层禁读，沙箱不可用时 fail-closed 绝不裸跑。"},
    {"id": "sandbox-2",
     "text": "沙箱网络隔离用本地回环 HTTP 服务做正反对照验证：无沙箱访问返回 200，deny network 沙箱内"
             "返回 000 连不上，显式放行网络后恢复 200。连接一个本来就没服务的端口不能证明隔离有效。"},
    {"id": "multiagent-1",
     "text": "多 Agent 的核心价值是上下文隔离而非更聪明：每个子 Agent 有独立上下文和预算，"
             "读十几个网页只回传简短结论，原始噪声不污染主上下文，也限制提示注入的爆炸半径。"},
    {"id": "multiagent-2",
     "text": "mini-agent 用 ThreadPoolExecutor 并行跑多个只读工人(fan-out/fan-in)：3 个工人并行总延迟"
             "0.41 秒，串行需 1.2 秒；结果按提交顺序归位，单个工人失败只标记 error 不拖垮整批。"},
    {"id": "compaction-1",
     "text": "上下文压缩有 truncate、summarize、structured 三种策略，按 token 阈值自动触发；"
             "实测 10 条消息压成 8 条，token 从 283 降到 96。压缩摘要只能当线索，不能产生新授权。"},
    {"id": "rag-1",
     "text": "RAG 的标准链路是：文档切块、embedding 向量化入库、问题向量化、向量相似度检索 Top-K、"
             "把检索片段拼进 prompt 让模型基于材料回答。embedding 模型如 text-embedding-v3，"
             "生产向量库可选 Milvus、Faiss、pgvector 等。"},
]


# ── 2. 向量化 ─────────────────────────────────────────
def embed(texts):
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


# ── 3. 建库（启动时把所有文档转向量；生产里是持久化的向量库，不必每次重建）──
def build_index(docs):
    vectors = embed([d["text"] for d in docs])
    for d, v in zip(docs, vectors):
        d["vec"] = v
    return docs


# ── 4. 检索 Top-K ─────────────────────────────────────
def retrieve(question, index, k=TOP_K):
    qv = embed([question])[0]
    scored = sorted(((cosine(qv, d["vec"]), d) for d in index),
                    key=lambda x: x[0], reverse=True)
    return scored[:k]


# ── 5. 增强生成：把检索到的片段塞进 prompt，再让模型作答 ──
def answer(question, index):
    hits = retrieve(question, index)
    context = "\n\n".join(f"[资料 {d['id']} | 相似度 {s:.3f}]\n{d['text']}" for s, d in hits)
    prompt = (
        "你是技术面试助手。只能依据下面【检索资料】回答，资料里没有就明确说“资料中没有”，不要编造。\n\n"
        f"【检索资料】\n{context}\n\n"
        f"【问题】{question}\n\n【回答】")
    resp = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    return resp.choices[0].message.content, hits


def main():
    print(f"建库：{len(DOCS)} 篇文档 → {EMBED_MODEL} 向量化 ...")
    index = build_index(DOCS)
    dim = len(index[0]["vec"])
    print(f"向量维度 = {dim}\n")

    questions = [
        "沙箱的网络隔离是怎么验证有效的？",      # 应命中 sandbox-2（200/000 对照）
        "为什么要用多 Agent，并行能快多少？",     # 命中 multiagent-1/2
        "公司明年的营收目标是多少？",            # 语料里没有 → 应明确说没有，不许编
    ]
    for q in questions:
        ans, hits = answer(q, index)
        print("=" * 70)
        print("问题：", q)
        print("检索到：", [(d["id"], round(s, 3)) for s, d in hits])
        print("回答：", ans)
        print()


if __name__ == "__main__":
    main()
