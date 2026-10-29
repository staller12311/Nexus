"""阶段 1：配置加载与校验的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.config import (
    CONFIG_ENV_VAR,
    AppConfig,
    ConfigError,
    build_config,
    load_config,
    resolve_config_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"


def _minimal_config() -> dict:
    return {
        "default_model": "deepseek",
        "models": {
            "deepseek": {
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "api_key_env": "DEEPSEEK_API_KEY",
            }
        },
    }


def _write(tmp_path: Path, data: dict, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 示例配置
def test_example_config_is_valid() -> None:
    config = load_config(EXAMPLE_CONFIG, load_env_file=False)
    assert isinstance(config, AppConfig)
    assert config.default_model == "deepseek"
    assert set(config.models) == {"deepseek", "ollama"}
    assert config.models["deepseek"].context_window == 65536
    assert config.models["ollama"].base_url == "http://localhost:11434/v1"
    assert config.tools.enabled == [
        "read_file",
        "write_file",
        "create_dir",
        "delete_path",
        "move_path",
        "list_dir",
        "get_time",
    ]
    assert "http_get" not in config.tools.enabled
    # 任意代码执行工具默认关闭（config.example.yaml 只在 approval_required 里登记它）
    assert "run_python_code" not in config.tools.enabled
    assert "run_python_code" in config.tools.approval_required
    # write_file / create_dir 在沙箱内静默执行，不再逐次审批
    assert "write_file" not in config.tools.approval_required
    assert "create_dir" not in config.tools.approval_required
    assert set(config.tools.approval_required) == {
        "run_python_code",
        "delete_path",
        "run_shell",
    }
    assert config.tools.python_exec.timeout == 10
    assert config.tools.python_exec.max_output_chars == 8000
    assert config.agent.max_tool_rounds == 15
    assert config.logging.level == "INFO"


def test_example_config_keeps_field_names() -> None:
    """字段名不允许随意改名。"""
    raw = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    assert set(raw) == {
        "default_model",
        "models",
        "agent",
        "tools",
        "storage",
        "logging",
    }
    assert set(raw["agent"]) == {
        "system_prompt_file",
        "system_prompt",
        "workspace",
        "extra_workspaces",
        "max_tool_rounds",
        "max_history_messages",
        "request_timeout",
        "max_retries",
    }
    assert set(raw["tools"]) == {
        "enabled",
        "require_approval",
        "approval_required",
        "approval_timeout",
        "run_shell",
        "python_exec",
        "http_get",
    }
    assert set(raw["storage"]) == {"db_path"}
    assert set(raw["logging"]) == {"level", "file"}


# ---------------------------------------------------------------- 加载优先级
def test_cli_path_wins_over_env(tmp_path: Path) -> None:
    _write(tmp_path, _minimal_config(), "a.yaml")
    _write(tmp_path, _minimal_config(), "b.yaml")
    resolved = resolve_config_path(
        str(tmp_path / "a.yaml"),
        cwd=tmp_path,
        env={CONFIG_ENV_VAR: str(tmp_path / "b.yaml")},
    )
    assert resolved == (tmp_path / "a.yaml").resolve()


def test_env_path_used_when_no_cli(tmp_path: Path) -> None:
    path = _write(tmp_path, _minimal_config(), "b.yaml")
    resolved = resolve_config_path(
        None, cwd=tmp_path, env={CONFIG_ENV_VAR: str(path)}
    )
    assert resolved == path.resolve()


def test_default_is_cwd_config_yaml(tmp_path: Path) -> None:
    resolved = resolve_config_path(None, cwd=tmp_path, env={})
    assert resolved == (tmp_path / "config.yaml").resolve()


def test_load_config_via_env_var(tmp_path: Path) -> None:
    path = _write(tmp_path, _minimal_config(), "custom.yaml")
    config = load_config(
        None, cwd=tmp_path, env={CONFIG_ENV_VAR: str(path)}, load_env_file=False
    )
    assert config.default_model == "deepseek"
    assert config.source_path == path.resolve()


def test_relative_cli_path_resolves_against_cwd(tmp_path: Path) -> None:
    _write(tmp_path, _minimal_config(), "rel.yaml")
    config = load_config("rel.yaml", cwd=tmp_path, env={}, load_env_file=False)
    assert config.source_path == (tmp_path / "rel.yaml").resolve()


# ---------------------------------------------------------------- 错误信息
def test_missing_config_file_names_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_config(tmp_path / "nope.yaml", load_env_file=False)
    assert "nope.yaml" in str(excinfo.value)


def test_missing_required_field_is_reported(tmp_path: Path) -> None:
    data = _minimal_config()
    del data["models"]["deepseek"]["base_url"]
    path = _write(tmp_path, data)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, load_env_file=False)
    message = str(excinfo.value)
    assert "models.deepseek.base_url" in message
    assert "缺少必填项" in message


def test_missing_default_model_is_reported() -> None:
    data = _minimal_config()
    del data["default_model"]
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "default_model" in str(excinfo.value)


def test_default_model_must_exist() -> None:
    data = _minimal_config()
    data["default_model"] = "ghost"
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "ghost" in str(excinfo.value)
    assert "deepseek" in str(excinfo.value)


def test_unknown_field_is_rejected() -> None:
    data = _minimal_config()
    data["agent"] = {"max_tool_round": 3}  # 少写一个 s
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "agent.max_tool_round" in str(excinfo.value)


def test_yaml_syntax_error_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("default_model: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, load_env_file=False)
    assert "YAML" in str(excinfo.value)


def test_non_mapping_top_level_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, load_env_file=False)
    assert "键值映射" in str(excinfo.value)


def test_invalid_base_url() -> None:
    data = _minimal_config()
    data["models"]["deepseek"]["base_url"] = "api.deepseek.com"
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "models.deepseek.base_url" in str(excinfo.value)


def test_invalid_temperature() -> None:
    data = _minimal_config()
    data["models"]["deepseek"]["temperature"] = 9
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "temperature" in str(excinfo.value)


# ---------------------------------------------------------------- 工具/日志
def test_unknown_tool_in_enabled_is_rejected() -> None:
    data = _minimal_config()
    data["tools"] = {"enabled": ["read_file", "rm_rf"]}
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "rm_rf" in str(excinfo.value)


def test_duplicate_tool_is_rejected() -> None:
    data = _minimal_config()
    data["tools"] = {"enabled": ["read_file", "read_file"]}
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "重复" in str(excinfo.value)


def test_run_shell_allowlist_defaults_to_empty() -> None:
    """run_shell 白名单默认必须为空：开箱即安全；代码执行工具默认关闭 + 需要审批。"""
    config = build_config(_minimal_config())
    assert config.tools.run_shell.allowed_commands == []
    assert config.tools.http_get.allowed_domains == []
    assert "run_shell" not in config.tools.enabled
    assert "http_get" not in config.tools.enabled
    assert "run_python_code" not in config.tools.enabled
    assert "run_python_code" in config.tools.approval_required
    assert config.tools.python_exec.timeout == 10
    assert config.tools.python_exec.max_output_chars == 8000


def test_example_config_has_empty_allowlists() -> None:
    config = load_config(EXAMPLE_CONFIG, load_env_file=False)
    assert config.tools.run_shell.allowed_commands == []
    assert config.tools.http_get.allowed_domains == []


def test_removed_stream_key_is_rejected() -> None:
    """stream 开关已随真流式落地废弃：再写进配置应报未知字段，而不是被静默忽略。"""
    data = _minimal_config()
    data["agent"] = {"stream": False}
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "stream" in str(excinfo.value)


def test_invalid_log_level_is_rejected() -> None:
    data = _minimal_config()
    data["logging"] = {"level": "LOUD"}
    with pytest.raises(ConfigError) as excinfo:
        build_config(data)
    assert "logging.level" in str(excinfo.value)


def test_log_level_is_uppercased() -> None:
    data = _minimal_config()
    data["logging"] = {"level": "debug"}
    assert build_config(data).logging.level == "DEBUG"


# ---------------------------------------------------------------- 路径解析
def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    nested = tmp_path / "conf"
    nested.mkdir()
    path = _write(nested, _minimal_config())
    config = load_config(path, load_env_file=False)
    assert config.workspace_path == (nested / "workspace").resolve()
    assert config.db_path == (nested / "data" / "sessions.db").resolve()
    assert config.log_path == (nested / "data" / "agent.log").resolve()


def test_absolute_paths_are_kept(tmp_path: Path) -> None:
    data = _minimal_config()
    target = tmp_path / "abs.db"
    data["storage"] = {"db_path": str(target)}
    config = build_config(data)
    assert config.db_path == target


# ---------------------------------------------------------------- 系统提示词
def test_system_prompt_file_takes_priority(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("来自文件", encoding="utf-8")
    data = _minimal_config()
    data["agent"] = {
        "system_prompt_file": "prompts/system.md",
        "system_prompt": "来自配置",
    }
    config = build_config(data)
    assert config.agent.system_prompt_text(tmp_path) == "来自文件"


def test_system_prompt_falls_back_when_file_missing(tmp_path: Path) -> None:
    data = _minimal_config()
    data["agent"] = {
        "system_prompt_file": "prompts/missing.md",
        "system_prompt": "来自配置",
    }
    config = build_config(data)
    assert config.agent.system_prompt_text(tmp_path) == "来自配置"


# ---------------------------------------------------------------- 密钥
def test_api_key_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "  sk-test  ")
    config = build_config(_minimal_config())
    assert config.get_model().api_key() == "sk-test"


def test_missing_api_key_names_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = build_config(_minimal_config())
    with pytest.raises(ConfigError) as excinfo:
        config.get_model().api_key()
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


def test_get_model_unknown_name() -> None:
    config = build_config(_minimal_config())
    with pytest.raises(ConfigError) as excinfo:
        config.get_model("nope")
    assert "nope" in str(excinfo.value)


def test_dotenv_is_loaded_from_config_dir(tmp_path: Path) -> None:
    pytest.importorskip("dotenv", reason="需要 python-dotenv 才能验证 .env 加载")
    # 真实环境变量的隔离由 tests/conftest.py 的 autouse fixture 统一负责（这里不再逐个 delenv）
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=from-dotenv\n", encoding="utf-8")
    path = _write(tmp_path, _minimal_config())
    config = load_config(path, cwd=tmp_path, env={}, load_env_file=True)
    assert config.models["deepseek"].api_key_env == "DEEPSEEK_API_KEY"
    assert config.get_model().api_key() == "from-dotenv"


def test_no_env_file_is_fine(tmp_path: Path) -> None:
    """没有 .env 时不应因为 python-dotenv 是否存在而报错。"""
    path = _write(tmp_path, _minimal_config())
    config = load_config(path, cwd=tmp_path, env={}, load_env_file=True)
    assert config.source_path == path.resolve()