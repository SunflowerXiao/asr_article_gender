"""
资料库管理模块
职责：
  - 任务2：管理原始 JSON 文件（双层存储的原始层）
  - 任务3：调用豆包 Embedding API，将文章向量化写入 ChromaDB
  - 任务4：按主题/风格做语义相似度检索，返回 Top-K 爆款案例
"""

import os
import json
import logging
import time
from datetime import datetime
from typing import Optional

import chromadb
from openai import OpenAI  # 豆包 SDK 兼容 OpenAI 接口

logger = logging.getLogger(__name__)

# ── 路径配置 ──────────────────────────────────────────────────────────────────

_BASE_DIR    = os.path.dirname(__file__)
RAW_DIR      = os.path.join(_BASE_DIR, "corpus", "raw")       # 原始 JSON 存放目录
DB_DIR       = os.path.join(_BASE_DIR, "corpus", "corpus_db") # ChromaDB 持久化目录
COLLECTION_NAME = "wechat_corpus"

# ── 豆包 Embedding 配置 ───────────────────────────────────────────────────────
# 复用现有 ARK_API_KEY，无需额外申请
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")

# 豆包 Embedding 模型 ID（支持中文，1024 维向量）
EMBEDDING_MODEL = "doubao-embedding-large-text-240915"

# 每批向量化的条数（避免单次请求过大）
EMBED_BATCH_SIZE = 20

# Embedding API 失败重试次数
EMBED_MAX_RETRY = 3

# ── ChromaDB 客户端（懒加载单例）──────────────────────────────────────────────
_chroma_client: Optional[chromadb.PersistentClient] = None
_collection: Optional[chromadb.Collection] = None
def _get_collection() -> chromadb.Collection:
    """获取 ChromaDB Collection（单例，按需初始化）"""
    global _chroma_client, _collection
    if _collection is None:
        os.makedirs(DB_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=DB_DIR)
        # get_or_create：首次自动建表，之后复用
        _collection = _chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},  # 余弦相似度
        )
        logger.info(f"[CORPUS] ChromaDB 已连接，collection='{COLLECTION_NAME}'，当前条数: {_collection.count()}")
    return _collection


def _get_embed_client() -> OpenAI:
    """获取豆包 Embedding API 客户端（兼容 OpenAI SDK）"""
    if not ARK_API_KEY:
        raise RuntimeError("ARK_API_KEY 未设置，无法调用 Embedding API")
    return OpenAI(
        api_key=ARK_API_KEY,
        base_url="https://ark.cn-beijing.volces.com/api/v3",
    )


# ── 原始 JSON 管理（存储层） ───────────────────────────────────────────────────

