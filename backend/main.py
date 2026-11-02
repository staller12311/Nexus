"""FastAPI 入口：``uvicorn backend.main:app --reload``。"""

from __future__ import annotations

import importlib
import ipaddress
import logging
import secrets
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import REPO_ROOT  # noqa: F401 - 导入即把仓库根加进 sys.path
from .api.routes import router

ALLOWED_ORIGINS = [
    "http://localhost:5173",  # Vite 默认端口
    "http://127.0.0.1:5173",
    # Tauri v2 自定义协议 origin（不同平台/版本两种写法都放行）
    "http://tauri.localhost",
    "https://tauri.localhost",
    "tauri://localhost",
]

#: 知识库文档解析的可选依赖：(import 名, pip 包名)
OPTIONAL_DEPENDENCIES = (("docx", "python-docx"), ("pypdf", "pypdf"))

app = FastAPI(
    title="LLM Agent Web API",
    version="1.2.2",
    description=(
        "把本地 CLI 智能体包装成 HTTP 接口。所有智能体逻辑复用 `agent` 包，"
        "本服务只负责调用与传事件。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

#: 不需要鉴权的路径：健康检查要被 Docker healthcheck 从容器内调用，
#: /docs 与 /openapi.json 是纯文档、不含用户数据。
_AUTH_EXEMPT_PATHS = frozenset({"/api/health", "/openapi.json", "/docs", "/redoc"})

#: 视为「本机」的客户端标识。TestClient 用的是 "testclient"，不是真实 IP。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient", ""})


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 整段都是回环，避免 ::ffff:127.0.0.1 之类的写法漏判
    return host.startswith("127.") or host.endswith(":127.0.0.1")


def _is_private(host: str | None) -> bool:
    """容器内网地址（172.16/12、10/8、192.168/16、fc00::/7 等）。"""
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:  # 主机名之类的写法，无法判定为内网
        return False
    return addr.is_private or addr.is_link_local


def _effective_client(request: Request) -> str | None:
    """真实客户端地址：优先取反代头，但只在「对端本身是本机或内网」时采信。

    两个场景必须同时成立：

    - Docker 部署时后端看到的是 nginx 容器的内网 IP（如 172.18.0.3），
      真正的访问者写在 ``X-Real-IP`` 里 —— 不读这个头，从宿主机浏览器访问也会被误判成外部来源。
    - 反过来，如果后端被直接暴露在公网，任何人都能伪造 ``X-Real-IP: 127.0.0.1`` 绕过限制，
      所以只有对端本身是回环/内网时才认这个头。
    """
    peer = request.client.host if request.client else None
    if peer is None:
        return None
    if _is_loopback(peer) or _is_private(peer):
        forwarded = (request.headers.get("x-real-ip") or "").strip()
        if not forwarded:
            chain = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            forwarded = chain
        if forwarded:
            return forwarded
    return peer


def _auth_token(request: Request) -> str:
    """读 ``server.auth_token``；配置坏了就当作没配（fail-closed 到「仅回环」）。"""
    service = getattr(request.app.state, "service", None)
    if service is not None:
        return service.config.server.auth_token
    try:
        from agent.config import load_config

        return load_config().server.auth_token
    except Exception:  # noqa: BLE001 - 配置不可用时不能把服务打死，退回最严格策略
        return ""


@app.middleware("http")
async def guard_access(request: Request, call_next):
    """两层访问控制，二者取其一：

    - 配了 ``server.auth_token``：所有 /api 请求必须带 ``Authorization: Bearer <token>``；
    - 没配：只接受回环地址，外部来源一律 403。

    为什么默认是回环而不是「默认开放」：README 推荐用 Docker 部署，
    而 compose 里的端口映射一旦写成 ``0.0.0.0``，等于把一个能执行代码的 Agent
    直接挂到局域网上。默认收紧、需要时显式放开，是这两层里唯一正确的顺序。
    """
    path = request.url.path
    if path in _AUTH_EXEMPT_PATHS or not path.startswith("/api"):
        return await call_next(request)

    token = _auth_token(request)
    if token:
        header = request.headers.get("authorization") or ""
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            presented.strip(), token
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": "需要访问令牌：请在请求头带上 Authorization: Bearer <token>"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    client_host = _effective_client(request)
    if not _is_loopback(client_host):
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "本服务未设置 server.auth_token，出于安全考虑只允许本机访问。"
                    "如需从其他主机访问，请在 config.yaml 里设置 server.auth_token。"
                )
            },
        )
    return await call_next(request)


@app.get("/api/health", tags=["meta"], summary="健康检查（含运行时诊断）")
def health() -> dict[str, object]:
    from .service.runtime_diag import process_integrity_level, write_scope_warning

    warning = write_scope_warning()
    return {
        "status": "ok",
        "python": sys.executable,
        "integrity": process_integrity_level(),
        "write_warning": warning,
    }


def check_optional_dependencies() -> list[str]:
    """检查知识库解析依赖，返回缺失的 pip 包名（只检查，绝不抛错）。"""
    missing: list[str] = []
    for module, package in OPTIONAL_DEPENDENCIES:
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001 - 缺失/损坏安装都只提示，不影响主服务
            missing.append(package)
    return missing


def warn_missing_optional_dependencies() -> list[str]:
    """缺失时在控制台打黄色警告并记日志；只警告、不崩溃。"""
    missing = check_optional_dependencies()
    if not missing:
        return []
    message = (
        f"⚠️ 缺少依赖 {' 或 '.join(missing)}，文档解析功能将不可用。"
        "请运行 pip install -r requirements.txt"
    )
    # ANSI 黄色：本地 uvicorn 控制台直接可见；不依赖 rich/日志配置
    print(f"\033[33m{message}\033[0m", file=sys.stderr, flush=True)
    logging.getLogger("agent.web").warning(message)
    return missing


@app.on_event("startup")
def _log_startup() -> None:
    logging.getLogger("agent.web").info("Web API 启动，仓库根：%s", REPO_ROOT)
    from .service.runtime_diag import write_scope_warning

    warning = write_scope_warning()
    if warning:
        logging.getLogger("agent.web").warning(warning)
    warn_missing_optional_dependencies()