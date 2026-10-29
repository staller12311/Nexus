"""命令行入口：argparse 子命令。

子命令一览（README「运行」章节有示例）：

    agent run "问题"            单次提问后退出（退出码：成功 0，失败非 0）
    agent chat                  交互式对话（/exit、Ctrl+D 退出；Ctrl+C 只中断当前请求）
    agent sessions list|show|resume
    agent models list
    agent tools list
    agent config check          打印将要使用的配置（不显示密钥明文）

退出码约定：成功 0；配置错误 / 密钥缺失 / 模型调用失败 1；被 Ctrl+C 中断 130；
argparse 用法错误 2（argparse 自带，不捕获）。
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .cli import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    ChatSession,
    chat_loop,
    install_interrupt_handlers,
    key_is_set,
    render_config_check,
    render_models_table,
    render_sessions_table,
    render_tools_table,
    print_messages,
    run_turn,
)
from .config import AppConfig, ConfigError, load_config
from .logging_setup import force_utf8_stdio, setup_logging
from .provider import ProviderError
from .storage import Storage, StorageError


def _common_options() -> argparse.ArgumentParser:
    """``--config`` / ``--model`` 的公共父解析器。

    放进父解析器，是为了让它们在**子命令之前和之后都能用**：
    ``agent --config x.yaml run "你好"`` 与 ``agent run "你好" --config x.yaml`` 等价。
    子解析器里默认值用 ``SUPPRESS``，避免用 None 覆盖父级已经解析到的值。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help="配置文件路径（优先级高于 AGENT_CONFIG 与 ./config.yaml）",
    )
    common.add_argument(
        "--model", default=argparse.SUPPRESS, help="覆盖 config 里的 default_model"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="agent",
        description="可配置的通用 LLM 智能体（OpenAI 兼容 API）",
    )
    parser.add_argument(
        "--config", help="配置文件路径（优先级高于 AGENT_CONFIG 与 ./config.yaml）"
    )
    parser.add_argument("--model", help="覆盖 config 里的 default_model")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="单次提问后退出", parents=[common])
    run_parser.add_argument("question", help="要问的问题，例如 run \"你好\"")

    chat_parser = subparsers.add_parser("chat", help="交互式对话", parents=[common])
    chat_parser.add_argument("--session", help="恢复指定会话 id（等同 sessions resume）")
    chat_parser.add_argument("--new", action="store_true", help="强制新建会话")

    sessions_parser = subparsers.add_parser("sessions", help="会话管理", parents=[common])
    sessions_sub = sessions_parser.add_subparsers(dest="session_command", required=True)
    list_parser = sessions_sub.add_parser("list", help="列出最近会话", parents=[common])
    list_parser.add_argument("--limit", type=int, default=20, help="最多列出多少条（默认 20）")
    show_parser = sessions_sub.add_parser(
        "show", help="显示某个会话的完整消息", parents=[common]
    )
    show_parser.add_argument("session_id")
    resume_parser = sessions_sub.add_parser(
        "resume", help="恢复某个会话并进入交互模式", parents=[common]
    )
    resume_parser.add_argument("session_id")

    models_parser = subparsers.add_parser("models", help="模型管理", parents=[common])
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_sub.add_parser("list", help="列出已配置的模型", parents=[common])

    tools_parser = subparsers.add_parser("tools", help="工具管理", parents=[common])
    tools_sub = tools_parser.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("list", help="列出全部工具及其启用状态", parents=[common])

    config_parser = subparsers.add_parser("config", help="配置管理", parents=[common])
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help="检查配置并打印将要使用的参数", parents=[common])

    return parser


def _prepare(args: argparse.Namespace) -> tuple[AppConfig, object]:
    """加载配置并初始化日志。抛出 ConfigError 由调用方处理。"""
    config = load_config(getattr(args, "config", None))
    setup_logging(level=config.logging.level, log_file=config.log_path)
    return config


def main(argv: Sequence[str] | None = None) -> int:
    force_utf8_stdio()  # Windows 代码页 936 下不设会中文乱码
    install_interrupt_handlers()  # Windows：让 Ctrl+Break 与 Ctrl+C 一样只中断当前请求

    from rich.console import Console

    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    model_name = getattr(args, "model", None)

    try:
        config = _prepare(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return _dispatch(args, config, console, model_name)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except StorageError as exc:
        print(f"会话错误：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except ProviderError as exc:
        print(f"模型调用失败：{exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED


def _dispatch(
    args: argparse.Namespace, config: AppConfig, console: object, model_name: str | None
) -> int:
    command = args.command

    if command == "config" and args.config_command == "check":
        return _config_check(config, console, model_name)

    if command == "models" and args.models_command == "list":
        render_models_table(console, config)
        return EXIT_OK

    if command == "tools" and args.tools_command == "list":
        render_tools_table(console, config)
        return EXIT_OK

    if command == "sessions":
        with Storage(config.db_path) as storage:
            if args.session_command == "list":
                render_sessions_table(console, storage.list_sessions(limit=args.limit))
                return EXIT_OK
            if args.session_command == "show":
                record = storage.find_session(args.session_id)  # 支持唯一短前缀
                render_sessions_table(console, [record])
                print_messages(console, storage.get_messages(record.id))
                return EXIT_OK
            if args.session_command == "resume":
                session = ChatSession.open(
                    config, console, session_id=args.session_id, model_name=model_name
                )
                try:
                    return chat_loop(session, console)
                finally:
                    session.close()

    if command == "run":
        session = ChatSession.open(config, console, model_name=model_name)
        try:
            return run_turn(session, console, args.question)
        finally:
            session.close()

    if command == "chat":
        session_id = getattr(args, "session", None)
        if getattr(args, "new", False):
            session_id = None
        session = ChatSession.open(config, console, session_id=session_id, model_name=model_name)
        try:
            return chat_loop(session, console)
        finally:
            session.close()

    parser_error = f"未知命令组合：{command}"  # pragma: no cover - argparse 已拦
    print(parser_error, file=sys.stderr)
    return EXIT_ERROR


def _config_check(config: AppConfig, console: object, model_name: str | None) -> int:
    """打印配置检查结果；密钥缺失算失败（错误走 stderr，退出码非 0）。"""
    selected = model_name or config.default_model
    try:
        model = config.get_model(selected)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ERROR

    render_config_check(console, config, selected)

    if not key_is_set(model):
        print(
            f"配置检查失败：环境变量 {model.api_key_env} 未设置或为空"
            f"（模型 {selected} 需要它）。请复制 .env.example 为 .env 并填入真实密钥。",
            file=sys.stderr,
        )
        return EXIT_ERROR

    console.print("[green]配置检查通过[/green]")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())