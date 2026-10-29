"""按 OpenAI 标准协议拉取上游的模型列表（``GET {base_url}/models``）。

安全约定：
- 这是一次性的只读请求，**不落库、不打日志里的 Key**（日志只记 host 与数量）；
- 用户填的 base_url 只允许 http/https；
- 失败一律抛 ``FetchModelsError``，由路由转成 400/502 的可读文案，前端降级为手动输入。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("agent.web")

#: 拉列表的超时（秒）；比对话短超时更短，因为它是用户点按钮的同步操作
FETCH_TIMEOUT_SECONDS = 10.0


class FetchModelsError(Exception):
    """拉取失败（映射成 400/502）。"""


def models_endpoint(base_url: str) -> str:
    """拼出 ``{base_url}/models``，并保持与「添加模型」一致的尾部斜杠处理。

    - ``https://api.x.cn/v1``   → ``https://api.x.cn/v1/models``
    - ``https://api.x.cn/v1/``  → ``https://api.x.cn/v1/models``（去尾斜杠，不会出现 ``//``）
    - ``https://api.x.cn/v1/models`` → 原样返回（用户直接把完整地址贴进来也不会拼成 ``/models/models``）
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise FetchModelsError("Base URL 不能为空")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise FetchModelsError("Base URL 必须是合法的 http:// 或 https:// 地址")
    if url.endswith("/models"):
        return url
    return f"{url}/models"


def _extract_ids(payload: Any) -> list[str]:
    """兼容几种常见返回形态：``{"data":[{"id":...}]}`` / ``{"models":[...]}`` / 裸数组。"""
    items: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                items = value
                break
    elif isinstance(payload, list):
        items = payload

    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            candidate = item.get("id") or item.get("name") or item.get("model")
        else:
            candidate = None
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip()
            if text not in ids:
                ids.append(text)
    return ids


def fetch_models(base_url: str, api_key: str, *, timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    key = (api_key or "").strip()
    if not key:
        raise FetchModelsError("API Key 不能为空")
    url = models_endpoint(base_url)
    host = urlparse(url).netloc

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        # trust_env=True：走系统代理设置，与 openai SDK 的行为保持一致
        with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=True) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise FetchModelsError(f"请求超时（{timeout:.0f}s），请检查网络或 Base URL") from exc
    except httpx.HTTPError as exc:
        raise FetchModelsError(f"连接失败：{exc.__class__.__name__}") from exc

    status = response.status_code
    if status in (401, 403):
        raise FetchModelsError(f"密钥无效或无权限（HTTP {status}），请检查 API Key")
    if status == 404:
        raise FetchModelsError(
            f"该服务商没有 /models 接口（HTTP 404，请求 {url}），请手动填写 Model ID"
        )
    if status >= 400:
        raise FetchModelsError(f"服务商返回 HTTP {status}，请手动填写 Model ID")

    try:
        payload = response.json()
    except ValueError as exc:
        raise FetchModelsError("上游返回的不是 JSON，可能是 HTML 网关页；请手动填写 Model ID") from exc

    ids = _extract_ids(payload)
    if not ids:
        raise FetchModelsError("上游返回里没有找到模型 id，请手动填写 Model ID")

    # 只记 host 与数量，绝不记 Key
    logger.info("拉取模型列表 host=%s count=%d", host, len(ids))
    return {"base_url": url, "models": ids, "count": len(ids)}