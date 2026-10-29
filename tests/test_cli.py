"""阶段 6：CLI 层测试。

覆盖：子命令齐全、config check 不泄密钥与退出码、sessions/models/tools 渲染、
斜杠命令边界、Ctrl+C 只中断当前请求（不退出）、重定向后无 ANSI 转义。

端到端部分复用 ``test_mock_server`` 里的真实 http.server 端点。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pytest
import yaml
from rich.console import Console

import test_mock_server as mock_server
from agent import cli
from agent.__main__ import main
from agent.config import build_config, load_config
from agent.provider import ChatResult, Provider
from agent.registry import ToolRegistry, ToolSpec
from agent.storage import SessionRecord, Storage, StorageError
from agent.tools import ToolError

CLI_KEY_ENV = "CLI_TEST_API_KEY"
FAKE_KEY = "sk-cli-should-never-be-printed-9876"
REPO_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 夹具
@pytest.fixture()
def api_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(CLI_KEY_ENV, FAKE_KEY)
    created: list[mock_server.ScriptedOpenAIServer] = []

    def make(
        script: Iterable[dict[str, Any]] = (),
        *,
        default: dict[str, Any] | None = None,
        delay: float = 0.0,
    ):
        server = mock_server.ScriptedOpenAIServer(script, default=default, delay=delay).start()
        created.append(server)
        return server

    yield make
    for server in created:
        server.stop()


def write_config(config_dir: Path, base_url: str, **overrides: Any) -> Path:
    """在临时目录里写一份配置文件；相对路径都以该目录为基准。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "workspace").mkdir(exist_ok=True)
    data: dict[str, Any] = {
        "default_model": "mock",
        "models": {
            "mock": {
                "base_url": base_url,
                "model": "mock-model",
                "api_key_env": CLI_KEY_ENV,
                "temperature": 0.0,
                "max_tokens": 64,
                "context_window": 4096,
            },
            "alt": {
                "base_url": base_url,
                "model": "alt-model",
                "api_key_env": CLI_KEY_ENV,
            },
            "nokey": {
                "base_url": base_url,
                "model": "nokey-model",
                "api_key_env": "CLI_DEFINITELY_MISSING_KEY",
            },
        },
        "agent": {
            "workspace": "workspace",
            "system_prompt": "你是一个严谨、简洁的助手。",
            "request_timeout": 5,
        },
        "storage": {"db_path": "data/sessions.db"},
        "logging": {"file": "data/agent.log", "level": "WARNING"},
    }
    data.update(overrides)
    path = config_dir / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def completion(content: str | None = "回答", **kwargs: Any) -> dict[str, Any]:
    return mock_server.chat_completion(content, model="mock-model", **kwargs)


def tool_call_step(name: str, arguments: str, call_id: str = "call_1") -> dict[str, Any]:
    return completion(
        None,
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
        finish_reason="tool_calls",
    )


def text_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def drive_chat(monkeypatch: pytest.MonkeyPatch, inputs: Sequence[Any]):
    """把 Console.input 换成脚本化输入（元素可以是字符串或要抛出的异常）。"""
    iterator = iter(inputs)
    prompts: list[str] = []

    def fake_input(self: Console, prompt: str = "", **kwargs: Any) -> str:
        prompts.append(prompt)
        try:
            item = next(iterator)
        except StopIteration:
            raise EOFError
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(Console, "input", fake_input)
    return prompts


