"""run_python_code（任意代码执行工具）的单元 + 集成测试。

覆盖任务验收要求：
- 正常 stdout（含工作目录 = workspace 根）、stderr、非零退出码；
- 超时（0.2s 的 sleep，timeout=0.05）→ ToolError；
- 临时文件 / 空目录清理（成功、非零退出、超时三种路径）；
- 空代码、超长代码、非字符串代码拒绝；
- 只读挂载拒绝（mock ``resolve_with_mount`` 强制命中挂载）；
- registry 的 schema 与参数校验、模型显式 timeout 覆盖默认值；
- 输出按 max_output_chars 截断并注明省略量；
- 默认审批集合包含 run_python_code，且代码默认不启用（config.example.yaml 语义）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent.config import DEFAULT_APPROVAL_TOOLS, build_config
from agent.registry import build_registry
from agent.tools import Mount, ToolError, Workspace, run_python_code
from agent.tools.python_exec import MAX_CODE_BYTES, TEMP_DIR_NAME


@pytest.fixture()
def workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root=root)


def execute(
    workspace: Workspace,
    code: str,
    *,
    timeout: float = 5,
    max_output_chars: int = 8000,
) -> str:
    return run_python_code(
        workspace, code, timeout=timeout, max_output_chars=max_output_chars
    )


def _minimal_config(tmp_path: Path, tools: dict | None = None):
    data = {
        "default_model": "m",
        "models": {
            "m": {
                "base_url": "http://127.0.0.1:9/v1",
                "model": "test-model",
                "api_key_env": "PYTHON_EXEC_TEST_KEY",
            }
        },
        "agent": {"workspace": str(tmp_path / "ws")},
    }
    if tools is not None:
        data["tools"] = tools
    return build_config(data)


# ---------------------------------------------------------------- 执行结果
def test_stdout_and_working_directory(workspace: Workspace) -> None:
    result = execute(workspace, "import os\nprint('hello')\nprint(os.getcwd())")

    assert "退出码：0" in result
    assert "用时：" in result
    assert "--- stdout ---" in result
    # 第二行 stdout 是子进程 cwd，必须固定为 workspace 根
    stdout = result.split("--- stdout ---", 1)[1].strip().splitlines()
    assert stdout[0] == "hello"
    assert Path(stdout[1]).resolve() == workspace.root


def test_stderr_is_reported(workspace: Workspace) -> None:
    result = execute(
        workspace,
        "import sys\nsys.stderr.write('warn-from-child\\n')\nprint('ok')",
    )

    assert "退出码：0" in result
    assert "--- stderr ---" in result
    assert "warn-from-child" in result
    assert "ok" in result


def test_nonzero_exit_code_is_reported_not_raised(workspace: Workspace) -> None:
    result = execute(workspace, "import sys\nprint('before-exit')\nsys.exit(3)")

    assert "退出码：3" in result
    assert "before-exit" in result


def test_timeout_raises_tool_error(workspace: Workspace) -> None:
    started = time.monotonic()
    with pytest.raises(ToolError) as excinfo:
        execute(workspace, "import time\ntime.sleep(0.2)", timeout=0.05)

    message = str(excinfo.value)
    assert "执行超时" in message
    assert "已终止" in message
    # 不能等到 sleep 自己结束（说明 timeout 真的把子进程杀了）
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------- 临时文件清理
@pytest.mark.parametrize(
    ("code", "kwargs"),
    [
        ("print('done')", {}),
        ("import sys\nsys.exit(2)", {"timeout": 5}),
        ("import time\ntime.sleep(0.2)", {"timeout": 0.05}),
    ],
    ids=["success", "nonzero-exit", "timeout"],
)
def test_temp_files_are_cleaned_up(
    workspace: Workspace, code: str, kwargs: dict
) -> None:
    temp_dir = workspace.root / TEMP_DIR_NAME
    try:
        execute(workspace, code, **kwargs)
    except ToolError:
        pass  # 超时是预期失败，这里只关心清理
    assert not temp_dir.exists(), f"临时目录没有清理：{temp_dir}"


# ---------------------------------------------------------------- 参数拒绝
def test_empty_code_is_rejected(workspace: Workspace) -> None:
    for bad in ("", "   ", "\n\t"):
        with pytest.raises(ToolError) as excinfo:
            execute(workspace, bad)
        assert "code 不能为空" in str(excinfo.value)


def test_overlong_code_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ToolError) as excinfo:
        execute(workspace, "x" * (MAX_CODE_BYTES + 1))
    assert "代码过长" in str(excinfo.value)
    assert not (workspace.root / TEMP_DIR_NAME).exists()


def test_non_string_code_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ToolError) as excinfo:
        run_python_code(workspace, 123, timeout=1, max_output_chars=100)  # type: ignore[arg-type]
    assert "code 必须是字符串" in str(excinfo.value)


def test_read_only_mount_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """强制让临时文件路径命中只读挂载：工具必须拒绝，不能写到共享目录里。"""
    root = tmp_path / "ws"
    root.mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    mount = Mount(name="libs", path=shared, writable=False)
    workspace = Workspace(root=root, extras=(mount,))

    real = Workspace.resolve_with_mount

    def forced(self: Workspace, user_path: str | Path):
        resolved, _ = real(self, user_path)
        return resolved, mount  # 伪造「解析到了只读挂载」

    monkeypatch.setattr(Workspace, "resolve_with_mount", forced)

    with pytest.raises(ToolError) as excinfo:
        execute(workspace, "print(1)")
    assert "只读挂载" in str(excinfo.value)
    assert list(shared.iterdir()) == []


# ---------------------------------------------------------------- 输出截断
def test_output_is_truncated_per_stream(workspace: Workspace) -> None:
    result = execute(
        workspace,
        "import sys\nprint('a' * 50)\nsys.stderr.write('b' * 60)",
        max_output_chars=20,
    )

    assert "已截断到 20 字符" in result
    assert "省略 30 字符" in result  # stdout: 50 - 20
    assert "省略 40 字符" in result  # stderr: 60 - 20


# ---------------------------------------------------------------- registry
def _registry(tmp_path: Path):
    (tmp_path / "ws").mkdir()
    config = _minimal_config(
        tmp_path,
        tools={
            "enabled": ["run_python_code"],
            "python_exec": {"timeout": 5, "max_output_chars": 500},
        },
    )
    return build_registry(config)


def test_registry_schema_and_successful_call(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    spec = registry.get("run_python_code")
    assert spec is not None
    assert spec.parameters["required"] == ["code"]
    assert set(spec.parameters["properties"]) == {"code", "timeout"}
    assert spec.parameters["properties"]["code"]["type"] == "string"
    assert spec.parameters["properties"]["timeout"]["type"] == "number"
    assert spec.parameters["additionalProperties"] is False
    assert "任意 Python 代码" in spec.description

    result = registry.call("run_python_code", {"code": "print(6 * 7)"})
    assert "退出码：0" in result
    assert "42" in result


def test_registry_argument_validation(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    missing = registry.call("run_python_code", {})
    assert missing.startswith("错误：") and "缺少必填参数" in missing

    wrong_type = registry.call("run_python_code", {"code": 123})
    assert wrong_type.startswith("错误：") and "参数 code 的类型应为 string" in wrong_type

    bad_timeout = registry.call("run_python_code", {"code": "print(1)", "timeout": "soon"})
    assert bad_timeout.startswith("错误：") and "timeout" in bad_timeout

    unknown = registry.call("run_python_code", {"code": "print(1)", "extra": 1})
    assert unknown.startswith("错误：") and "未定义的参数" in unknown


def test_registry_explicit_timeout_overrides_default(tmp_path: Path) -> None:
    registry = _registry(tmp_path)  # 配置默认 timeout=5
    result = registry.call(
        "run_python_code",
        {"code": "import time\ntime.sleep(0.2)", "timeout": 0.05},
    )
    assert result.startswith("错误：") and "执行超时" in result


# ---------------------------------------------------------------- 配置默认值
def test_default_approval_contains_run_python_code() -> None:
    assert "run_python_code" in DEFAULT_APPROVAL_TOOLS

    config = build_config(
        {
            "default_model": "m",
            "models": {
                "m": {
                    "base_url": "http://127.0.0.1:9/v1",
                    "model": "m",
                    "api_key_env": "K",
                }
            },
        }
    )
    assert "run_python_code" in config.tools.approval_required
    assert "run_python_code" not in config.tools.enabled
    assert config.tools.python_exec.timeout == 10
    assert config.tools.python_exec.max_output_chars == 8000