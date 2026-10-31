"""Nexus PyInstaller 入口：绑定随机端口 → 打印端口 → 交给 uvicorn。

开发模式也可以直接运行：

    D:\\python3.11\\python.exe packaging\\nexus_backend.py --config config.yaml

Tauri 通过 stdout 的 ``NEXUS_PORT=<n>`` 行拿到实际端口；
stderr 由 Rust 收集到日志。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
from pathlib import Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nexus backend launcher")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--config", default="", help="外部 config.yaml 路径")
    parser.add_argument("--data-dir", default="", help="桌面端数据目录")
    return parser.parse_args(argv)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """PyInstaller onedir: ``_MEIPASS`` 指向 ``_internal``；开发态指向仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return repo_root()


def bundled_root() -> Path:
    """exe 所在目录，用于放构建脚本额外复制进去的 .env（不放进 _internal）。"""
    return Path(sys.executable).resolve().parent


def prepare_runtime(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir) if args.data_dir else None
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)

    if args.config:
        config = Path(args.config).expanduser().resolve()
    elif data_dir is not None:
        config = data_dir / "config.yaml"
        if not config.exists():
            example = resource_root() / "config.example.yaml"
            if not example.is_file():
                example = bundled_root() / "config.example.yaml"
            if example.is_file():
                shutil.copyfile(example, config)
    else:
        config = repo_root() / "config.yaml"

    if not config.exists():
        raise SystemExit(f"找不到配置文件：{config}")
    os.environ["AGENT_CONFIG"] = str(config)

    # 第一次运行时，把打包资源里的 prompts / .env 复制到用户数据目录。
    # 已存在则绝不覆盖：用户改过的提示词/密钥优先。
    if data_dir is not None:
        prompts_src = resource_root() / "prompts"
        prompts_dst = data_dir / "prompts"
        if prompts_src.is_dir() and not prompts_dst.exists():
            shutil.copytree(prompts_src, prompts_dst)

        env_src = bundled_root() / ".env"
        if not env_src.is_file():
            env_src = resource_root() / ".env"
        env_dst = data_dir / ".env"
        if env_src.is_file() and not env_dst.exists():
            shutil.copyfile(env_src, env_dst)
            print(f"NEXUS_ENV_COPIED={env_dst}", file=sys.stderr, flush=True)

    if getattr(sys, "frozen", False):
        # 个人版优先用随包的完整 venv；分发版只有便携 Python。
        candidates = [
            bundled_root() / "venv" / "Scripts" / "python.exe",
            bundled_root() / "python" / "python.exe",
        ]
        for python_exe in candidates:
            if python_exe.is_file():
                os.environ["NEXUS_PYTHON_EXECUTABLE"] = str(python_exe)
                print(f"NEXUS_PYTHON={python_exe}", file=sys.stderr, flush=True)
                break

    if not getattr(sys, "frozen", False):
        # 开发态：让 backend/agent 包和 site-packages 都可导入
        sys.path.insert(0, str(repo_root()))
        site_packages = repo_root() / ".venv" / "Lib" / "site-packages"
        if site_packages.is_dir():
            sys.path.insert(0, str(site_packages))
    print(f"NEXUS_CONFIG={config}", file=sys.stderr, flush=True)


def bind_socket(host: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # uvicorn 自己会 set SO_REUSEADDR；先设置避免 Windows TIME_WAIT 小概率冲突
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    sock.bind((host, 0))
    sock.listen(128)
    return sock


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    prepare_runtime(args)

    sock = bind_socket(args.host)
    port = sock.getsockname()[1]
    # 这一行是 Rust 与 Python 之间唯一约定；必须先 flush
    print(f"NEXUS_PORT={port}", flush=True)

    import uvicorn

    config = uvicorn.Config(
        "backend.main:app",
        host=args.host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # 把手里的 socket 直接交给 uvicorn，避免关闭后再 bind 的竞态
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()