# ============================================================ config check
class TestConfigCheck:
    def test_success_exit_code_and_no_secret_leak(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)

        code = main(["--config", str(config_path), "config", "check"])
        captured = capsys.readouterr()

        assert code == 0, captured.err
        assert "base_url" in captured.out
        assert "request_timeout" in captured.out
        assert "max_tokens" in captured.out
        assert "stream" in captured.out
        assert f"{CLI_KEY_ENV}: 已设置" in captured.out
        assert "配置检查通过" in captured.out
        # 密钥明文绝不允许出现
        assert FAKE_KEY not in captured.out
        assert FAKE_KEY not in captured.err
        assert "api_key_env" in captured.out

    def test_missing_key_is_nonzero_and_goes_to_stderr(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(
            tmp_path / "conf", server.base_url, default_model="nokey"
        )

        code = main(["--config", str(config_path), "config", "check"])
        captured = capsys.readouterr()

        assert code != 0
        assert "CLI_DEFINITELY_MISSING_KEY" in captured.err
        assert "未设置" in captured.out, "仍应打印配置表，方便排查"
        assert FAKE_KEY not in captured.out + captured.err

    def test_invalid_config_is_nonzero_and_goes_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_dir = tmp_path / "bad"
        config_dir.mkdir()
        path = config_dir / "config.yaml"
        path.write_text(
            "default_model: ghost\nmodels:\n  mock:\n    base_url: http://x/v1\n"
            "    model: m\n    api_key_env: K\n",
            encoding="utf-8",
        )

        code = main(["--config", str(path), "config", "check"])
        captured = capsys.readouterr()
        assert code != 0
        assert "配置校验失败" in captured.err
        assert "ghost" in captured.err

    def test_missing_config_file_is_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--config", str(tmp_path / "nope.yaml"), "config", "check"])
        captured = capsys.readouterr()
        assert code != 0
        assert "找不到配置文件" in captured.err


# ============================================================ run
class TestRunCommand:
    def test_run_success(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server([completion("你好，我是助手。", usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})])
        config_path = write_config(tmp_path / "conf", server.base_url)

        code = main(["--config", str(config_path), "run", "你好"])
        captured = capsys.readouterr()

        assert code == 0
        assert "你好，我是助手。" in captured.out
        assert "total=7" in captured.out
        assert len(server.requests) == 1
        assert server.requests[0]["body"]["messages"][-1]["content"] == "你好"

    def test_run_executes_tool_and_shows_tool_line(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(
            [
                tool_call_step("read_file", '{"path": "note.txt"}'),
                completion("note.txt 的内容是 hello"),
            ]
        )
        config_path = write_config(tmp_path / "conf", server.base_url)
        (tmp_path / "conf" / "workspace" / "note.txt").write_text("hello", encoding="utf-8")

        code = main(["--config", str(config_path), "run", "读 note.txt"])
        captured = capsys.readouterr()

        assert code == 0
        assert "🔧 调用 read_file" in captured.out
        assert "note.txt 的内容是 hello" in captured.out
        assert len(server.requests) == 2

    def test_run_provider_error_exits_nonzero(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(
            default={
                "status": 401,
                "json": mock_server.error_body("Invalid API key"),
            }
        )
        config_path = write_config(tmp_path / "conf", server.base_url)

        code = main(["--config", str(config_path), "run", "你好"])
        captured = capsys.readouterr()

        assert code != 0
        assert "模型调用失败" in captured.out
        assert "Traceback" not in captured.out

    def test_missing_key_exits_nonzero(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url, default_model="nokey")

        code = main(["--config", str(config_path), "run", "你好"])
        captured = capsys.readouterr()

        assert code != 0
        assert "CLI_DEFINITELY_MISSING_KEY" in captured.err
        assert len(server.requests) == 0

    def test_run_with_unknown_model_exits_nonzero(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)

        code = main(["--config", str(config_path), "--model", "ghost", "run", "你好"])
        captured = capsys.readouterr()
        assert code != 0
        assert "ghost" in captured.err


# ============================================================ sessions
class TestSessionCommands:
    def test_sessions_list_and_show(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(
            [
                tool_call_step("read_file", '{"path": "note.txt"}'),
                completion("读完了"),
            ]
        )
        config_path = write_config(tmp_path / "conf", server.base_url)
        (tmp_path / "conf" / "workspace" / "note.txt").write_text("hello", encoding="utf-8")

        assert main(["--config", str(config_path), "run", "读 note.txt"]) == 0
        session_id = Storage(tmp_path / "conf" / "data" / "sessions.db").list_sessions()[0].id

        assert main(["--config", str(config_path), "sessions", "list"]) == 0
        listing = capsys.readouterr().out
        assert session_id[:8] in listing
        assert "消息" in listing
        assert "tokens" in listing

        assert main(["--config", str(config_path), "sessions", "show", session_id]) == 0
        shown = capsys.readouterr().out
        assert "[1] system" in shown
        assert "[2] user" in shown
        assert "[3] assistant" in shown
        assert "tool_call_id=call_1" in shown
        assert "读完了" in shown

    def test_sessions_show_accepts_unique_short_id(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """列表只显示 8 位短 id，show/resume 必须能用这个短 id（真实手测发现的坑）。"""
        server = api_server(default=completion("短 id 可用"))
        config_path = write_config(tmp_path / "conf", server.base_url)
        assert main(["--config", str(config_path), "run", "你好"]) == 0
        capsys.readouterr()

        with Storage(tmp_path / "conf" / "data" / "sessions.db") as storage:
            full_id = storage.list_sessions()[0].id
        short_id = full_id[:8]

        assert main(["--config", str(config_path), "sessions", "show", short_id]) == 0
        assert "短 id 可用" in capsys.readouterr().out

    def test_sessions_show_rejects_ambiguous_prefix(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)
        with Storage(tmp_path / "conf" / "data" / "sessions.db") as storage:
            storage.create_session("m", session_id="dup123aaaaaa")
            storage.create_session("m", session_id="dup123bbbbbb")

        code = main(["--config", str(config_path), "sessions", "show", "dup123"])
        captured = capsys.readouterr()
        assert code != 0
        assert "不唯一" in captured.err
        assert "dup123aaaaaa" in captured.err and "dup123bbbbbb" in captured.err

    def test_config_position_before_and_after_subcommand(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--config 放在子命令前后都必须可用（曾只支持前置，用户复制命令必然踩坑）。"""
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)

        assert main(["--config", str(config_path), "sessions", "list"]) == 0
        assert main(["sessions", "list", "--config", str(config_path)]) == 0
        assert main(["sessions", "list", "--config", str(config_path), "--limit", "5"]) == 0

        # 后置 --config 必须真的生效（指向另一个库 → 会话列表为空）
        other = write_config(tmp_path / "other", server.base_url)
        assert main(["sessions", "list", "--config", str(other)]) == 0
        assert "还没有任何会话" in capsys.readouterr().out

    def test_model_option_position(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server([completion("用 alt 回答")])
        config_path = write_config(tmp_path / "conf", server.base_url)

        code = main(["run", "你好", "--config", str(config_path), "--model", "alt"])
        assert code == 0
        assert "用 alt 回答" in capsys.readouterr().out
        assert server.requests[0]["body"]["model"] == "alt-model"

    def test_sessions_works_without_api_key(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)
        monkeypatch.delenv(CLI_KEY_ENV, raising=False)

        assert main(["--config", str(config_path), "sessions", "list"]) == 0
        assert "还没有任何会话" in capsys.readouterr().out

    def test_sessions_show_unknown_id_is_friendly(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)

        code = main(["--config", str(config_path), "sessions", "show", "nope1234"])
        captured = capsys.readouterr()
        assert code != 0
        assert "nope1234" in captured.err
        assert "找不到会话" in captured.err
        assert "Traceback" not in captured.err

    def test_sessions_resume_unknown_id_is_friendly(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)
        code = main(["--config", str(config_path), "sessions", "resume", "ghost"])
        captured = capsys.readouterr()
        assert code != 0
        assert "找不到会话" in captured.err


# ============================================================ models / tools
class TestModelsAndToolsList:
    def test_models_list_hides_key_values(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)

        assert main(["--config", str(config_path), "models", "list"]) == 0
        out = capsys.readouterr().out
        assert "mock" in out and "alt" in out and "nokey" in out
        # 表格里 api_key_env 与状态分列显示；超长变量名会被省略号截断（完整值见 config check）
        assert CLI_KEY_ENV in out
        assert "CLI_DEFINITELY" in out
        assert "已设置" in out and "未设置" in out
        assert FAKE_KEY not in out

    def test_tools_list_shows_all_with_marks(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)

        assert main(["--config", str(config_path), "tools", "list"]) == 0
        out = capsys.readouterr().out
        for name in ("read_file", "write_file", "list_dir", "run_shell", "http_get"):
            assert name in out
        assert "✓ 启用" in out
        assert "✗ 未启用" in out
        assert "默认禁用" in out


# ============================================================ 斜杠命令
class TestSlashCommands:
    def test_full_command_flow(
        self,
        api_server: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = api_server(default=completion("回答一"))
        config_path = write_config(tmp_path / "conf", server.base_url)
        drive_chat(
            monkeypatch,
            [
                "",  # 空回车：必须被忽略
                "/help",
                "/foo",  # 未知命令
                "/tools",
                "/history",  # 空会话
                "/model",  # 查看当前
                "/model nope",  # 未知名：报错不切换
                "/model alt",  # 正常切换
                "你好",  # 真请求（应使用 alt）
                "/history",
                "/new",
                "/model",
                "/exit",
            ],
        )

        code = main(["--config", str(config_path), "chat"])
        out = capsys.readouterr().out

        assert code == 0
        assert "/exit" in out  # /help 内容
        assert "未知命令 /foo" in out
        assert "（当前会话只有系统提示词，还没有对话内容）" in out
        assert "切换失败" in out  # /model nope
        assert "已切换到模型 alt" in out
        assert "回答一" in out
        assert "已新建会话" in out
        assert "旧会话" in out and "已保存" in out

        assert len(server.requests) == 1, "空回车与斜杠命令都不该发出请求"
        assert server.requests[0]["body"]["model"] == "alt-model", "/model 必须对下一次请求生效"

    def test_ctrl_c_at_prompt_does_not_exit(
        self,
        api_server: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)
        prompts = drive_chat(monkeypatch, [KeyboardInterrupt(), "/exit"])

        code = main(["--config", str(config_path), "chat"])
        out = capsys.readouterr().out

        assert code == 0, "Ctrl+C 绝不能退出程序"
        assert "已中断（不会退出" in out
        assert len(prompts) >= 2, "中断后必须重新回到输入提示符"

    def test_eof_exits_cleanly(
        self,
        api_server: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)
        drive_chat(monkeypatch, [])  # 立刻 EOF

        code = main(["--config", str(config_path), "chat"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Ctrl+D" in out

    def test_ctrl_c_during_request_keeps_program_alive(
        self,
        api_server: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = api_server(default=completion("第二次成功了"))
        config_path = write_config(tmp_path / "conf", server.base_url)

        # 循环现在走流式核心（run() 内部消费 run_stream → provider.chat_stream），
        # 所以要在 chat_stream 上注入「第一次请求被打断」。
        original_stream = Provider.chat_stream
        state = {"interrupted": False}

        def flaky_stream(self, messages, tools=None, **kwargs):  # type: ignore[no-untyped-def]
            if not state["interrupted"]:
                state["interrupted"] = True
                raise KeyboardInterrupt
            yield from original_stream(self, messages, tools=tools, **kwargs)

        monkeypatch.setattr(Provider, "chat_stream", flaky_stream)
        drive_chat(monkeypatch, ["第一次提问", "第二次提问", "/exit"])

        code = main(["--config", str(config_path), "chat"])
        out = capsys.readouterr().out

        assert code == 0, "请求被 Ctrl+C 打断后程序必须还活着"
        assert "已中断" in out
        assert "第二次成功了" in out, "中断后应能继续提问"
        assert "Traceback" not in out

    def test_interrupt_during_tool_keeps_history_legal(
        self,
        api_server: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """工具执行中被中断：补全未执行的 tool 结果，历史仍然合法。"""
        server = api_server([tool_call_step("boom", "{}")], default=None)
        config_path = write_config(tmp_path / "conf", server.base_url)
        config = load_config(config_path, load_env_file=False)

        def handler(**kwargs: Any) -> str:
            raise KeyboardInterrupt

        registry = ToolRegistry()
        registry.register(ToolSpec("boom", "会模拟 Ctrl+C", text_schema(), handler))
        monkeypatch.setattr(cli, "build_registry", lambda config: registry)

        console = Console()
        session = cli.ChatSession.open(config, console)
        try:
            code = cli.run_turn(session, console, "调用工具")
            out = capsys.readouterr().out

            assert code == cli.EXIT_INTERRUPTED
            assert "已中断" in out
            assert "补全了 1 条" in out

            # 关键：不能留下悬空的 tool_calls
            assert session.loop.pending_tool_calls() == []
            stored = session.history()
            assert stored[-1]["role"] == "tool"
            assert stored[-1]["tool_call_id"] == "call_1"
            assert "中断了本次请求" in stored[-1]["content"]

            # 历史序列合法：每个 tool 都有前驱 assistant(tool_calls)
            seen: set[str] = set()
            for message in stored:
                if message["role"] == "assistant":
                    for call in message.get("tool_calls") or []:
                        seen.add(call["id"])
                elif message["role"] == "tool":
                    assert message["tool_call_id"] in seen
        finally:
            session.close()


# ============================================================ 真实子进程 Ctrl+C
def _wait_until(predicate: Callable[[], bool], timeout: float, message: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"等待超时（{timeout}s）：{message}")


def _send_console_interrupt(proc: subprocess.Popen) -> str:
    """给子进程投递一个**真实**的终端中断。

    Windows：``CTRL_BREAK_EVENT``（需要 CREATE_NEW_PROCESS_GROUP；
    在 POSIX 上按 SIGINT 处理，故非 Windows 分支用 ``send_signal(SIGINT)``）。
    """
    if os.name == "nt":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        return "CTRL_BREAK_EVENT"
    proc.send_signal(signal.SIGINT)
    return "SIGINT"


@pytest.mark.skipif(
    os.name == "nt" and not hasattr(signal, "CTRL_BREAK_EVENT"),
    reason=(
        "Windows 上缺少控制台中断事件 API（signal.CTRL_BREAK_EVENT）；"
        "POSIX 分支改用 SIGINT，不跳过"
    ),
)
def test_ctrl_c_in_real_subprocess_keeps_prompt_alive(
    api_server: Any, tmp_path: Path
) -> None:
    """真实子进程 + 真实中断事件：请求进行中被打断后进程必须存活、提示符必须回来。

    这是「Ctrl+C 只中断当前请求」的端到端证据；上面两条用例是进程内注入，
    只有行为、没有真实信号投递。
    """
    server = api_server(default=completion("中断之后还能继续"), delay=2.0)
    config_path = write_config(tmp_path / "conf", server.base_url)

    env = {
        **os.environ,
        CLI_KEY_ENV: FAKE_KEY,
        "PYTHONIOENCODING": "utf-8",
        # stdout 是管道时 Python 默认块缓冲，提示符会卡在缓冲区里读不到
        "PYTHONUNBUFFERED": "1",
        "COLUMNS": "80",
    }
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent", "--config", str(config_path), "chat"],
        cwd=str(REPO_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        creationflags=creationflags,
    )

    chunks: list[str] = []

    def reader() -> None:
        assert proc.stdout is not None
        while True:
            # 二进制管道 + read1：read(n) 会阻塞到读满 n 字节或 EOF，
            # 而 TextIOWrapper 又没有 read1。提示符 "你 > " 没有换行、总量又小，
            # 用 read(n) 会一直等子进程退出才交付。
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", "replace"))

    threading.Thread(target=reader, daemon=True).start()
    output = lambda: "".join(chunks)  # noqa: E731

    def send(text: str) -> None:
        assert proc.stdin is not None
        proc.stdin.write(text.encode("utf-8") + b"\n")
        proc.stdin.flush()

    try:
        _wait_until(lambda: "你 > " in output(), 20, f"等待启动提示符；当前输出：{output()!r}")
        send("这个请求会停两秒")

        # 等请求真的到达假端点，确保中断落在「请求进行中」
        _wait_until(lambda: len(server.requests) >= 1, 20, "等待子进程发出请求")
        try:
            signal_name = _send_console_interrupt(proc)
        except OSError as exc:  # pragma: no cover - 取决于运行环境有无控制台
            pytest.skip(f"当前环境无法向子进程投递终端中断：{type(exc).__name__}: {exc}")

        _wait_until(lambda: "已中断" in output(), 20, f"{signal_name} 后应打印「已中断」")
        assert proc.poll() is None, f"收到 {signal_name} 后进程必须存活，而不是退出"

        _wait_until(
            lambda: output().count("你 > ") >= 2, 10, "中断后应重新出现输入提示符"
        )

        send("中断之后还能继续吗")
        _wait_until(lambda: "中断之后还能继续" in output(), 25, "中断后应能继续提问")

        send("/exit")
        proc.wait(timeout=20)
        assert proc.returncode == 0, output()

        text = output()
        assert text.count("你 > ") >= 3, "提示符应出现至少三次（启动 / 中断后 / 第二次后）"
        assert "Traceback" not in text
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdin is not None:
            proc.stdin.close()


# ============================================================ 重定向干净
class TestRedirectCleanliness:
    def test_no_ansi_when_not_a_terminal(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        server = api_server([completion("干净输出")])
        config_path = write_config(tmp_path / "conf", server.base_url)

        assert main(["--config", str(config_path), "run", "你好"]) == 0
        assert main(["--config", str(config_path), "sessions", "list"]) == 0
        assert main(["--config", str(config_path), "config", "check"]) == 0
        out = capsys.readouterr().out

        assert "\x1b[" not in out, "重定向到文件时不应带 ANSI 转义码"
        assert "干净输出" in out


# ============================================================ 渲染细节
class TestRendering:
    def test_model_output_is_not_parsed_as_markup(self, capsys: pytest.CaptureFixture[str]) -> None:
        console = Console()
        cli.print_segments(console, "这是 [red]危险[/red] 的模型输出 [not-a-tag]")
        out = capsys.readouterr().out
        assert "[red]危险[/red]" in out
        assert "[not-a-tag]" in out

    def test_argument_summary_is_truncated(self) -> None:
        long_args = json.dumps({"path": "x" * 500})
        summary = cli.summarize_arguments(long_args)
        assert len(summary) <= cli.ARG_SUMMARY_LIMIT
        assert summary.endswith("…")

    def test_sessions_table_truncates_title(self, capsys: pytest.CaptureFixture[str]) -> None:
        record = SessionRecord(
            id="abcdef1234567890",
            title="标" * 100,
            model="m",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            message_count=2,
            total_tokens=99,
        )
        console = Console(width=80)
        cli.render_sessions_table(console, [record])
        out = capsys.readouterr().out
        assert "abcdef12" in out  # id 截 8 位
        assert "标" * cli.TITLE_LIMIT not in out  # 标题被截断
        for line in out.splitlines():
            assert len(line) <= 80, f"80 列下折行错乱：{line!r}"

    def test_models_table_layout_with_long_base_urls(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """回归（第 31 条设计决策）：长 base_url 不得把表格压塌。

        ``no_wrap`` 列的最小宽度等于其最长内容，所以 40+ 字符的端点一旦留在表里，
        80 列终端下所有列都会被截成「…」。这里用 4 个真实长名字 + 长端点固定行为。
        """
        models = {
            "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k", "MOONSHOT_API_KEY"),
            "qwen": (
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "qwen2.5-72b-instruct",
                "DASHSCOPE_API_KEY",
            ),
            "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
            "openrouter": (
                "https://openrouter.ai/api/v1",
                "gpt-4o-mini-2024-07-18",
                "OPENROUTER_API_KEY",
            ),
        }
        config = build_config(
            {
                "default_model": "moonshot",
                "models": {
                    name: {"base_url": url, "model": model, "api_key_env": env}
                    for name, (url, model, env) in models.items()
                },
            }
        )
        console = Console(width=80)
        cli.render_models_table(console, config, environ={})
        out = capsys.readouterr().out

        assert config.models and len(config.models) == 4
        assert "…" not in out, f"80 列下不应出现任何截断省略号：\n{out}"
        for name, (url, model, env) in models.items():
            assert url in out, f"{name} 的 base_url 被截断了"
            assert model in out, f"{name} 的 model 被截断了"
            assert env in out, f"{name} 的 api_key_env 被截断了"
        assert "已设置" not in out and "未设置" in out  # environ={} → 全部未设置
        assert out.count("未设置") == len(models), "每个模型一行数据"
        for line in out.splitlines():
            assert len(line) <= 80, f"超过 80 列：{line!r}"

    def test_history_on_truly_empty_session(self, capsys: pytest.CaptureFixture[str]) -> None:
        class EmptySession:
            def history(self) -> list[dict[str, Any]]:
                return []

        console = Console()
        assert cli.handle_slash_command("/history", EmptySession(), console) is True
        assert "（当前会话还没有消息）" in capsys.readouterr().out

    def test_tables_do_not_collapse_at_80_columns(
        self, api_server: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """回归：表格总宽超限时 rich 会把列压到 0 宽（id 列曾整列消失）。"""
        server = api_server(default=completion())
        config_path = write_config(tmp_path / "conf", server.base_url)
        config = load_config(config_path, load_env_file=False)

        record = SessionRecord(
            id="abcdef1234567890",
            title="标" * 100,
            model="m",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-10-27T09:19:38.123456+00:00",
            message_count=12,
            total_tokens=34567,
        )
        console = Console(width=80)

        cli.render_sessions_table(console, [record])
        cli.render_models_table(console, config)
        cli.render_tools_table(console, config)
        cli.render_config_check(console, config, "mock")
        out = capsys.readouterr().out

        for line in out.splitlines():
            assert len(line) <= 80, f"超过 80 列：{line!r}"
        assert "abcdef12" in out, "id 列不能消失"
        assert "mock" in out and "alt" in out and "nokey" in out
        assert "read_file" in out and "http_get" in out