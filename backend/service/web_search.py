"""联网搜索工具：Bing(CN) 为主、百度 兜底，零新增依赖。

为什么是 HTML 抓取而不是 duckduckgo_search / Jina：
实测这台机器上 DuckDuckGo（含 ddgs 库的数据源）、Jina Reader、SearXNG、Wikipedia **全部超时**，
只有 cn.bing.com 与 www.baidu.com 可达。所以「免费 + 免 Key」的现实解就是抓这两个的 HTML，
并用双源 + 解析自检来对冲「搜索引擎改版」的脆弱性（README 第 53 条）。

安全/礼貌：
- 只取标题/链接/摘要，不再抓结果页正文（省流量也不越界）；
- 超时 12s、单次最多 8 条、同 query 结果缓存 5 分钟；
- 日志只记状态码与条数，不记 query 以外的东西（query 会发给搜索引擎，这是联网查询的固有代价）。
"""

from __future__ import annotations

import html as html_module
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from agent.registry import ToolSpec

logger = logging.getLogger("agent.web")

BING_URL = "https://cn.bing.com/search"
BAIDU_URL = "https://www.baidu.com/s"
REQUEST_TIMEOUT = 12.0
MAX_RESULTS = 8
CACHE_TTL_SECONDS = 300.0
MAX_CONTEXT_CHARS = 2000

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class WebSearchError(Exception):
    """搜索失败（工具层会转成「错误：…」文本，不抛给用户）。"""


def _clean(raw: str) -> str:
    """去掉标签与 HTML 实体（Bing 摘要里满是 `&ensp;` 之类）。"""
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_bing(markup: str) -> list[dict[str, str]]:
    """解析 Bing 的 ``li.b_algo`` 结果块。"""
    results: list[dict[str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', markup, re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = html_module.unescape(link.group(1))
        title = _clean(link.group(2))
        snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = _clean(snippet_match.group(1)) if snippet_match else ""
        if title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snippet})
    return results


def parse_baidu(markup: str) -> list[dict[str, str]]:
    """解析百度的 ``div.result`` 结果块（链接是 baidu.com/link 跳转链，保留原样）。"""
    results: list[dict[str, str]] = []
    blocks = re.findall(
        r'<div[^>]+class="result[^"]*"[^>]*>(.*?)(?=<div[^>]+class="result|<div id="page|$)',
        markup,
        re.S,
    )
    for block in blocks:
        link = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url = html_module.unescape(link.group(1))
        title = _clean(link.group(2))
        snippet = _clean(block)
        # 摘要里通常包含标题文本，去掉前缀更像"摘要"
        snippet = snippet.replace(title, "").strip(" -—|")
        if title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": snippet[:200]})
    return results


def diversify(results: list[dict[str, str]], per_domain: int = 2) -> list[dict[str, str]]:
    """同一域名最多保留 ``per_domain`` 条，避免前几条全被官网/百科占满。"""
    counts: dict[str, int] = {}
    kept: list[dict[str, str]] = []
    for item in results:
        domain = urlparse(item["url"]).netloc.lower()
        if counts.get(domain, 0) >= per_domain:
            continue
        counts[domain] = counts.get(domain, 0) + 1
        kept.append(item)
    return kept


