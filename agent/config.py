"""配置加载与校验。

加载优先级：命令行 ``--config`` > 环境变量 ``AGENT_CONFIG`` > ``./config.yaml``。

设计决策（详见 README「设计决策」）：
- 所有相对路径以「配置文件所在目录」为基准解析，而不是当前工作目录，
  这样从任何目录启动都能得到同一套 workspace / 数据库 / 日志路径。
- 未知字段直接报错（``extra="forbid"``），用于抓住配置里的拼写错误。
- 流式是运行时的唯一路径（见 ``provider.chat_stream``），没有开关可配。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

try:  # python-dotenv 只在需要读取 .env 时才是硬依赖
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 取决于运行环境
    load_dotenv = None  # type: ignore[assignment]

CONFIG_ENV_VAR = "AGENT_CONFIG"
DEFAULT_CONFIG_FILENAME = "config.yaml"

#: registry 中真正实现了的工具名（阶段 4 与 registry.py 保持一致）
KNOWN_TOOLS = (
    "read_file",
    "write_file",
    "create_dir",
    "delete_path",
    "move_path",
    "list_dir",
    "run_shell",
    "run_python_code",
    "http_get",
    "get_time",
    "search_code",
    "git_status",
)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: 默认需要人工审批的工具：代码执行 / 删除 / shell。
#: write_file、create_dir 在 workspace 沙箱边界内静默执行（覆盖已有文件会在结果里注明）。
DEFAULT_APPROVAL_TOOLS = ("run_python_code", "delete_path", "run_shell")


class ConfigError(Exception):
    """配置文件缺失、无法解析或校验失败。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _require_non_blank(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    return value.strip()


class ModelConfig(_StrictModel):
    """单个模型的接入参数。密钥只记录环境变量名，不记录值。"""

    base_url: str
    model: str
    api_key_env: str
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    context_window: int = Field(default=32768, gt=0)
    #: 是否支持图片输入（OpenAI Vision content 数组）
    supports_vision: bool = False

    @field_validator("base_url", "model", "api_key_env")
    @classmethod
    def _non_blank(cls, value: str, info: Any) -> str:
        return _require_non_blank(value, info.field_name)

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    def api_key(self) -> str:
        """从环境变量读取密钥；缺失时只报告变量名，绝不回显内容。"""
        raw = os.environ.get(self.api_key_env, "")
        if not raw.strip():
            raise ConfigError(
                f"环境变量 {self.api_key_env} 未设置或为空（模型 {self.model} 需要它）。"
                f"请复制 .env.example 为 .env 并填入真实密钥。"
            )
        return raw.strip()


class ExtraWorkspace(_StrictModel):
    """附加工作区（命名挂载）：``@name/路径`` 访问，默认只读。

    见 README 第 59 条。``name`` 只允许小写字母/数字开头，可含 ``-`` 和 ``_``。
    """

    name: str
    path: Path
    writable: bool = False

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("附加工作区的 name 不能为空")
        if text.startswith("@"):
            text = text[1:]
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,15}$", text):
            raise ValueError(
                f"附加工作区名 {value!r} 不合法：只允许小写字母/数字开头，可含 - 和 _，最长 16 个字符"
            )
        return text


class AgentConfig(_StrictModel):
    system_prompt_file: str = "prompts/system.md"
    system_prompt: str = "你是一个严谨、简洁的助手。"
    workspace: Path = Path("./workspace")
    #: 附加工作区（多根）：通过 @name/ 前缀访问，默认只读
    extra_workspaces: list[ExtraWorkspace] = Field(default_factory=list)
    max_tool_rounds: int = Field(default=8, ge=1)
    max_history_messages: int = Field(default=40, ge=1)
    #: 上下文占用达到该比例时触发「历史摘要压缩」（0–1），替代粗暴裁剪
    context_summary_trigger: float = Field(default=0.8, gt=0, le=1)
    #: 每次摘要压缩最旧的多少条消息
    context_summary_messages: int = Field(default=10, ge=2)
    request_timeout: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0)
    #: 推理模型把输出预算全烧在思考上时，补救重试的次数
    #: （追加「直接输出」提醒后重跑；0 = 不补救，直接给提示）
    empty_answer_retries: int = Field(default=1, ge=0, le=3)

    def system_prompt_text(self, config_dir: Path) -> str:
        """优先读取 system_prompt_file；文件不存在时退回 system_prompt。"""
        path = self.system_prompt_file
        if not path or not str(path).strip():
            return self.system_prompt
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = config_dir / candidate
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
        return self.system_prompt


class RunShellConfig(_StrictModel):
    """run_shell 白名单。

    默认空列表（什么都跑不了）。只能填 PATH 中真实存在的可执行文件；
    ``dir`` / ``type`` 这类 cmd 内建命令不受支持（见 README「设计决策」第 14 条）。
    """

    allowed_commands: list[str] = Field(default_factory=list)


class HttpGetConfig(_StrictModel):
    allowed_domains: list[str] = Field(default_factory=list)


