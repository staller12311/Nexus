"""测具体查询 vs 模糊查询在 Bing 上的相关性差异。"""
from __future__ import annotations

import sys

sys.path.insert(0, r"D:\MyAi")

from backend.service.web_search import WebSearchService  # noqa: E402

service = WebSearchService()
for query in (
    "Python 3.15 发布",
    "Python 3.14 新特性",
    "site:python.org 3.15",
    "上海 天气",
):
    print("=" * 72)
    print("查询:", query)
    try:
        for item in service.search(query, 3):
            print("  -", item["title"][:46])
            print("    ", item["url"][:78])
    except Exception as exc:  # noqa: BLE001
        print("  失败:", exc)