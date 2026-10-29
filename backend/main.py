"""FastAPI 入口：``uvicorn backend.main:app --reload``。"""

from __future__ import annotations

import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import REPO_ROOT  # noqa: F401 - 导入即把仓库根加进 sys.path
from .api.routes import router

ALLOWED_ORIGINS = [
    "http://localhost:5173",  # Vite 默认端口
    "http://127.0.0.1:5173",
]

app = FastAPI(
    title="LLM Agent Web API",
    version="0.1.0",
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


@app.on_event("startup")
def _log_startup() -> None:
    logging.getLogger("agent.web").info("Web API 启动，仓库根：%s", REPO_ROOT)
    from .service.runtime_diag import write_scope_warning

    warning = write_scope_warning()
    if warning:
        logging.getLogger("agent.web").warning(warning)