class PythonExecConfig(_StrictModel):
    """run_python_code 的执行上限。

    工具本身默认禁用（``tools.enabled`` 不含它）、默认需要人工审批
    （``tools.approval_required`` 含它），这两项与这里的数值无关。
    """

    #: 单次执行超时（秒）；超时后子进程被终止，本次调用返回 ToolError
    timeout: float = Field(default=10, gt=0)
    #: stdout / stderr 各自最多保留的字符数，超出部分截断并注明省略量
    max_output_chars: int = Field(default=8000, ge=100)


class ToolsConfig(_StrictModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "read_file",
            "write_file",
            "list_dir",
            "get_time",
            "search_code",
            "git_status",
        ]
    )
    #: 危险工具调用前是否挂起等人工审批（Web 走 SSE 审批卡片，CLI 走终端 y/N）
    require_approval: bool = False
    #: 是否允许 run_shell 使用 shell=True。开启后必须同时 require_approval=true，
    #: 且每次调用都强制进入审批流；这仍不是 OS 隔离。
    allow_unsafe_shell: bool = False
    #: 需要审批的工具名（require_approval=false 时不生效，但仍会做合法性校验）
    approval_required: list[str] = Field(
        default_factory=lambda: list(DEFAULT_APPROVAL_TOOLS)
    )
    #: 审批等待上限（秒）；超时/客户端断开都按「拒绝」处理
    approval_timeout: float = Field(default=300, gt=0)
    run_shell: RunShellConfig = Field(default_factory=RunShellConfig)
    http_get: HttpGetConfig = Field(default_factory=HttpGetConfig)
    python_exec: PythonExecConfig = Field(default_factory=PythonExecConfig)

    @field_validator("enabled")
    @classmethod
    def _known_tools(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in KNOWN_TOOLS]
        if unknown:
            raise ValueError(
                f"存在未知工具名 {unknown}，可用工具为 {list(KNOWN_TOOLS)}"
            )
        duplicated = sorted({name for name in value if value.count(name) > 1})
        if duplicated:
            raise ValueError(f"tools.enabled 中存在重复项：{duplicated}")
        return value

    @field_validator("approval_required")
    @classmethod
    def _known_approval_tools(cls, value: list[str]) -> list[str]:
        unknown = [name for name in value if name not in KNOWN_TOOLS]
        if unknown:
            raise ValueError(
                f"tools.approval_required 存在未知工具名 {unknown}，"
                f"可用工具为 {list(KNOWN_TOOLS)}"
            )
        duplicated = sorted({name for name in value if value.count(name) > 1})
        if duplicated:
            raise ValueError(f"tools.approval_required 中存在重复项：{duplicated}")
        return value

    @model_validator(mode="after")
    def _unsafe_shell_requires_approval(self) -> "ToolsConfig":
        if self.allow_unsafe_shell and not self.require_approval:
            raise ValueError(
                "tools.allow_unsafe_shell=true 时必须同时设置 tools.require_approval=true："
                "shell=True 会绕过命令白名单，必须强制人工审批。"
            )
        return self


class StorageConfig(_StrictModel):
    db_path: Path = Path("./data/sessions.db")


class LoggingConfig(_StrictModel):
    level: str = "INFO"
    file: Path | None = Path("./data/agent.log")

    @field_validator("level")
    @classmethod
    def _valid_level(cls, value: str) -> str:
        level = _require_non_blank(value, "level").upper()
        if level not in LOG_LEVELS:
            raise ValueError(f"level 必须是 {list(LOG_LEVELS)} 之一，当前为 {value!r}")
        return level


class ServerConfig(_StrictModel):
    """HTTP 服务的访问控制（只影响 ``backend``，CLI 不读这一段）。

    默认「什么都不配」也是安全的：没有令牌时只接受回环地址访问，
    这样 `docker compose up` 之后不会因为忘了配鉴权就把接口暴露到局域网。
    """

    #: 访问令牌。设置后所有 /api 请求必须带 ``Authorization: Bearer <token>``。
    #: 不设置（默认）则退化为「只允许回环地址访问」。
    auth_token: str = ""

    @field_validator("auth_token")
    @classmethod
    def _strip_token(cls, value: str) -> str:
        # 从 .env / shell 里带出来的换行和引号是最常见的「令牌明明对了却 401」来源
        return (value or "").strip().strip('"').strip("'")

    @model_validator(mode="after")
    def _warn_if_weak(self) -> "ServerConfig":
        if self.auth_token and len(self.auth_token) < 16:
            raise ValueError(
                "server.auth_token 太短（<16 字符）：这个令牌等于把服务开放给整个网络，"
                "请用 `python -c \"import secrets;print(secrets.token_urlsafe(32))\"` 生成"
            )
        return self


