import json
import os
import threading
import time

from flask import Blueprint, jsonify, request

from utils.auth import require_auth
from utils.token_tracker import get_daily_token_usage
from config import SINGLE_CALL_TOKEN_LIMIT, DAILY_TOKEN_LIMIT, DAILY_COST_LIMIT

# 资料库模块（可选，未安装时相关接口返回 503）
try:
    import corpus_manager
    _CORPUS_AVAILABLE = True
except ImportError:
    _CORPUS_AVAILABLE = False

corpus_bp = Blueprint("corpus", __name__)

_VALID_STYLES = {"干货", "情感", "搞笑", "带货", "探店"}


# ══════════════════════════════════════════════════════════════════════════════
# Token 统计
# ══════════════════════════════════════════════════════════════════════════════

@corpus_bp.route("/api/token-stats", methods=["GET"])
def token_stats():
    """查询 token 使用情况统计"""
    err = require_auth()
    if err:
        return err

    daily_usage = get_daily_token_usage()
    return jsonify({
        "code": 200,
        "msg":  "查询成功",
        "data": {
            "date":          time.strftime("%Y-%m-%d"),
            "input_tokens":  daily_usage["input_tokens"],
            "output_tokens": daily_usage["output_tokens"],
            "total_tokens":  daily_usage["total_tokens"],
            "cost_yuan":     f"{daily_usage['cost']:.4f}",
            "limits": {
                "single_call_token_limit": SINGLE_CALL_TOKEN_LIMIT,
                "daily_token_limit":       DAILY_TOKEN_LIMIT,
                "daily_cost_limit":        DAILY_COST_LIMIT,
            },
        },
    })


# ══════════════════════════════════════════════════════════════════════════════
# 语料库管理
# ══════════════════════════════════════════════════════════════════════════════

@corpus_bp.route("/api/corpus/crawl", methods=["POST"])
def corpus_crawl():
    """
    触发一次公众号爬取并入库（后台异步执行，立即返回）
    请求体：{ "keyword": "护肤干货", "style": "干货", "topic": "护肤", "limit": 10 }
    """
    err = require_auth()
    if err:
        return err

    if not _CORPUS_AVAILABLE:
        return jsonify({"code": 503, "msg": "资料库模块未安装，请先安装依赖：chromadb, openai, beautifulsoup4, requests"}), 503

    data    = request.get_json(silent=True) or {}
    keyword = data.get("keyword", "").strip()
    style   = data.get("style",   "干货").strip()
    topic   = data.get("topic",   "").strip() or keyword
    limit   = min(int(data.get("limit", 10)), 20)

    if not keyword:
        return jsonify({"code": 400, "msg": "keyword 不能为空"})
    if style not in _VALID_STYLES:
        return jsonify({"code": 400, "msg": "style 不合法，可选值：干货、情感、搞笑、带货、探店"})

    def _crawl_and_index():
        try:
            from crawler.wechat_crawler import crawl_wechat
            articles = crawl_wechat(keyword=keyword, topic=topic, style=style, limit=limit)
            if articles:
                corpus_manager.save_raw(articles)
                result = corpus_manager.index_articles(articles)
                print(f"[CORPUS] 爬取任务完成 keyword='{keyword}' 新增={result['added']} 跳过={result['skipped']}")
            else:
                print(f"[CORPUS] 爬取任务完成但无有效数据 keyword='{keyword}'")
        except Exception as e:
            print(f"[CORPUS] 爬取任务异常: {e}")

    threading.Thread(target=_crawl_and_index, daemon=True).start()

    return jsonify({
        "code": 200,
        "msg":  f"爬取任务已提交，关键词='{keyword}'，最多爬取 {limit} 条，后台处理中...",
        "data": {"keyword": keyword, "style": style, "topic": topic, "limit": limit},
    })


