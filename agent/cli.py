"""交互界面与渲染（rich）。

分工（README「设计决策」第 24–27 条）：

- 本模块只做**界面**：把已经完整拿到的回复分段打印、把表格渲染出来；
  它不参与推理，也**不引入 ``stream=True``**（P0 固定非流式）。
- 所有**模型产出**的文本一律包成 ``rich.text.Text`` 再打印，不做 markup 解析 ——
  否则模型输出里的 ``[red]`` 之类会被当成富文本标记（轻则变色，重则抛异常）。
- ``Console()`` 不强制终端：重定向到文件时 rich 自动关闭颜色，输出是干净的纯文本；
  交互终端里则正常带色。``NO_COLOR`` / ``FORCE_COLOR`` 交给 rich 自己处理。
"""

from __future__ import annotations

import os
import re
import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .config import KNOWN_TOOLS, AppConfig, ConfigError, ModelConfig
from .loop import AgentLoop, LoopResult
from .provider import Provider, ProviderError, ToolCall
from .registry import ERROR_PREFIX, ToolRegistry, build_registry
from .storage import SessionRecord, Storage, StorageError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INTERRUPTED = 130

PROMPT = "你 > "
ARG_SUMMARY_LIMIT = 60
RESULT_SUMMARY_LIMIT = 70
TITLE_LIMIT = 30
ID_LIMIT = 8

ROLE_STYLES = {
    "system": "dim",
    "user": "cyan",
    "assistant": "green",
    "tool": "yellow",
}

#: 默认禁用、需要显式配置白名单才安全的工具
DEFAULT_DISABLED_TOOLS = {"run_shell", "http_get"}


def install_interrupt_handlers() -> None:
    """让 Ctrl+Break 与 Ctrl+C 行为一致（Windows）。

    这是真实进程手测暴露的问题：Windows 上 Python 只为 SIGINT（=Ctrl+C）装了
    ``default_int_handler``，**SIGBREAK（=Ctrl+Break）没有**，而 CRT 对未处理的
    SIGBREAK 默认动作是**直接终止进程** —— 用户按 Ctrl+Break 会把整个 agent 打死，
    绕过所有 KeyboardInterrupt 处理。这里显式把 SIGBREAK 也接到同一个处理器上。
    """
    if os.name != "nt":
        return
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is None:  # pragma: no cover - 非 Windows
        return
    try:
        signal.signal(sigbreak, signal.default_int_handler)
    except (ValueError, OSError):  # pragma: no cover - 非主线程等场景
        pass