class AppConfig(_StrictModel):
    default_model: str
    models: dict[str, ModelConfig]
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    _source_path: Path | None = PrivateAttr(default=None)

    @field_validator("default_model")
    @classmethod
    def _non_blank_default(cls, value: str) -> str:
        return _require_non_blank(value, "default_model")

    @field_validator("models")
    @classmethod
    def _non_empty_models(cls, value: dict[str, ModelConfig]) -> dict[str, ModelConfig]:
        if not value:
            raise ValueError("至少需要配置一个模型")
        return value

    @model_validator(mode="after")
    def _default_model_exists(self) -> "AppConfig":
        if self.default_model not in self.models:
            raise ValueError(
                f"default_model={self.default_model!r} 未在 models 中定义，"
                f"已定义的模型为 {sorted(self.models)}"
            )
        return self

    # ---- 路径解析 -------------------------------------------------------
    @property
    def source_path(self) -> Path | None:
        """配置文件的绝对路径；直接构造的配置对象为 None。"""
        return self._source_path

    @property
    def config_dir(self) -> Path:
        if self._source_path is not None:
            return self._source_path.parent
        return Path.cwd()

    def resolve_path(self, path: Path) -> Path:
        """把配置中的相对路径解析为绝对路径（基准：配置文件所在目录）。"""
        if path.is_absolute():
            return path
        return (self.config_dir / path).resolve()

    @property
    def workspace_path(self) -> Path:
        return self.resolve_path(self.agent.workspace)

    @property
    def db_path(self) -> Path:
        return self.resolve_path(self.storage.db_path)

    @property
    def log_path(self) -> Path | None:
        if self.logging.file is None:
            return None
        return self.resolve_path(self.logging.file)

    def system_prompt_text(self) -> str:
        return self.agent.system_prompt_text(self.config_dir)

    # ---- 模型选择 -------------------------------------------------------
    def get_model(self, name: str | None = None) -> ModelConfig:
        key = name or self.default_model
        try:
            return self.models[key]
        except KeyError:
            raise ConfigError(
                f"未定义模型 {key!r}，已定义的模型为 {sorted(self.models)}"
            ) from None


def _format_validation_error(exc: ValidationError) -> str:
    problems: list[str] = []
    missing: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) if err["loc"] else "<顶层>"
        if err["type"] == "missing":
            missing.append(loc)
            problems.append(f"  - {loc}: 缺少必填项")
        else:
            problems.append(f"  - {loc}: {err['msg']}")
    head = "配置校验失败"
    if missing:
        head += "，缺失必填字段：" + ", ".join(missing)
    return head + "\n" + "\n".join(problems)


def build_config(data: Mapping[str, Any]) -> AppConfig:
    """用字典构造并校验配置；失败时抛出带字段路径的 ConfigError。"""
    try:
        return AppConfig.model_validate(dict(data))
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc


def resolve_config_path(
    cli_path: str | os.PathLike[str] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """按优先级算出要加载的配置文件绝对路径（不检查是否存在）。"""
    base = Path.cwd() if cwd is None else Path(cwd)
    environ = os.environ if env is None else env

    candidate: str | os.PathLike[str] | None = None
    if cli_path:
        candidate = cli_path
    elif environ.get(CONFIG_ENV_VAR, "").strip():
        candidate = environ[CONFIG_ENV_VAR].strip()

    if candidate is None:
        return (base / DEFAULT_CONFIG_FILENAME).resolve()

    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _load_env_files(config_dir: Path, cwd: Path | None) -> None:
    """已存在的 .env 依次加载（不覆盖已有环境变量），缺失则静默跳过。

    python-dotenv 未安装时：没有 .env 就正常继续（密钥来自真实环境变量），
    存在 .env 则明确报错，绝不静默忽略用户写的 .env。
    """
    env_files = [
        Path(directory) / ".env"
        for directory in (cwd, config_dir)
        if directory is not None and (Path(directory) / ".env").is_file()
    ]
    if not env_files:
        return
    if load_dotenv is None:
        raise ConfigError(
            f"检测到 {env_files[0]}，但未安装 python-dotenv，无法加载它。\n"
            f"请执行：python -m pip install python-dotenv"
        )
    seen: set[Path] = set()
    for env_file in env_files:
        if env_file in seen:
            continue
        seen.add(env_file)
        load_dotenv(env_file, override=False)


def load_config(
    cli_path: str | os.PathLike[str] | None = None,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    load_env_file: bool = True,
) -> AppConfig:
    """加载并校验配置。cli_path 对应命令行 ``--config``。"""
    path = resolve_config_path(cli_path, cwd=cwd, env=env)
    if not path.is_file():
        raise ConfigError(
            f"找不到配置文件：{path}\n"
            f"请复制 config.example.yaml 为 {DEFAULT_CONFIG_FILENAME}，"
            f"或用 --config / 环境变量 {CONFIG_ENV_VAR} 指定路径。"
        )

    if load_env_file:
        _load_env_files(path.parent, Path.cwd() if cwd is None else Path(cwd))

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件：{path}（{exc}）") from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败：{path}\n{exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"配置文件顶层必须是键值映射，实际是 {type(data).__name__}：{path}"
        )

    config = build_config(data)
    config._source_path = path
    return config