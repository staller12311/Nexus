"""FastAPI 入口：``uvicorn backend.main:app --reload``。"""

from __future__ import annotations

import importlib
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    version="1.2.0",
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