@corpus_bp.route("/api/corpus/stats", methods=["GET"])
def corpus_stats():
    """查看语料库统计信息（总条数、各风格分布、各平台分布）"""
    err = require_auth()
    if err:
        return err

    if not _CORPUS_AVAILABLE:
        return jsonify({"code": 503, "msg": "资料库模块未安装"}), 503

    try:
        stats = corpus_manager.get_stats()
        return jsonify({"code": 200, "msg": "查询成功", "data": stats})
    except Exception as e:
        return jsonify({"code": 500, "msg": f"查询失败: {e}"}), 500
        @corpus_bp.route("/api/corpus/list", methods=["GET"])
def corpus_list():
    """
    分页查看语料库中的原始内容
    查询参数：page=1&page_size=20&style=干货&topic=护肤
    """
    err = require_auth()
    if err:
        return err

    if not _CORPUS_AVAILABLE:
        return jsonify({"code": 503, "msg": "资料库模块未安装"}), 503

    page      = max(1, int(request.args.get("page",      1)))
    page_size = min(50, max(1, int(request.args.get("page_size", 20))))
    style     = request.args.get("style", "").strip()
    topic     = request.args.get("topic", "").strip()

    try:
        all_articles = corpus_manager.load_raw_all()
        if style:
            all_articles = [a for a in all_articles if a.get("style") == style]
        if topic:
            all_articles = [a for a in all_articles if topic in a.get("topic", "")]

        total = len(all_articles)
        all_articles.sort(key=lambda x: x.get("crawled_at", ""), reverse=True)
        start     = (page - 1) * page_size
        page_data = all_articles[start: start + page_size]

        return jsonify({
            "code": 200,
            "msg":  "查询成功",
            "data": {
                "total":     total,
                "page":      page,
                "page_size": page_size,
                "items":     page_data,
            },
        })
    except Exception as e:
        return jsonify({"code": 500, "msg": f"查询失败: {e}"}), 500


@corpus_bp.route("/api/corpus/delete", methods=["DELETE"])
def corpus_delete():
    """
    删除语料库中的单条内容（向量库删除，原始 JSON 保留备份）
    请求体：{ "id": "wechat_xxxx" }
    """
    err = require_auth()
    if err:
        return err

    if not _CORPUS_AVAILABLE:
        return jsonify({"code": 503, "msg": "资料库模块未安装"}), 503

    data       = request.get_json(silent=True) or {}
    article_id = data.get("id", "").strip()

    if not article_id:
        return jsonify({"code": 400, "msg": "id 不能为空"})

    success = corpus_manager.delete_article(article_id)
    if success:
        return jsonify({"code": 200, "msg": f"已删除 id={article_id}"})
    return jsonify({"code": 500, "msg": "删除失败，请检查 id 是否存在"}), 500
    @corpus_bp.route("/api/corpus/schedule", methods=["POST"])
def corpus_schedule():
    """
    设置每日定时爬取关键词列表（持久化到 corpus/schedule.json）
    请求体：{ "keywords": [{"keyword":"护肤干货","style":"干货","topic":"护肤","limit":10}, ...] }
    """
    err = require_auth()
    if err:
        return err

    if not _CORPUS_AVAILABLE:
        return jsonify({"code": 503, "msg": "资料库模块未安装"}), 503

    data     = request.get_json(silent=True) or {}
    keywords = data.get("keywords", [])

    if not isinstance(keywords, list) or not keywords:
        return jsonify({"code": 400, "msg": "keywords 不能为空，格式：[{keyword, style, topic, limit}]"})

    for item in keywords:
        if not item.get("keyword"):
            return jsonify({"code": 400, "msg": "每个关键词配置必须包含 keyword 字段"})
        if item.get("style") and item["style"] not in _VALID_STYLES:
            return jsonify({"code": 400, "msg": f"style '{item['style']}' 不合法"})

    schedule_path = os.path.join(os.path.dirname(__file__), "..", "corpus", "schedule.json")
    os.makedirs(os.path.dirname(schedule_path), exist_ok=True)
    with open(schedule_path, "w", encoding="utf-8") as f:
        json.dump(
            {"keywords": keywords, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            f, ensure_ascii=False, indent=2,
        )

    return jsonify({
        "code": 200,
        "msg":  f"定时任务已设置，共 {len(keywords)} 个关键词，每日 06:00 自动爬取",
        "data": {"keywords_count": len(keywords)},
    })
