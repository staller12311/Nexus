"""探测 Bing 的 news 垂直频道与 market 参数对相关性的影响。"""
from __future__ import annotations

import html as html_module
import re

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
H = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}


def get(url: str, params: dict[str, str]) -> str:
    with httpx.Client(timeout=15, follow_redirects=True, trust_env=True) as client:
        return client.get(url, params=params, headers=H).text


def clean(raw: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", "", raw or ""))).strip()


query = "最新 Python 版本 发布 2026"

print("=" * 72)
print("A) /search + mkt=zh-CN")
markup = get("https://cn.bing.com/search", {"q": query, "mkt": "zh-CN", "count": "10"})
blocks = re.findall(r'<li class="b_algo".*?</li>', markup, re.S)
print(f"   b_algo 块数={len(blocks)}")
for block in blocks[:5]:
    hit = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
    if hit:
        print("   -", clean(hit.group(2))[:50], "|", hit.group(1)[:60])

print("=" * 72)
print("B) /news/search 新闻垂直")
news = get("https://cn.bing.com/news/search", {"q": "Python 新版本 发布"})
print(f"   HTML {len(news)}B")
for pattern in (
    r'<a class="news-card[^"]*"[^>]*data-title="([^"]+)"[^>]*href="([^"]+)"',
    r'<div class="news-card[^"]*"[^>]*>.*?data-title="([^"]+)".*?href="(https?://[^"]+)"',
    r'title="([^"]{10,120})"[^>]*href="(https?://[^"]+)"',
):
    found = re.findall(pattern, news, re.S)
    print(f"   模式 {pattern[:40]}… -> {len(found)} 条")
    for title, url in found[:3]:
        print("     -", clean(title)[:50], "|", url[:60])
    if found:
        break

print("=" * 72)
print("C) 对照：天气查询")
markup2 = get("https://cn.bing.com/search", {"q": "上海 今天 天气", "mkt": "zh-CN"})
algo_pattern = '<li class="b_algo".*?</li>'
print("   b_algo 块数=", len(re.findall(algo_pattern, markup2, re.S)))