# ----------------------------------------------------------------- 小工具
def _clip(text: str, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_timestamp(iso: str) -> str:
    """把 ISO 时间压成 16 字符（YYYY-MM-DD HH:MM）。

    秒和微秒对「最近会话」没有信息量，但在 80 列终端里值 3 格宽度 ——
    表格能否不折行就差这几个字符（实测见 README 第 26 条）。
    """
    text = (iso or "").replace("T", " ")
    return text[:16]


def summarize_arguments(arguments: str, limit: int = ARG_SUMMARY_LIMIT) -> str:
    """工具参数摘要：折叠空白 + 截断，保证一行放得下。"""
    text = " ".join((arguments or "").split())
    return _clip(text, limit)


def key_is_set(model: ModelConfig, environ: Mapping[str, str] | None = None) -> bool:
    """只看环境变量是否存在/非空，绝不读取或返回它的值。"""
    env = os.environ if environ is None else environ
    return bool(str(env.get(model.api_key_env, "")).strip())


def key_status_text(model: ModelConfig, environ: Mapping[str, str] | None = None) -> Text:
    status = Text()
    status.append(f"{model.api_key_env}: ")
    if key_is_set(model, environ):
        status.append("已设置", style="green")
    else:
        status.append("未设置", style="red")
    return status


# ----------------------------------------------------------------- 输出
def print_segments(console: Console, content: str) -> None:
    """把**已经完整拿到**的回复按空行分段打印（不是流式）。"""
    text = (content or "").strip()
    if not text:
        return
    blocks = [block for block in re.split(r"\n\s*\n", text) if block.strip()]
    for index, block in enumerate(blocks):
        if index:
            console.print()
        console.print(Text(block))


def print_tool_call(console: Console, call: ToolCall) -> None:
    line = Text("🔧 调用 ", style="bold")
    line.append(f"{call.name}({summarize_arguments(call.arguments)})", style="bold magenta")
    console.print(line)


def print_tool_result(console: Console, call: ToolCall, output: str) -> None:
    body = (output or "").strip()
    first_line = body.splitlines()[0] if body else "（无输出）"
    style = "yellow" if body.startswith(ERROR_PREFIX) else "dim"
    line = Text("   ↳ ", style="dim")
    line.append(_clip(first_line, RESULT_SUMMARY_LIMIT), style=style)
    console.print(line)


def print_usage(console: Console, usage: Any, elapsed: float) -> None:
    console.print(
        Text(
            f"   tokens: prompt={usage.prompt_tokens} completion={usage.completion_tokens} "
            f"total={usage.total_tokens}  用时 {elapsed:.1f}s",
            style="dim",
        )
    )


def render_sessions_table(console: Console, records: Sequence[SessionRecord]) -> None:
    if not records:
        console.print(Text("（还没有任何会话，先跑一次 agent run \"你好\"）", style="dim"))
        return
    # 列宽必须让表格的「最小宽度」≤ 终端宽度，否则 rich 会压缩列、把内容截成 abcdef…
    # pad_edge=False + 固定列宽是实测出来的组合（README 第 26 条）。
    table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=False, pad_edge=False)
    table.add_column("id", width=ID_LIMIT, no_wrap=True, overflow="ellipsis", style="cyan")
    table.add_column("标题", max_width=TITLE_LIMIT, no_wrap=True, overflow="ellipsis")
    table.add_column("更新时间", width=16, no_wrap=True, overflow="ellipsis")
    table.add_column("消息", width=4, justify="right", no_wrap=True)
    table.add_column("tokens", width=7, justify="right", no_wrap=True)
    for record in records:
        table.add_row(
            record.id[:ID_LIMIT],
            _clip(record.title or "(未命名)", TITLE_LIMIT),
            format_timestamp(record.updated_at),
            str(record.message_count),
            str(record.total_tokens),
        )
    console.print(table)


def render_models_table(
    console: Console, config: AppConfig, environ: Mapping[str, str] | None = None
) -> None:
    """模型表格：4 列全部完整显示，**不显示密钥值**。

    ``base_url`` 特意**不放进表格**：``no_wrap`` 列的最小宽度等于其最长内容，
    而 provider 的端点动辄 40+ 字符（``https://dashscope.aliyuncs.com/compatible-mode/v1``），
    留在表里会把整表最小宽度顶到 100+，80 列终端下所有列一起被压缩（名称、model、密钥全变 "…"）。
    端点改在表下一行按行展示，长就自然换行，不影响表格。
    """
    table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=False, pad_edge=False)
    table.add_column("名称", width=12, no_wrap=True, overflow="ellipsis", style="cyan")
    table.add_column("model", max_width=30, no_wrap=True, overflow="ellipsis")
    table.add_column("api_key_env", width=18, no_wrap=True, overflow="ellipsis")
    table.add_column("密钥", width=8, no_wrap=True)
    for name, model in config.models.items():
        label = f"* {name}" if name == config.default_model else f"  {name}"
        status = (
            Text("已设置", style="green")
            if key_is_set(model, environ)
            else Text("未设置", style="red")
        )
        table.add_row(label, model.model, model.api_key_env, status)
    console.print(table)

    base_urls = " · ".join(f"{name}={model.base_url}" for name, model in config.models.items())
    console.print(Text(f"base_url  {base_urls}", style="dim"))
    parameters = " · ".join(
        f"{name}: t={model.temperature} max_tokens={model.max_tokens} ctx={model.context_window}"
        for name, model in config.models.items()
    )
    console.print(Text(f"参数      {parameters}", style="dim"))
    console.print(Text("* = default_model；只显示环境变量名，不显示密钥值", style="dim"))


