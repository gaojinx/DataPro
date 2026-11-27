"""联网搜索（无 Key 方案）：调用 Bing RSS 搜索，供通用问答引用。

仅用于非取数类问题（天气/新闻/行业信息）；取数与口径问题一律走数仓链路，
不把搜索结果当作数据依据，避免编造数值。
"""
import asyncio
import html
import logging
import re
import urllib.parse
import urllib.request

from app.config import settings

logger = logging.getLogger("datapro.websearch")

ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.S)
DESC_RE = re.compile(r"<description>(.*?)</description>", re.S)
# 提问中的口语词，检索前去掉以提高命中率
STOPWORDS = ("请问", "帮我", "查一下", "查查", "最近", "有什么", "怎么样", "多少", "是什么",
             "可以", "吗", "呢", "呀", "啊", "的新闻", "一下")


def _clean_query(query: str) -> str:
    q = query.strip().strip("？?。.!！")
    for w in STOPWORDS:
        q = q.replace(w, " ")
    q = re.sub(r"\s+", " ", q).strip() or query
    if "天气" in q:
        # 天气类：去掉口语时间词，补"天气预报"，提高命中气象站点
        q = re.sub(r"今天|明天|现在|怎么样|如何", " ", q)
        q = re.sub(r"\s+", " ", q).strip() + " 天气预报"
    elif "新闻" in q:
        q = q + " 最新消息"
    return q


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=6) as resp:
        return resp.read().decode("utf-8", "ignore")


def _text(s: str) -> str:
    return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", s)).strip()


def _parse(page: str, limit: int) -> list[dict]:
    out = []
    for item in ITEM_RE.findall(page):
        t, l = TITLE_RE.search(item), LINK_RE.search(item)
        if not t or not l:
            continue
        d = DESC_RE.search(item)
        out.append({"title": _text(t.group(1))[:80], "url": _text(l.group(1)),
                    "snippet": _text(d.group(1))[:200] if d else ""})
        if len(out) >= limit:
            break
    return out


async def search(query: str, limit: int = 5) -> list[dict]:
    """返回 [{title, url, snippet}]；失败返回空列表（不影响主链路）。"""
    if not settings.web_search_enabled:
        return []
    url = "https://cn.bing.com/search?format=rss&q=" + urllib.parse.quote(_clean_query(query)) + "&setlang=zh-CN"
    try:
        page = await asyncio.to_thread(_fetch, url)
        return _parse(page, limit)
    except Exception as exc:  # noqa: BLE001 —— 搜索失败按无结果处理
        logger.warning("联网搜索失败：%s", exc)
        return []
