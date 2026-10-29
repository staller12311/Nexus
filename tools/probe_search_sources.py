"""探测各免 Key 搜索源的可达性（决定 web_search 用什么）。"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

ENDPOINTS = [
    ("DuckDuckGo HTML", "https://html.duckduckgo.com/html/?q=python+asyncio"),
    ("DuckDuckGo Lite", "https://lite.duckduckgo.com/lite/?q=python+asyncio"),
    ("DuckDuckGo API(instant)", "https://api.duckduckgo.com/?q=python&format=json&no_html=1"),
    ("Jina Reader (r.jina.ai)", "https://r.jina.ai/https://example.com"),
    ("Jina Search (s.jina.ai)", "https://s.jina.ai/python+asyncio"),
    ("Bing (cn.bing.com)", "https://cn.bing.com/search?q=python+asyncio"),
    ("Baidu", "https://www.baidu.com/s?wd=python+asyncio"),
    ("SearXNG 公共实例", "https://searx.be/search?q=python&format=json"),
    ("Wikipedia API", "https://zh.wikipedia.org/api/rest_v1/page/summary/Python"),
]


def probe(label: str, url: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            body = response.read(4000)
            length = len(body)
            preview = body[:120].decode("utf-8", "replace").replace("\n", " ")
            ok = "<html" in preview.lower() or "{" in preview or "title" in preview.lower()
            print(f"  {label:26} HTTP {response.status}  {length}B  {preview[:70]!r}")
        _ = ok
    except urllib.error.HTTPError as exc:
        print(f"  {label:26} HTTP {exc.code}  ❌")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:26} 失败 {type(exc).__name__}: {str(exc)[:60]}")


print("=" * 78)
print("网络可达性探测（无 Key 搜索源）")
print("=" * 78)
for label, url in ENDPOINTS:
    probe(label, url)

print("\n" + "=" * 78)
print("已安装的相关包")
print("=" * 78)
for module in ("ddgs", "duckduckgo_search", "httpx", "bs4", "lxml", "html5lib"):
    try:
        __import__(module)
        print(f"  {module:20} 已安装")
    except ImportError:
        print(f"  {module:20} 未安装")
sys.exit(0)