def render_tools_table(console: Console, config: AppConfig) -> None:
    enabled = set(config.tools.enabled)
    table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=False, pad_edge=False)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("工具", width=12, no_wrap=True, style="cyan")
    table.add_column("状态", max_width=24, no_wrap=True, overflow="ellipsis")
    table.add_column("说明", no_wrap=True, overflow="ellipsis")
    descriptions = {
        "read_file": "读取 workspace 内文件",
        "write_file": "写入 workspace 内文件",
        "list_dir": "列出 workspace 内目录",
        "run_shell": f"执行命令（白名单：{config.tools.run_shell.allowed_commands or '空'}）",
        "http_get": f"GET 白名单域名（{config.tools.http_get.allowed_domains or '空'}）",
        # 新增工具时这里也要加一行，否则 tools list 的「说明」列是空的
        "get_time": "返回当前时间（示例工具）",
    }
    for name in KNOWN_TOOLS:
        is_on = name in enabled
        marks = Text()
        marks.append("✓ 启用" if is_on else "✗ 未启用", style="green" if is_on else "dim")
        if name in DEFAULT_DISABLED_TOOLS:
            marks.append("  默认禁用", style="yellow")
        table.add_row(Text(" "), name, marks, descriptions.get(name, ""))
    console.print(table)
    console.print(
        Text("启用哪些工具由 config.yaml 的 tools.enabled 决定；空白名单 = 什么都跑不了。", style="dim")
    )


def render_config_check(
    console: Console,
    config: AppConfig,
    model_name: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    """打印将要使用的配置。**任何分支都不会输出密钥值。**"""
    model = config.get_model(model_name)
    agent = config.agent

    rows: list[tuple[str, Any]] = [
        ("配置文件", str(config.source_path or "(直接构造，无文件)")),
        ("默认模型", config.default_model),
        ("当前模型", f"{model_name} (model={model.model})"),
        ("base_url", model.base_url),
        ("api_key_env", key_status_text(model, environ)),
        ("temperature", str(model.temperature)),
        ("max_tokens", str(model.max_tokens)),
        ("context_window", str(model.context_window)),
        ("request_timeout", f"{agent.request_timeout:g}s"),
        ("max_retries", str(agent.max_retries)),
        ("max_tool_rounds", str(agent.max_tool_rounds)),
        ("max_history_messages", str(agent.max_history_messages)),
        ("workspace", str(config.workspace_path)),
        ("db_path", str(config.db_path)),
        ("log_file", f"{config.log_path} ({config.logging.level})"),
        ("启用工具", ", ".join(config.tools.enabled) or "(无)"),
        ("系统提示词", _clip(config.system_prompt_text(), 50)),
    ]

    table = Table(box=None, show_header=False, padding=(0, 1), expand=False, pad_edge=False)
    table.add_column("项目", style="bold", no_wrap=True)
    table.add_column("值", overflow="fold")
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)


def print_help(console: Console) -> None:
    table = Table(box=box.SIMPLE_HEAD, padding=(0, 1), expand=False)
    table.add_column("命令", no_wrap=True, style="cyan")
    table.add_column("作用")
    for command, description in (
        ("/exit", "退出程序（等同于 Ctrl+D）"),
        ("/new", "新建会话（当前会话已保存，不会丢）"),
        ("/model <name>", "切换模型，写进当前会话并下次请求生效"),
        ("/history", "从数据库读当前会话的完整消息并渲染"),
        ("/tools", "查看工具启用状态"),
        ("/help", "显示这份帮助"),
    ):
        table.add_row(command, description)
    console.print(table)
    console.print(Text("Ctrl+C 只中断当前这次请求，不会退出程序。", style="dim"))