def _http_get(url: str, params: dict[str, str]) -> str:
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        # trust_env=True：跟随系统代理设置（与模型调用一致）
        with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True, trust_env=True) as client:
            response = client.get(url, params=params, headers=headers)
    except httpx.TimeoutException as exc:
        raise WebSearchError(f"搜索超时（{REQUEST_TIMEOUT:.0f}s）") from exc
    except httpx.HTTPError as exc:
        raise WebSearchError(f"搜索请求失败：{exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        raise WebSearchError(f"搜索服务返回 HTTP {response.status_code}")
    return response.text


class WebSearchService:
    """带缓存的双源搜索。线程安全（锁只保护缓存字典）。"""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._lock = threading.Lock()

    def search(self, query: str, top_k: int = 5) -> list[dict[str, str]]:
        keyword = (query or "").strip()
        if not keyword:
            raise WebSearchError("查询词不能为空")
        limit = max(1, min(int(top_k or 5), MAX_RESULTS))
        key = f"{keyword}|{limit}"

        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
                return cached[1][:limit]

        results: list[dict[str, str]] = []
        errors: list[str] = []
        for label, fetcher in (
            ("bing", lambda: self._bing(keyword)),
            ("baidu", lambda: self._baidu(keyword)),
        ):
            try:
                results = fetcher()
            except WebSearchError as exc:
                errors.append(f"{label}: {exc}")
                continue
            if results:
                logger.info("搜索命中源=%s query_len=%d results=%d", label, len(keyword), len(results))
                break
            errors.append(f"{label}: 解析到 0 条（页面结构可能变了）")

        if not results:
            raise WebSearchError("所有搜索源都不可用（" + "；".join(errors) + "）")

        with self._lock:
            self._cache[key] = (time.monotonic(), results)
        return results[:limit]

    # ---- 各源 ----
    def _bing(self, keyword: str) -> list[dict[str, str]]:
        markup = _http_get(
            BING_URL, {"q": keyword, "setlang": "zh-CN", "mkt": "zh-CN", "count": "10"}
        )
        results = parse_bing(markup) or parse_bing_alternate(markup)
        return diversify(results)

    def _baidu(self, keyword: str) -> list[dict[str, str]]:
        markup = _http_get(BAIDU_URL, {"wd": keyword, "rn": "10"})
        return diversify(parse_baidu(markup))

    # ---- 给模型看的上下文 ----
    def as_context(self, query: str, top_k: int = 5) -> str:
        results = self.search(query, top_k)
        lines = [f"以下是「{query}」的联网搜索结果（可能过时，回答时请附上来源链接）："]
        used = 0
        for index, item in enumerate(results, start=1):
            domain = urlparse(item["url"]).netloc or item["url"]
            block = f"\n[{index}] {item['title']}\n来源：{domain}（{item['url']}）\n摘要：{item['snippet'] or '（无摘要）'}"
            if used + len(block) > MAX_CONTEXT_CHARS:
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines)


def parse_bing_alternate(markup: str) -> list[dict[str, str]]:
    """Bing 有时不给 ``b_algo`` 而是 ``b_algoheader``/新结构；用更宽松的 h2+a 兜底。"""
    results: list[dict[str, str]] = []
    for match in re.finditer(
        r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', markup, re.S
    ):
        url = html_module.unescape(match.group(1))
        title = _clean(match.group(2))
        if title and url:
            results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= MAX_RESULTS:
            break
    return results


def build_web_search_tool(service: WebSearchService) -> ToolSpec:
    """构造 `web_search` 的 ToolSpec（与 KB 工具同一注入方式，agent/ 不动）。"""

    def handler(query: str, top_k: int = 5) -> str:
        try:
            return service.as_context(str(query), int(top_k))
        except WebSearchError as exc:
            return f"错误：联网搜索失败（{exc}）。可以改用本地知识库或直接说明无法联网查询。"
        except Exception as exc:  # noqa: BLE001
            return f"错误：联网搜索异常（{type(exc).__name__}）"

    return ToolSpec(
        name="web_search",
        description=(
            "联网搜索实时信息（新闻、行情、天气、最近发生的事、你不确定的新知识）。"
            "返回若干条结果的标题、来源链接与摘要。凡是问题涉及「最新/今天/最近/新闻/价格/版本发布」"
            "或你知识里没有的实时信息，先调用本工具再回答，并在回答中附上来源链接；"
            "本地文档类问题请用 search_knowledge_base，不要用本工具。"
            "query 请用具体关键词（例如「Python 3.15 发布」而不是「Python 最新版本是什么」）；"
            "查官方文档/发布页时可加 site: 限定（例如「site:python.org 3.15」）。"
            "通常一次调用就够，拿到结果后直接回答。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，尽量具体（可含时间、地点等限定）"},
                "top_k": {
                    "type": "integer",
                    "description": f"返回条数，默认 5，最多 {MAX_RESULTS}",
                },
            },
            "required": ["query"],
        },
        handler=handler,
    )


def decode_for_log(value: Any, limit: int = 40) -> str:  # pragma: no cover - 仅调试用
    return str(value)[:limit].replace(quote(" "), " ")