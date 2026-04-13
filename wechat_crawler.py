"""
微信公众号爬虫模块
来源：搜狗微信搜索（https://weixin.sogou.com）
特点：无需登录账号，直接 HTTP 请求，风险最低
"""

import time
import random
import hashlib
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── 常量配置 ─────────────────────────────────────────────────────────────────

SOGOU_SEARCH_URL = "https://weixin.sogou.com/weixin"

# 常见浏览器 UA 池，随机轮换，降低被识别为爬虫的概率
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# 请求超时（秒）
REQUEST_TIMEOUT = 15

# 每次请求后的随机延迟范围（秒）
DELAY_MIN = 3.0
DELAY_MAX = 8.0

# 单次爬取最大条数上限（防止过度爬取）
MAX_LIMIT = 20


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _random_delay():
    """随机等待，模拟人类浏览行为"""
    delay = random.uniform(DELAY_MIN, DELAY_MAX)
    logger.debug(f"[CRAWLER] 等待 {delay:.1f} 秒...")
    time.sleep(delay)


def _get_headers() -> dict:
    """构造随机 UA 的请求头"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://weixin.sogou.com/",
    }


def _make_id(url: str, crawled_at: str) -> str:
    """根据 URL + 日期生成唯一 ID"""
    raw = f"{url}_{crawled_at}"
    return "wechat_" + hashlib.md5(raw.encode()).hexdigest()[:12]


def _clean_text(text: str) -> str:
    """清洗文本：去除多余空白、特殊字符"""
    if not text:
        return ""
    # 合并连续空白
    text = re.sub(r'\s+', ' ', text)
    # 去除零宽字符
    text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
    return text.strip()


def _is_ad_or_low_quality(title: str, content: str) -> bool:
    """简单过滤广告和低质量内容"""
    ad_keywords = ["广告", "点击领取", "扫码领红包", "限时优惠", "立即购买", "免费领取"]
    text = title + content
    for kw in ad_keywords:
        if kw in text:
            return True
    # 正文过短，质量太低
    if len(content) < 50:
        return True
    return False


# ── 核心爬取函数 ──────────────────────────────────────────────────────────────

def fetch_article_content(url: str, session: requests.Session) -> str:
"""
    爬取单篇微信文章的正文内容
    返回纯文本正文，失败时返回空字符串
    """
    try:
        _random_delay()
        resp = session.get(url, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
        resp.encoding = "utf-8"

        if resp.status_code != 200:
            logger.warning(f"[CRAWLER] 文章请求失败 status={resp.status_code} url={url}")
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")

        # 微信文章正文容器
        content_div = soup.find("div", id="js_content")
        if not content_div:
            # 兜底：尝试 article 标签
            content_div = soup.find("article")
        if not content_div:
            logger.warning(f"[CRAWLER] 未找到正文容器 url={url}")
            return ""

        # 提取所有段落文本
        paragraphs = []
        for tag in content_div.find_all(["p", "section", "span"]):
            text = _clean_text(tag.get_text())
            if text and len(text) > 10:
                paragraphs.append(text)

        # 去重相邻重复段落
        unique_paragraphs = []
        prev = ""
        for p in paragraphs:
            if p != prev:
                unique_paragraphs.append(p)
            prev = p

        return "\n".join(unique_paragraphs)

    except requests.exceptions.Timeout:
        logger.warning(f"[CRAWLER] 请求超时 url={url}")
        return ""
    except Exception as e:
        logger.warning(f"[CRAWLER] 文章抓取异常: {e} url={url}")
        return ""


def crawl_wechat(
    keyword: str,
    topic: str,
    style: str,
    limit: int = 10,
    fetch_content: bool = True,
) -> list[dict]:
    """
    爬取微信公众号爆款内容

    Args:
        keyword:       搜索关键词，如 "护肤干货"
        topic:         主题标签，如 "护肤"（用于资料库分类）
        style:         风格标签，如 "干货"（干货/情感/搞笑/带货/探店）
        limit:         最多爬取条数，上限 MAX_LIMIT=20
        fetch_content: 是否爬取文章详情页正文（False 时只取摘要，速度更快）

    Returns:
        list[dict]，每条数据结构见 corpus_plan.md 任务1
    """
    limit = min(limit, MAX_LIMIT)
    crawled_at = datetime.now().strftime("%Y-%m-%d")
    results = []

    logger.info(f"[CRAWLER] 开始爬取 keyword='{keyword}' limit={limit}")

    # 创建 Session 复用连接
    session = requests.Session()
    session.headers.update(_get_headers())

    # ── 第一步：请求搜狗微信搜索列表页 ─────────────────────────────────────
    params = {
        "type": "2",        # type=2 表示搜文章
        "query": keyword,
        "page": "1",
    }
    search_url = f"{SOGOU_SEARCH_URL}?{urlencode(params)}"

    try:
        resp = session.get(search_url, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
        resp.encoding = "utf-8"
    except requests.exceptions.Timeout:
        logger.error(f"[CRAWLER] 搜索页请求超时 keyword='{keyword}'")
        return []
    except Exception as e:
        logger.error(f"[CRAWLER] 搜索页请求失败: {e}")
        return []

    if resp.status_code != 200:
        logger.error(f"[CRAWLER] 搜索页返回异常 status={resp.status_code}")
        return []

    # ── 检测是否触发验证码 ───────────────────────────────────────────────────
    if "请输入验证码" in resp.text or "sogou.com/antispider" in resp.url:
        logger.error("[CRAWLER] ⚠️ 触发搜狗验证码，本次爬取中止。请稍后再试或更换 IP。")
        return []

    # ── 第二步：解析文章列表 ─────────────────────────────────────────────────
    soup = BeautifulSoup(resp.text, "html.parser")

    # 搜狗微信搜索结果容器
    article_items = soup.select("ul.news-list li.news-box") or soup.select(".news-list .news-box")

    if not article_items:
        # 兜底选择器（搜狗偶尔改版）
        article_items = soup.select(".txt-box") or soup.select("[uigs]")

    if not article_items:
        logger.warning(f"[CRAWLER] 未解析到文章列表，可能页面结构已变更。keyword='{keyword}'")
        return []

    logger.info(f"[CRAWLER] 找到 {len(article_items)} 条候选文章")
# ── 第三步：逐条提取数据 ─────────────────────────────────────────────────
    for idx, item in enumerate(article_items[:limit]):
        try:
            # 标题
            title_tag = item.select_one("h3 a") or item.select_one(".txt-box h3 a")
            if not title_tag:
                continue
            title = _clean_text(title_tag.get_text())
            if not title:
                continue

            # 文章链接（搜狗会做跳转，取原始 href 即可，后续会被重定向到 mp.weixin.qq.com）
            raw_url = title_tag.get("href", "")
            if not raw_url:
                continue
            article_url = urljoin("https://weixin.sogou.com", raw_url)

            # 摘要（作为 content 的兜底）
            summary_tag = item.select_one("p.txt-info") or item.select_one(".txt-box p")
            summary = _clean_text(summary_tag.get_text()) if summary_tag else ""

            # ── 爬取详情页正文 ────────────────────────────────────────────
            content = ""
            if fetch_content:
                logger.info(f"[CRAWLER] [{idx+1}/{limit}] 抓取正文: {title[:30]}...")
                content = fetch_article_content(article_url, session)
            
            # 详情页失败时降级用摘要
            if not content:
                content = summary

            # 过滤广告和低质内容
            if _is_ad_or_low_quality(title, content):
                logger.info(f"[CRAWLER] 跳过低质/广告内容: {title[:30]}")
                continue

            article_id = _make_id(article_url, crawled_at)

            results.append({
                "id": article_id,
                "platform": "wechat",
                "title": title,
                "content": content,
                "url": article_url,
                "topic": topic,
                "style": style,
                "crawled_at": crawled_at,
            })

            logger.info(f"[CRAWLER] ✅ 已采集 [{len(results)}] {title[:30]}（正文 {len(content)} 字）")

        except Exception as e:
            logger.warning(f"[CRAWLER] 解析第 {idx+1} 条异常: {e}")
            continue

    logger.info(f"[CRAWLER] 爬取完成，共采集 {len(results)} 条有效内容（keyword='{keyword}'）")
    return results