# ----------------------------------------------------------------- 会话
@dataclass
class ChatSession:
    """一次交互会话：provider + registry + loop + 持久化的绑定。"""

    config: AppConfig
    storage: Storage
    session_id: str
    model_name: str
    console: Console
    provider: Provider = field(init=False)
    registry: ToolRegistry = field(init=False)
    loop: AgentLoop = field(init=False)

    def __post_init__(self) -> None:
        self._rebuild(reset_history=False)

    # ---- 构造 -----------------------------------------------------------
    @classmethod
    def open(
        cls,
        config: AppConfig,
        console: Console,
        *,
        session_id: str | None = None,
        model_name: str | None = None,
    ) -> "ChatSession":
        storage = Storage(config.db_path)
        if session_id:
            # 允许用短 id 前缀（会话列表只显示 8 位），唯一前缀即可恢复
            record = storage.find_session(session_id)
            active_model = model_name or record.model
        else:
            active_model = model_name or config.default_model
            record = storage.create_session(model=active_model)
        return cls(
            config=config,
            storage=storage,
            session_id=record.id,
            model_name=active_model,
            console=console,
        )

    def _rebuild(self, *, reset_history: bool) -> None:
        self.registry = build_registry(self.config)
        self.provider = Provider.from_config(self.config, self.model_name)
        history: list[dict[str, Any]] | None = None
        if not reset_history and self.storage.get_session(self.session_id):
            if self.storage.count_messages(self.session_id):
                history = self.storage.get_messages(self.session_id)
        self.loop = AgentLoop.from_config(
            self.config,
            self.provider,
            self.registry,
            storage=self.storage,
            session_id=self.session_id,
            history=history,
            on_tool_call=lambda call: print_tool_call(self.console, call),
            on_tool_result=lambda call, output: print_tool_result(self.console, call, output),
        )

    # ---- 操作 -----------------------------------------------------------
    @property
    def model(self) -> ModelConfig:
        return self.config.get_model(self.model_name)

    def switch_model(self, name: str) -> None:
        """切换模型：校验 → 更新会话记录 → 重建 provider/loop（历史保留）。"""
        self.config.get_model(name)  # 未定义时抛 ConfigError，这里不改任何状态
        self.model_name = name
        self.provider.close()
        self._rebuild(reset_history=False)
        self.storage.touch_session(self.session_id, model=name)

    def start_new_session(self) -> str:
        record = self.storage.create_session(model=self.model_name)
        self.session_id = record.id
        self._rebuild(reset_history=True)
        return record.id

    def history(self) -> list[dict[str, Any]]:
        return self.storage.get_messages(self.session_id)

    def record(self) -> SessionRecord:
        return self.storage.require_session(self.session_id)

    def close(self) -> None:
        try:
            self.provider.close()
        finally:
            self.storage.close()


# ----------------------------------------------------------------- 一轮对话
def run_turn(session: ChatSession, console: Console, question: str) -> int:
    """跑一轮问答，返回退出码（交互模式忽略它，`run` 子命令用它）。"""
    started = time.monotonic()
    try:
        result: LoopResult = session.loop.run(question)
    except KeyboardInterrupt:
        repaired = session.loop.abort_pending_tool_calls(
            "用户按 Ctrl+C 中断了本次请求，该工具未执行"
        )
        console.print()
        message = "已中断"
        if repaired:
            message += f"（补全了 {repaired} 条未执行的工具结果，历史仍然合法）"
        console.print(Text(message, style="yellow"))
        return EXIT_INTERRUPTED
    except ProviderError as exc:
        console.print()
        console.print(Text(f"模型调用失败：{exc}", style="red"))
        return EXIT_ERROR

    if result.content:
        print_segments(console, result.content)
    print_usage(console, result.usage, time.monotonic() - started)
    if result.tool_limit_reached:
        console.print(Text("（本轮因达到工具调用上限而结束）", style="yellow"))
    return EXIT_OK


