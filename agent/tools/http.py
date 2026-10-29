"""http_get（默认禁用，骨架 + 白名单）。

安全模型：
- ``allowed_domains`` 为空 → 拒绝一切请求（默认状态）；
- 只允许 http/https 的 GET；
- 命中规则：host 完全相同，或是白名单域名的子域；
- **重定向也要重新过白名单**：默认的 urllib 会自动跟随跳转，
  等于给了「白名单域名 302 到任意地址」的绕过口子，因此自定义 RedirectHandler 拦截；
- 用标准库 urllib，不引入额外依赖。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Sequence
from urllib.parse import urlparse

from . import ToolError

DEFAULT_TIMEOUT = 15.0
MAX_BYTES = 200_000
USER_AGENT = "dsh-agent/0.1 (+http_get tool)"


def _normalize_domains(allowed_domains: Sequence[str]) -> list[str]:
    return [
        item.strip().lower().lstrip(".")
        for item in allowed_domains
        if item and item.strip()
    ]


def _host_allowed(host: str | None, domains: Sequence[str]) -> bool:
    if not host:
        return False
    host = host.lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _check_url(url: str, domains: Sequence[str]) -> str:
    if not domains:
        raise ToolError(
            "http_get 域名白名单为空，已拒绝访问。"
            "如确需联网，请在 config.yaml 的 tools.http_get.allowed_domains 中"
            "填入允许的域名（如 api.github.com），并把 http_get 加入 tools.enabled。"
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ToolError(f"只支持 http/https，收到 {parsed.scheme or '空'} 协议：{url}")
    if not _host_allowed(parsed.hostname, domains):
        raise ToolError(
            f"域名 {parsed.hostname!r} 不在白名单中。白名单：{domains}。"
            f"需要新增请修改 config.yaml 的 tools.http_get.allowed_domains。"
        )
    return url


class _WhitelistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """每次跳转都重新校验目标域名，防止「白名单域名 302 到别处」。"""

    def __init__(self, domains: Sequence[str]) -> None:
        super().__init__()
        self._domains = list(domains)

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        _check_url(newurl, self._domains)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(
    url: str,
    allowed_domains: Sequence[str] = (),
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> str:
    """GET 一个白名单内的 URL，返回状态、内容类型与截断后的正文。"""
    domains = _normalize_domains(allowed_domains)
    target = _check_url(url.strip(), domains)

    opener = urllib.request.build_opener(_WhitelistRedirectHandler(domains))
    request = urllib.request.Request(target, method="GET", headers={"User-Agent": USER_AGENT})

    try:
        with opener.open(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl()
            raw = response.read(int(max_bytes) + 1)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(2000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 错误响应体读不到就算了
            body = ""
        raise ToolError(f"HTTP {exc.code} {exc.reason}：{target}\n{body[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"请求失败（网络不可达或域名解析失败）：{target}（{exc.reason}）") from exc
    except TimeoutError as exc:
        raise ToolError(f"请求超时（{timeout:g}s）：{target}") from exc

    limit = max(1, int(max_bytes))
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")

    header = [f"GET {final_url}", f"HTTP {status}", f"Content-Type: {content_type or '未知'}"]
    if final_url != target:
        header.append(f"（发生过重定向，原始地址：{target}）")
    if truncated:
        header.append(f"（响应过大，只返回前 {limit} 字节）")
    return "\n".join(header) + "\n\n" + text