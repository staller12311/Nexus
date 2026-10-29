"""深挖：Bing(CN) 与百度 的 HTML 能否稳定解析出 标题/链接/摘要。"""
from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

print("代理环境变量: HTTP_PROXY=%s HTTPS_PROXY=%s NO_PROXY=%s" % (
    bool(os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")),
    bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
    os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "(未设置)",
))


def fetch(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", "replace")


def parse_bing(html: str) -> list[tuple[str, str, str]]:
    """Bing 结果块：<li class="b_algo"><h2><a href=...>标题</a></h2><p>摘要</p>"""
    results: list[tuple[str, str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url, raw_title = link.group(1), link.group(2)
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip() if snippet_match else ""
        results.append((title, url, snippet))
    return results


def parse_baidu(html: str) -> list[tuple[str, str, str]]:
    """百度结果块：<div class="result ..."> ... <h3><a href=...>标题</a></h3> ... 摘要"""
    results: list[tuple[str, str, str]] = []
    for block in re.findall(r'<div[^>]+class="result[^"]*"[^>]*>(.*?)</div>\s*(?=<div|<script|$)', html, re.S):
        link = re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url, raw_title = link.group(1), link.group(2)
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        body = re.sub(r"<[^>]+>", " ", block)
        body = re.sub(r"\s+", " ", body).strip()
        results.append((title, url, body[:160]))
    return results


QUERIES = ["Python asyncio 教程", "今天 A股 行情"]

for query in QUERIES:
    print("\n" + "=" * 78)
    print(f"查询：{query}")
    print("=" * 78)

    bing_url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-CN&count=10"
    try:
        html = fetch(bing_url)
        results = parse_bing(html)
        print(f"  Bing: 解析到 {len(results)} 条（HTML {len(html)}B）")
        for title, url, snippet in results[:3]:
            print(f"    - {title[:40]!r} | {url[:60]}")
            print(f"      {snippet[:70]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  Bing 失败: {type(exc).__name__}: {str(exc)[:70]}")

    baidu_url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(query)
    try:
        html = fetch(baidu_url)
        results = parse_baidu(html)
        print(f"  百度: 解析到 {len(results)} 条（HTML {len(html)}B）")
        for title, url, snippet in results[:3]:
            print(f"    - {title[:40]!r} | {url[:60]}")
        if "百度安全验证" in html or "wappass" in html:
            print("    ⚠️ 命中百度反爬验证页")
    except Exception as exc:  # noqa: BLE001
        print(f"  百度 失败: {type(exc).__name__}: {str(exc)[:70]}")