# ----------------------------------------------------------------- 斜杠命令
def handle_slash_command(line: str, session: ChatSession, console: Console) -> bool:
    """返回 False 表示要退出交互模式。"""
    command, _, argument = line.partition(" ")
    command = command.strip().lower()
    argument = argument.strip()

    if command == "/exit":
        console.print(Text("已退出。", style="dim"))
        return False

    if command == "/help":
        print_help(console)
        return True

    if command == "/new":
        old_id = session.session_id
        new_id = session.start_new_session()
        console.print(
            Text(f"已新建会话 {new_id[:ID_LIMIT]}", style="green")
            + Text(f"（旧会话 {old_id[:ID_LIMIT]} 已保存，随时可用 sessions resume 恢复）", style="dim")
        )
        return True

    if command == "/model":
        if not argument:
            console.print(
                Text(f"当前模型：{session.model_name}（{session.model.model}）", style="cyan")
            )
            console.print(Text(f"可用模型：{', '.join(session.config.models)}", style="dim"))
            return True
        try:
            session.switch_model(argument)
        except ConfigError as exc:
            console.print(Text(f"切换失败：{exc}", style="red"))
            console.print(Text(f"可用模型：{', '.join(session.config.models)}", style="dim"))
            return True
        console.print(
            Text(f"已切换到模型 {session.model_name}（{session.model.model}），下次请求生效", style="green")
        )
        return True

    if command == "/history":
        messages = session.history()
        if not messages:
            console.print(Text("（当前会话还没有消息）", style="dim"))
            return True
        if len(messages) == 1 and messages[0].get("role") == "system":
            console.print(Text("（当前会话只有系统提示词，还没有对话内容）", style="dim"))
            print_messages(console, messages)
            return True
        console.print(
            Text(f"会话 {session.session_id}（{len(messages)} 条消息，来自数据库）", style="dim")
        )
        print_messages(console, messages)
        return True

    if command == "/tools":
        render_tools_table(console, session.config)
        return True

    console.print(Text(f"未知命令 {command}，输入 /help 查看可用命令。", style="yellow"))
    return True


# ----------------------------------------------------------------- 渲染消息
def print_messages(console: Console, messages: Sequence[Mapping[str, Any]]) -> None:
    for index, message in enumerate(messages, start=1):
        role = str(message.get("role", "?"))
        header = Text(f"[{index}] {role}", style=ROLE_STYLES.get(role, "white"))
        if role == "tool":
            header.append(f"  tool_call_id={message.get('tool_call_id')}", style="dim")
            if message.get("name"):
                header.append(f"  name={message['name']}", style="dim")
        console.print(header)

        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            console.print(
                Text(
                    f"    → {function.get('name')}"
                    f"({summarize_arguments(str(function.get('arguments') or ''))})"
                    f"  id={call.get('id')}",
                    style="magenta",
                )
            )

        content = message.get("content")
        if content:
            for line in str(content).splitlines() or [""]:
                console.print(Text("    " + line))
    console.print()


# ----------------------------------------------------------------- 交互循环
def chat_loop(
    session: ChatSession,
    console: Console,
    *,
    reader: Callable[[str], str] | None = None,
) -> int:
    """交互模式。

    - **Ctrl+C 只中断当前请求**：捕获 ``KeyboardInterrupt`` 后补全未完成的工具结果、
      打印「已中断」并回到输入提示符；只有 ``/exit`` 或 Ctrl+D（EOF）才退出。
    - 空回车直接忽略，不发给模型。
    """
    read = reader or (lambda prompt: console.input(prompt))

    console.print(
        Text(f"会话 {session.session_id[:ID_LIMIT]}", style="cyan")
        + Text(f" · 模型 {session.model_name} ({session.model.model})", style="cyan")
        + Text(f" · 工具 {', '.join(session.registry.names()) or '(无)'}", style="dim")
    )
    console.print(Text("输入 /help 查看命令，/exit 或 Ctrl+D 退出；Ctrl+C 只中断当前请求。", style="dim"))

    while True:
        try:
            raw = read(PROMPT)
        except EOFError:
            console.print()
            console.print(Text("已退出（Ctrl+D）。", style="dim"))
            return EXIT_OK
        except KeyboardInterrupt:
            console.print()
            console.print(Text("已中断（不会退出；/exit 或 Ctrl+D 才退出）", style="yellow"))
            continue

        line = (raw or "").strip()
        if not line:
            continue  # 空回车忽略

        if line.startswith("/"):
            if not handle_slash_command(line, session, console):
                return EXIT_OK
            continue

        run_turn(session, console, line)