def save_raw(articles: list[dict], date_str: Optional[str] = None) -> str:
    """
    将爬取到的原始文章列表追加保存到按日期命名的 JSON 文件
    
    Returns:
        保存的文件路径
    """
    os.makedirs(RAW_DIR, exist_ok=True)
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    filepath = os.path.join(RAW_DIR, f"wechat_{date_str}.json")

    # 读取已有内容（追加模式，不覆盖同一天的旧数据）
    existing = []
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []

    # 去重：已存在相同 id 的不重复写入
    existing_ids = {item["id"] for item in existing}
    new_articles = [a for a in articles if a["id"] not in existing_ids]

    if new_articles:
        existing.extend(new_articles)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        logger.info(f"[CORPUS] 原始 JSON 已保存：新增 {len(new_articles)} 条 → {filepath}")
    else:
        logger.info(f"[CORPUS] 无新数据需要保存（全部已存在）")

    return filepath
  def load_raw_all() -> list[dict]:
    """读取 corpus/raw/ 下所有 JSON 文件，合并返回去重后的完整列表"""
    os.makedirs(RAW_DIR, exist_ok=True)
    all_articles = {}  # id → article，自动去重
    for fname in sorted(os.listdir(RAW_DIR)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(RAW_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                articles = json.load(f)
            for a in articles:
                if a.get("id"):
                    all_articles[a["id"]] = a
        except Exception as e:
            logger.warning(f"[CORPUS] 读取 {fname} 失败: {e}")
    return list(all_articles.values())


# ── Embedding API 调用（向量层） ───────────────────────────────────────────────

def _embed_texts(texts: list[str]) -> list[list[float]]:
    """
    批量调用豆包 Embedding API 将文本列表转换为向量列表
    内置重试机制，失败时最多重试 EMBED_MAX_RETRY 次
    """
    client = _get_embed_client()
    vectors = []

    for batch_start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[batch_start: batch_start + EMBED_BATCH_SIZE]
        for attempt in range(1, EMBED_MAX_RETRY + 1):
            try:
                resp = client.embeddings.create(
                    model=EMBEDDING_MODEL,
                    input=batch,
                )
                batch_vectors = [item.embedding for item in resp.data]
                vectors.extend(batch_vectors)
                logger.debug(f"[CORPUS] Embedding 批次 {batch_start//EMBED_BATCH_SIZE + 1} 成功，{len(batch)} 条")
                break
            except Exception as e:
                logger.warning(f"[CORPUS] Embedding 第 {attempt} 次失败: {e}")
                if attempt < EMBED_MAX_RETRY:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    logger.error(f"[CORPUS] Embedding 达到最大重试次数，跳过本批次")
                    # 用零向量占位，保证列表长度对齐（后续检索时会排在最末）
                    vectors.extend([[0.0] * 1024] * len(batch))

    return vectors


# ── 向量入库 ──────────────────────────────────────────────────────────────────

def index_articles(articles: list[dict]) -> dict:
    """
    将文章列表向量化并写入 ChromaDB
    自动跳过已存在的 id，实现增量入库

    Returns:
        {"added": int, "skipped": int}
    """
    if not articles:
        return {"added": 0, "skipped": 0}

    collection = _get_collection()

    # ── 去重：过滤掉已在 ChromaDB 中的 id ─────────────────────────────────
    all_ids = [a["id"] for a in articles]
    try:
        existing = collection.get(ids=all_ids, include=[])
        existing_ids = set(existing["ids"])
    except Exception:
        existing_ids = set()

    new_articles = [a for a in articles if a["id"] not in existing_ids]
    skipped = len(articles) - len(new_articles)

    if not new_articles:
        logger.info(f"[CORPUS] 全部 {len(articles)} 条已存在，无需入库")
        return {"added": 0, "skipped": skipped}

    logger.info(f"[CORPUS] 开始向量化 {len(new_articles)} 条新文章（跳过 {skipped} 条已存在）")

    # ── 构造向量化输入文本（标题 + 正文前 500 字） ─────────────────────────
    texts = [
        f"{a['title']}\n{a['content'][:500]}"
        for a in new_articles
    ]

    # ── 调用 Embedding API ─────────────────────────────────────────────────
    vectors = _embed_texts(texts)

    # ── 写入 ChromaDB ──────────────────────────────────────────────────────
    collection.add(
        ids=[a["id"] for a in new_articles],
        embeddings=vectors,
        documents=[f"{a['title']}\n{a['content'][:200]}" for a in new_articles],  # 用于展示
        metadatas=[
            {
                "topic":      a.get("topic", ""),
                "style":      a.get("style", ""),
                "platform":   a.get("platform", "wechat"),
                "crawled_at": a.get("crawled_at", ""),
                "title":      a["title"],
                "url":        a.get("url", ""),
            }
            for a in new_articles
        ],
    )

    logger.info(f"[CORPUS] ✅ 入库完成：新增 {len(new_articles)} 条，跳过 {skipped} 条")
    return {"added": len(new_articles), "skipped": skipped}
# ── 相似度检索 ────────────────────────────────────────────────────────────────

def search_similar(
    theme: str,
    style: str,
    top_k: int = 3,
    style_filter: bool = True,
) -> list[dict]:
    """
    根据主题和风格检索资料库中最相似的爆款内容

    Args:
        theme:        用户输入的主题，如 "护肤"
        style:        用户输入的风格，如 "干货"
        top_k:        返回条数，默认 3
        style_filter: 是否仅在同风格内检索（True=更精准，False=全库检索）

    Returns:
        list[dict]，每条包含 title / content / style / topic / similarity
        资料库为空时返回 []
    """
    collection = _get_collection()
    total = collection.count()

    if total == 0:
        logger.info("[CORPUS] 资料库为空，跳过检索")
        return []

    # ── 向量化查询文本 ─────────────────────────────────────────────────────
    query_text = f"{theme} {style}"
    try:
        vectors = _embed_texts([query_text])
        query_vector = vectors[0]
    except Exception as e:
        logger.error(f"[CORPUS] 查询向量化失败: {e}")
        return []

    # ── 构造检索参数 ───────────────────────────────────────────────────────
    where_filter = {"style": {"$eq": style}} if style_filter and style else None

    # 如果风格过滤后剩余条数不足 top_k，自动降级为全库检索
    if where_filter:
        try:
            count_result = collection.get(where=where_filter, include=[])
            if len(count_result["ids"]) < top_k:
                logger.info(f"[CORPUS] 风格 '{style}' 条数不足，降级为全库检索")
                where_filter = None
        except Exception:
            where_filter = None

    try:
        query_params = {
            "query_embeddings": [query_vector],
            "n_results": min(top_k, total),
            "include": ["documents", "metadatas", "distances"],
        }
        if where_filter:
            query_params["where"] = where_filter

        results = collection.query(**query_params)
    except Exception as e:
        logger.error(f"[CORPUS] ChromaDB 检索失败: {e}")
        return []

    # ── 格式化返回结果 ─────────────────────────────────────────────────────
    output = []
    ids       = results.get("ids", [[]])[0]
    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    for i, doc_id in enumerate(ids):
        meta = metadatas[i] if i < len(metadatas) else {}
        dist = distances[i] if i < len(distances) else 1.0
        # ChromaDB 余弦距离：distance = 1 - cosine_similarity
        similarity = round(1.0 - dist, 4)

        output.append({
            "id":         doc_id,
            "title":      meta.get("title", ""),
            "content":    documents[i] if i < len(documents) else "",
            "style":      meta.get("style", ""),
            "topic":      meta.get("topic", ""),
            "platform":   meta.get("platform", ""),
            "crawled_at": meta.get("crawled_at", ""),
            "similarity": similarity,
        })

    logger.info(f"[CORPUS] 检索完成 query='{query_text}'，返回 {len(output)} 条（top_k={top_k}）")
    return output


# ── 资料库统计 ────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    """获取资料库统计信息（总条数、各风格分布、各平台分布）"""
    collection = _get_collection()
    total = collection.count()

    by_style    = {}
    by_platform = {}
    last_updated = ""

    if total > 0:
        try:
            # 获取所有 metadata 用于统计
            all_data = collection.get(include=["metadatas"])
            for meta in all_data["metadatas"]:
                style    = meta.get("style", "未知")
                platform = meta.get("platform", "未知")
                date     = meta.get("crawled_at", "")

                by_style[style]       = by_style.get(style, 0) + 1
                by_platform[platform] = by_platform.get(platform, 0) + 1
                if date > last_updated:
                    last_updated = date
        except Exception as e:
            logger.warning(f"[CORPUS] 统计信息获取失败: {e}")

    return {
        "total":        total,
        "by_style":     by_style,
        "by_platform":  by_platform,
        "last_updated": last_updated,
    }


def delete_article(article_id: str) -> bool:
    """按 id 删除资料库中的单条内容（原始 JSON 不删除，保留备份）"""
    try:
        collection = _get_collection()
        collection.delete(ids=[article_id])
        logger.info(f"[CORPUS] 已删除 id={article_id}")
        return True
    except Exception as e:
        logger.error(f"[CORPUS] 删除失败 id={article_id}: {e}")
        return False
