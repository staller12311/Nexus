"""装配并调用 ``agent`` 的核心能力。

**这里没有智能体逻辑**：工具循环、历史裁剪、错误分类、重试、持久化全部复用
``agent.loop`` / ``agent.provider`` / ``agent.registry`` / ``agent.storage``；
本模块只做「读配置 → 装配 → 调用 → 转成 Web 用的数据结构」。

线程安全说明：sqlite 连接默认不允许跨线程复用，而 FastAPI 会把同步端点丢到工作线程，
所以每次调用都**独立开一个 ``Storage``**（sqlite 打开是微秒级，值得）。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from agent.config import KNOWN_TOOLS, AppConfig, ConfigError, ExtraWorkspace, load_config
from agent.logging_setup import setup_logging
from agent.loop import SUMMARY_SYSTEM_PROMPT, SUMMARY_USER_TEMPLATE, AgentLoop
from agent.provider import Provider, ToolCall, Usage
from agent.registry import ToolRegistry, build_registry
from agent.tools.files import Mount
from agent.storage import SessionRecord, Storage, utc_now

from .custom_models import (
    CustomModel,
    CustomModelError,
    CustomModelStore,
    DuplicateCustomModel,
)
from .approvals import ApprovalRegistry
from .knowledge import KnowledgeService, build_search_tool
from .model_runtime import build_custom_provider
from .web_search import WebSearchService, build_web_search_tool
from .workspace import (
    WorkspaceError,
    WorkspaceSettings,
    dump_settings,
    parse_settings,
    validate_extras,
    validate_workspace,
)

#: 标题里要清掉的标点与空白（模型经常不听话地加句号/引号/书名号）
_TITLE_NOISE = re.compile(r"[\s\"'“”‘’《》「」【】\[\]()（）<>.,，。!！?？:：;；、\-—_*#`~|/\\]+")
_TITLE_PREFIX = re.compile(r"^(标题|topic|title)\s*[:：]?\s*", re.IGNORECASE)

logger = logging.getLogger("agent.web")

DEFAULT_SESSION_LIMIT = 20
#: 自定义模型没有显式上下文配置时的保守默认值（128K）
DEFAULT_CONTEXT_WINDOW = 131072
#: 官方文档确认 1M 上下文的模型（DeepSeek-V4 系列 / Qwen3.8-Flash 等）
_ONE_MILLION_PREFIXES = (
    "deepseek-v4",
    "deepseek-chat",
    "deepseek-reasoner",
    "qwen3.8",
    "qwen3.7",
    "qwen-turbo",
)
#: 需要显式配置白名单、默认禁用的工具（与 README 第 14/16 条一致）
DEFAULT_DISABLED_TOOLS = frozenset({"run_shell", "http_get"})
#: 自动起标题用的提示词（第 45 条）
TITLE_PROMPT = "请用不超过 10 个字总结这段对话的主题作为标题，只输出标题，不要任何标点符号和解释。"
#: 只喂前几条消息（第一对 user/assistant 就够）
TITLE_MESSAGE_LIMIT = 4
#: 单条消息截断长度，避免把长回答整段塞进去
TITLE_MESSAGE_CHARS = 200
#: 起标题的超时与重试：失败就降级，绝不拖住前端
TITLE_TIMEOUT_SECONDS = 15.0
TITLE_MAX_CHARS = 20


@dataclass(frozen=True)
class ModelInfo:
    name: str
    model: str
    base_url: str
    api_key_env: str
    key_set: bool
    is_default: bool
    #: 上下文窗口（用于前端显示当前会话的上下文占用率；自定义模型没有该字段）
    context_window: int = DEFAULT_CONTEXT_WINDOW
    #: 来自 Web 端添加（而不是 config.yaml）
    is_custom: bool = False
    #: 是否已有可用密钥（内置=环境变量有值；自定义=库里存了）
    has_key: bool = True
    #: 脱敏后的密钥（内置模型为空，因为后端从不读它的值）
    api_key_masked: str = ""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    enabled: bool
    default_disabled: bool
    description: str


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str
    model: str
    created_at: str
    updated_at: str
    message_count: int
    total_tokens: int
    #: title 为空时给前端的兜底显示用：该会话第一条 user 消息（截断）
    first_user_message: str = ""


@dataclass(frozen=True)
class ChatOutcome:
    content: str | None
    usage: Usage
    rounds: int
    tool_limit_reached: bool


def clean_title(raw: str) -> str:
    """把模型返回的文字清理成「纯标题」：去前缀、去标点空白、截断。"""
    text = (raw or "").strip()
    text = text.splitlines()[0] if text else ""
    text = _TITLE_PREFIX.sub("", text)
    text = _TITLE_NOISE.sub("", text)
    return text[:TITLE_MAX_CHARS]


class AgentService:
    """一个进程内单例即可：除 ``config`` 外无可变状态。

    **唯一可变状态**是工作区（workspace）：用户可以在前端切换，切换后重建工具注册表
    （文件工具的根目录绑在 ``Workspace`` 实例里），并把选择持久化到 ``app_settings``。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.custom_models = CustomModelStore(config.db_path)
        # 本地知识库：解析/检索都在 backend 自己这层，agent/ 不知道它的存在；
        # 工具通过注册注入（KNOWN_TOOLS 不用改），未启用工具时注册了也不会被调用。
        self.knowledge = KnowledgeService(config.db_path)
        # 联网搜索：Bing 主 / 百度 兜底，零新增依赖（README 第 53 条）
        self.web_search = WebSearchService()
        # 工作区设置（README 第 58 条）
        self.workspace_settings = WorkspaceSettings(config.db_path)
        #: 人工审批登记表（进程级；本地单用户场景足够）
        self.approvals = ApprovalRegistry()
        self._workspace: Path | None = None
        self._extras: tuple[Mount, ...] = ()
        persisted = self.workspace_settings.get()
        if persisted:
            saved_root, saved_extras = parse_settings(persisted)
            if saved_root is not None:
                try:
                    self._workspace = validate_workspace(str(saved_root))
                except WorkspaceError:
                    # 之前存的路径现在不可用了（被删/被改名）：忽略它，回退默认
                    self._workspace = None
            self._extras = saved_extras
        self.registry = self._build_registry()
        self._descriptions = self._collect_descriptions()

    # ---- 工作区 ---------------------------------------------------------
    @property
    def default_workspace(self) -> Path:
        return self.config.workspace_path

    @property
    def workspace(self) -> Path:
        return self._workspace or self.default_workspace

    @property
    def extras(self) -> tuple[Mount, ...]:
        """附加工作区（@name 命名挂载）。"""
        return self._extras

    def _config_with_workspace(self) -> AppConfig:
        if self._workspace is None and not self._extras:
            return self.config
        # 注意：model_copy 不做类型校验：agent.workspace 是 Path，
        # extra_workspaces 必须是 ExtraWorkspace 实例（已单独校验过）
        agent = self.config.agent.model_copy(
            update={
                "workspace": Path(self.workspace),
                "extra_workspaces": [
                    ExtraWorkspace(name=mount.name, path=mount.path, writable=mount.writable)
                    for mount in self._extras
                ],
            }
        )
        return self.config.model_copy(update={"agent": agent})

    def _build_registry(self) -> ToolRegistry:
        """按当前工作区重建工具注册表，并重新注入 KB / web_search 工具。"""
        registry = build_registry(self._config_with_workspace())
        try:
            registry.register(build_search_tool(self.knowledge))
        except ValueError:
            pass
        try:
            registry.register(build_web_search_tool(self.web_search))
        except ValueError:
            pass
        return registry

    def set_workspace(
        self, raw_path: str, extras: list[dict] | None = None
    ) -> Path:
        """校验并切换工作区（可带附加挂载）；返回解析后的主根路径。

        非法路径抛 ``WorkspaceError``。任何一项不合法都整体拒绝（不做部分生效）。
        """
        resolved = validate_workspace(raw_path)
        mounts = validate_extras(extras)
        if any(mount.path == resolved for mount in mounts):
            raise WorkspaceError("附加目录不能与主工作区相同")
        self._workspace = resolved
        self._extras = mounts
        self.workspace_settings.set(dump_settings(resolved, mounts))
        self.registry = self._build_registry()
        self._descriptions = self._collect_descriptions()
        return resolved

    def reset_workspace(self) -> Path:
        """恢复 config.yaml 里的默认工作区（并清空附加挂载）。"""
        self._workspace = None
        self._extras = ()
        self.workspace_settings.set(None)
        self.registry = self._build_registry()
        self._descriptions = self._collect_descriptions()
        return self.default_workspace

    @classmethod
    def load(cls, config_path: str | None = None) -> "AgentService":
        # 复用 agent/config.py：不自己解析 YAML，也不重复实现校验与路径解析
        config = load_config(config_path)
        setup_logging(level=config.logging.level, log_file=config.log_path)
        return cls(config)

    # ---- 只读视图 -------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """内置（config.yaml）在上，自定义（数据库）在下，一次返回给前端。"""
        infos = [
            ModelInfo(
                name=name,
                model=model.model,
                base_url=model.base_url,
                api_key_env=model.api_key_env,
                key_set=bool(os.environ.get(model.api_key_env, "").strip()),
                is_default=(name == self.config.default_model),
                context_window=model.context_window,
                is_custom=False,
                has_key=bool(os.environ.get(model.api_key_env, "").strip()),
                api_key_masked="",  # 内置模型的密钥后端从不读取明文，也就给不出掩码
            )
            for name, model in self.config.models.items()
        ]
        for custom in self.custom_models.list():
            infos.append(
                ModelInfo(
                    name=custom.name,
                    model=custom.model,
                    base_url=custom.base_url,
                    api_key_env="(自定义)",
                    key_set=bool(custom.api_key.strip()),
                    is_default=False,
                    context_window=self.context_window_for(custom.name),
                    is_custom=True,
                    has_key=bool(custom.api_key.strip()),
                    api_key_masked=custom.masked_key,
                )
            )
        return infos

    # ---- 自定义模型的增删 ----------------------------------------------
    def list_custom_models(self) -> list[CustomModel]:
        return self.custom_models.list()

    def add_custom_model(
        self, name: str, base_url: str, model: str, api_key: str
    ) -> CustomModel:
        clean = (name or "").strip()
        if clean in self.config.models:
            raise DuplicateCustomModel(f"「{clean}」是 config.yaml 里的内置模型名，不能重复")
        return self.custom_models.add(clean, base_url, model, api_key)

    def delete_custom_model(self, name: str) -> dict[str, Any]:
        target = self.custom_models.get(name)
        if target is None:
            raise CustomModelError(f"找不到自定义模型：{name}")
        referenced = self.custom_models.count_sessions_referencing(target.name)
        if not self.custom_models.delete(target.name):
            raise CustomModelError(f"删除失败：{target.name}")
        return {"deleted": True, "name": target.name, "referenced_sessions": referenced}

    def context_window_for(self, model_name: str) -> int:
        """返回模型的真实上下文窗口（内置读 config；自定义按已知模型映射）。"""
        model_config = self.config.models.get(model_name)
        if model_config is not None:
            return int(model_config.context_window)
        custom = self.custom_models.get(model_name)
        model_id = (custom.model if custom else model_name).lower()
        if any(prefix in model_id for prefix in _ONE_MILLION_PREFIXES):
            return 1_000_000
        return DEFAULT_CONTEXT_WINDOW

    def _make_summarizer(self, provider: Provider):
        """构造「后台静默摘要器」：用同一个模型把旧对话压成一句话（短输出、低温度）。"""

        def summarize(transcript: str) -> str | None:
            result = provider.chat(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": SUMMARY_USER_TEMPLATE.format(transcript=transcript),
                    },
                ],
                temperature=0.2,
                max_tokens=512,
            )
            summary = (result.content or "").strip()
            return summary or None

        return summarize

    def has_model(self, name: str) -> bool:
        return name in self.config.models or self.custom_models.get(name) is not None

    # ---- 自动起标题 -----------------------------------------------------
    def generate_title(self, session_id: str, *, force: bool = False) -> dict[str, Any]:
        """用一次**非流式、短超时**的模型调用给会话起标题。

        - 不阻塞主对话：这是一个独立请求，前端在 ``message_end`` 之后才调用它；
        - 已有标题（用户手动改过 / 之前生成过）时默认跳过，避免被覆盖；
        - 生成失败不抛给前端"弹窗"，而是抛给调用方由路由转成 503，前端静默降级。
        """
        with self._storage() as storage:
            record = storage.find_session(session_id)
            messages = storage.get_messages(record.id)

        if record.title and not force:
            return {
                "session_id": record.id,
                "title": record.title,
                "generated": False,
                "model": record.model,
            }

        picked = [item for item in messages if item.get("role") in ("user", "assistant")]
        if len(picked) < 2:
            raise ConfigError("会话还没有形成一轮对话，无法生成标题")

        transcript = "\n".join(
            f"{'用户' if item['role'] == 'user' else '助手'}："
            f"{str(item.get('content') or '')[:TITLE_MESSAGE_CHARS]}"
            for item in picked[:TITLE_MESSAGE_LIMIT]
        )
        provider = self._title_provider(record.model or self.config.default_model)
        try:
            result = provider.chat(
                [
                    {"role": "system", "content": TITLE_PROMPT},
                    {"role": "user", "content": transcript},
                ],
                tools=None,
            )
        finally:
            provider.close()

        title = clean_title(result.content or "")
        if not title:
            raise ConfigError("模型没有返回可用标题")

        with self._storage() as storage:
            storage.rename_session(record.id, title)
        return {
            "session_id": record.id,
            "title": title,
            "generated": True,
            "model": record.model,
        }

    def _title_provider(self, model_name: str) -> Provider:
        """起标题专用 Provider：短超时 + 不重试，失败尽快降级。"""
        custom = self.custom_models.get(model_name)
        if custom is not None:
            return build_custom_provider(
                self.config,
                custom,
                request_timeout=TITLE_TIMEOUT_SECONDS,
                max_retries=0,
            )
        return Provider.from_config(
            self.config,
            model_name,
            request_timeout=TITLE_TIMEOUT_SECONDS,
            max_retries=0,
        )

    def _provider_for(self, model_name: str) -> Provider:
        """内置模型走 ``Provider.from_config``；自定义模型走数据库组装的 Provider。"""
        if model_name in self.config.models:
            return Provider.from_config(self.config, model_name)
        row = self.custom_models.get(model_name)
        if row is not None:
            return build_custom_provider(self.config, row)
        available = sorted(
            list(self.config.models) + [item.name for item in self.custom_models.list()]
        )
        raise ConfigError(f"未定义模型 {model_name!r}，已定义的模型为 {available}")

    def list_tools(self) -> list[ToolInfo]:
        """全部工具：config.yaml 里的内置工具 + registry 中动态注入的工具。

        注入工具（``search_knowledge_base`` / ``web_search``）不在 ``KNOWN_TOOLS`` 里，
        但它们确实注册进了当前 registry、每轮对话都会被模型看到并调用，所以按 registry
        的真实内容补齐 —— 保证 ``/api/tools`` 与模型实际可用工具一致。
        """
        enabled = set(self.config.tools.enabled)
        tools = [
            ToolInfo(
                name=name,
                enabled=name in enabled,
                default_disabled=name in DEFAULT_DISABLED_TOOLS,
                description=self._descriptions.get(name, ""),
            )
            for name in KNOWN_TOOLS
        ]
        listed = {tool.name for tool in tools}
        for schema in self.registry.schemas():
            function = schema.get("function") or {}
            name = str(function.get("name") or "")
            if not name or name in listed:
                continue
            tools.append(
                ToolInfo(
                    name=name,
                    # 注入工具不受 tools.enabled 控制：注册即生效
                    enabled=True,
                    default_disabled=False,
                    description=str(function.get("description") or ""),
                )
            )
        return tools

    def list_sessions(self, limit: int = DEFAULT_SESSION_LIMIT) -> list[SessionInfo]:
        with self._storage() as storage:
            records = storage.list_sessions(limit=limit)
            # 只有 title 为空的会话才需要兜底标题，已起过标题的零开销
            pending = [record.id for record in records if not record.title]
            first_user = self._first_user_messages(pending)
            return [
                self._to_session_info(record, first_user.get(record.id, "")) for record in records
            ]

    def get_session(self, session_id: str) -> tuple[SessionInfo, list[dict[str, Any]]]:
        with self._storage() as storage:
            record = storage.find_session(session_id)  # 支持唯一短前缀，与 CLI 一致
            # 详情接口要回放思维链与单条 usage；agent 的历史回放（chat/chat_stream）不带
            return (
                self._to_session_info(record),
                storage.get_messages(record.id, with_reasoning=True, with_usage=True),
            )

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        """token 用量统计（/api/stats），供前端底部状态栏展示。

        - 传 ``session_id``：只统计**当前会话**（默认口径，保证数据连贯：同一个会话里
          单条消息的命中率与累计命中率才对得上）；
        - 不传：全库累计（保留给诊断/脚本用）。

        命中率口径固定为 **cache_hit_tokens / prompt_tokens**，completion 绝不进分母。
        """
        if session_id:
            with self._storage() as storage:
                record = storage.find_session(session_id)
                totals = {
                    "sessions": 1,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "total_tokens": record.total_tokens,
                    "cache_hit_tokens": record.cache_hit_tokens,
                    "session_id": record.id,
                }
        else:
            with self._storage() as storage:
                totals = storage.usage_totals()
            totals["session_id"] = None
        prompt_tokens = totals["prompt_tokens"]
        totals["cache_hit_rate"] = (
            round(totals["cache_hit_tokens"] / prompt_tokens, 4) if prompt_tokens > 0 else 0.0
        )
        return totals

    def create_session(self, model: str | None = None, title: str = "") -> SessionInfo:
        model_name = model or self.config.default_model
        if not self.has_model(model_name):
            available = list(self.config.models) + [item.name for item in self.custom_models.list()]
            raise ConfigError(f"未定义模型 {model_name!r}，已定义的模型为 {sorted(available)}")
        with self._storage() as storage:
            return self._to_session_info(storage.create_session(model=model_name, title=title))

    # ---- 重命名 / 删除 -------------------------------------------------
    def rename_session(self, session_id: str, title: str) -> SessionInfo:
        """改标题。空标题表示"清空"，前端会退回用第一条 user 消息兜底显示。"""
        text = (title or "").strip()
        with self._storage() as storage:
            record = storage.find_session(session_id)
            storage.rename_session(record.id, text)
            return self._to_session_info(storage.find_session(record.id), "")

    def delete_session(self, session_id: str) -> SessionInfo:
        """删除会话（消息由外键 ON DELETE CASCADE 一并删除，见 README 第 43 条）。"""
        with self._storage() as storage:
            record = storage.find_session(session_id)
            snapshot = self._to_session_info(record, "")
            if not storage.delete_session(record.id):
                raise StorageError(f"删除失败：{record.id}")
            return snapshot

    # ---- 系统提示词 -----------------------------------------------------
    def get_system_prompt(self, session_id: str) -> str:
        """读会话当前的 system 消息（没有就返回空串，前端据此显示"默认"）。"""
        with self._storage() as storage:
            record = storage.find_session(session_id)
            for message in storage.get_messages(record.id):
                if message.get("role") == "system":
                    return str(message.get("content") or "")
        return ""

    def set_system_prompt(self, session_id: str, prompt: str) -> dict[str, Any]:
        """覆盖（或补建）会话的 system 消息，**下一轮对话立即生效**。

        为什么改库就能立即生效：每轮 ``chat()`` 都会重新 ``storage.get_messages()`` 取历史，
        再交给 ``AgentLoop``；循环看到历史里已有 system 消息就不会用配置里的默认提示词覆盖
        （``AgentLoop.__init__`` 的行为），所以改完这一行，下一句就走新提示词。

        并发：``agent/storage.py`` 建库时已 ``PRAGMA journal_mode = WAL``；
        这里再显式设一次（幂等），用 ``BEGIN IMMEDIATE`` + 一条 UPDATE 快速提交，
        冲突时 sqlite 会按 timeout 等锁，不会把正在跑的 loop 打断。
        """
        text = (prompt or "").strip()
        with self._storage() as storage:  # 复用 Storage 校验会话存在（不存在抛 StorageError）
            record = storage.find_session(session_id)

        created, seq = self._write_system_message(record.id, text)
        return {
            "session_id": record.id,
            "system_prompt": text,
            "created": created,
            "seq": seq,
        }

    def _write_system_message(self, session_id: str, text: str) -> tuple[bool, int]:
        """返回 (是否新建, seq)。已存在 system 行就 UPDATE，否则插到最前面。"""
        path = Path(self.config.db_path)
        connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        try:
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.Error:
                pass  # 只影响并发性能，不影响正确性

            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "UPDATE messages SET content = ? WHERE session_id = ? AND role = 'system'",
                    (text, session_id),
                )
                created = cursor.rowcount == 0
                seq = -1
                if created and text:
                    # 消息的 seq 从 1 开始（agent 用 MAX(seq)+1），且 UNIQUE(session_id, seq)；
                    # 插到最前面：空库给 0，已有消息给 MIN(seq)-1（正常就是 0）
                    row = connection.execute(
                        "SELECT COALESCE(MIN(seq), 1) - 1 AS s FROM messages WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                    seq = int(row[0])
                    connection.execute(
                        "INSERT INTO messages (session_id, seq, role, content, created_at)"
                        " VALUES (?, ?, 'system', ?, ?)",
                        (session_id, seq, text, utc_now()),
                    )
                elif created and not text:
                    # 空串 + 本来就没有 system 行 = 什么都不用做。
                    # 若硬插一条空 system，会把配置里的默认提示词「挡住」（循环只在没有 system 时才补默认）。
                    created = False
                connection.execute("COMMIT")
            except sqlite3.Error:
                connection.execute("ROLLBACK")
                raise
            return created, seq
        finally:
            connection.close()

    # ---- 对话 -----------------------------------------------------------
    def chat(
        self,
        session_id: str,
        message: str,
        *,
        model: str | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        approval_handler: Callable[[ToolCall], bool] | None = None,
    ) -> ChatOutcome:
        """跑一轮问答。工具执行前后会触发回调（供 SSE 推送事件）。

        注意：本方法**同步阻塞**直到整轮结束；SSE 层用 ``anyio.to_thread`` 调用它。
        客户端断开不会中断这里的执行（已采纳的语义：跑完、结果落库、只是丢事件流）。
        """
        text = (message or "").strip()
        if not text:
            raise ValueError("消息不能为空")

        with self._storage() as storage:
            record = storage.find_session(session_id)
            model_name = model or record.model or self.config.default_model
            provider = self._provider_for(model_name)

            history = storage.get_messages(record.id) or None
            try:
                loop = AgentLoop.from_config(
                    self._loop_config(),
                    provider,
                    self.registry,
                    storage=storage,
                    session_id=record.id,
                    history=history,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    approval_handler=approval_handler,
                    context_window=self.context_window_for(model_name),
                    summarizer=self._make_summarizer(provider),
                )
                result = loop.run(text)
            finally:
                provider.close()

        return ChatOutcome(
            content=result.content,
            usage=result.usage,
            rounds=result.rounds,
            tool_limit_reached=result.tool_limit_reached,
        )

    def chat_stream(
        self,
        session_id: str,
        message: str,
        *,
        model: str | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        approval_handler: Callable[[ToolCall], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """流式跑一轮问答，产出**SSE 就绪**的事件字典（真·逐 token）。

        事件：``reasoning_delta`` / ``text_delta`` / ``message_end``。
        工具事件仍由 ``on_tool_call`` / ``on_tool_result`` 回调触发（与 chat() 一致）。
        """
        text = (message or "").strip()
        if not text:
            raise ValueError("消息不能为空")
        yield from self._stream_turn(
            session_id,
            text,
            model=model,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            approval_handler=approval_handler,
        )

    def regenerate_stream(
        self,
        session_id: str,
        *,
        model: str | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        approval_handler: Callable[[ToolCall], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """重新生成：删掉「最后一条 user 及其之后」的消息，再用同一句话重跑。

        历史不会重复：删掉的那条 user 会被 loop 原样重新写入（README 第 53 条）。
        """
        with self._storage() as storage:
            record = storage.find_session(session_id)
            text = (storage.delete_from_last_user(record.id) or "").strip()
        if not text:
            raise ValueError("这个会话里没有可重新生成的用户消息")

        yield from self._stream_turn(
            session_id,
            text,
            model=model,
            on_tool_call=on_tool_call,
            on_tool_result=on_tool_result,
            approval_handler=approval_handler,
        )

    def _stream_turn(
        self,
        session_id: str,
        text: str,
        *,
        model: str | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolCall, str], None] | None = None,
        approval_handler: Callable[[ToolCall], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        with self._storage() as storage:
            record = storage.find_session(session_id)
            model_name = model or record.model or self.config.default_model
            provider = self._provider_for(model_name)
            history = storage.get_messages(record.id) or None
            try:
                loop = AgentLoop.from_config(
                    self._loop_config(),
                    provider,
                    self.registry,
                    storage=storage,
                    session_id=record.id,
                    history=history,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                    approval_handler=approval_handler,
                    context_window=self.context_window_for(model_name),
                    summarizer=self._make_summarizer(provider),
                )
                for event in loop.run_stream(text):
                    kind = event.get("type")
                    if kind == "reasoning":
                        yield {"type": "reasoning_delta", "text": event.get("text") or ""}
                    elif kind == "text":
                        yield {"type": "text_delta", "delta": event.get("text") or ""}
                    elif kind == "notice":
                        yield {"type": "text_delta", "delta": event.get("text") or ""}
                    elif kind == "done":
                        result = event["result"]
                        yield {
                            "type": "message_end",
                            "content": result.content or "",
                            "reasoning": event.get("reasoning") or "",
                            "usage": {
                                "prompt_tokens": result.usage.prompt_tokens,
                                "completion_tokens": result.usage.completion_tokens,
                                "total_tokens": result.usage.total_tokens,
                                "cache_hit_tokens": result.usage.cache_hit_tokens,
                                "cache_hit_rate": round(result.usage.cache_hit_rate, 4),
                            },
                            "rounds": result.rounds,
                            "tool_limit_reached": result.tool_limit_reached,
                        }
            finally:
                # 审批被取消 / 客户端断开 / 任何异常退出：把还没回填的 tool_call
                # 补成「未执行」的结果，保证历史仍是合法序列（不会悬空）。
                try:
                    loop.abort_pending_tool_calls("本轮已中断，工具未执行。")
                except Exception:  # noqa: BLE001 - 收尾失败不能掩盖原始异常
                    logger.exception("中断收尾失败")
                provider.close()

    # ---- 内部 -----------------------------------------------------------
    def _loop_config(self) -> AppConfig:
        """拼「运行时才知道」的提示词增强：知识库文档名 + 附加工作区（挂载）清单。

        只在有内容时生效；会话自己设置过 system 提示词时（历史里已有 system 消息），
        ``AgentLoop`` 会优先用历史的，这里的增强不会覆盖用户的自定义。
        """
        additions: list[str] = []

        # ① 本地知识库文档清单
        try:
            names = [doc.name for doc in self.knowledge.list_documents() if doc.status == "ready"]
        except Exception:  # noqa: BLE001 - 知识库读失败不能拖垮对话
            names = []
        if names:
            shown = "、".join(names[:10]) + ("等" if len(names) > 10 else "")
            additions.append(
                "\n\n【本地知识库】当前知识库里有这些文档："
                f"{shown}。"
                "用户提到这些文档、或要求解析/总结/查找文档内容时，先调用 search_knowledge_base 检索；"
                "这些文件不在 workspace 里，不要用 list_dir / read_file 去猜路径。"
            )

        # ② 附加工作区（命名挂载）：不告诉模型，它就只会用相对路径，白配
        if self._extras:
            lines = []
            for mount in self._extras:
                perm = "可读写" if mount.writable else "只读"
                lines.append(f"- @{mount.name}/ -> {mount.path}（{perm}）")
            additions.append(
                "\n\n【附加工作区】除了主工作区，你还可以访问这些挂载目录"
                "（用 `@名字/路径` 的形式，例如 `read_file(\"@libs/http/client.py\")`）：\n"
                + "\n".join(lines)
                + "\n注意：相对路径只相对于主工作区解析；访问附加目录必须带 @名字 前缀，"
                "否则会被沙箱拒绝。只读挂载不能写入。"
            )

        if not additions:
            return self.config

        agent = self.config.agent.model_copy(
            update={
                "system_prompt": self.config.system_prompt_text() + "".join(additions),
                # 置空文件路径，确保用的是上面这段拼好的文本
                "system_prompt_file": "",
            }
        )
        return self.config.model_copy(update={"agent": agent})

    @contextmanager
    def _storage(self) -> Iterator[Storage]:
        storage = Storage(self.config.db_path)
        try:
            yield storage
        finally:
            storage.close()

    def _collect_descriptions(self) -> dict[str, str]:
        """所有工具的描述都从 registry 取（含未启用的），避免在 backend 里抄一份文案。

        做法：拿一份「全工具启用」的探针配置再 build 一次注册表 —— 只构造、不执行。
        """
        tools = self.config.tools.model_copy(update={"enabled": list(KNOWN_TOOLS)})
        probe = build_registry(self.config.model_copy(update={"tools": tools}))
        return {
            schema["function"]["name"]: schema["function"]["description"]
            for schema in probe.schemas()
        }

    def _first_user_messages(self, session_ids: Sequence[str]) -> dict[str, str]:
        """一次查询取出每个会话的**第一条** user 消息（截断 60 字），给前端做标题兜底。

        为什么不用 ``Storage.get_messages``：它的 ``limit`` 只能取**最近** N 条，拿不到「第一条」；
        给 storage 加「取前 N 条」的新方法属于改 ``agent/``（红线），所以这里按已知 schema
        直接做一次**只读**查询 —— 一条 SQL 覆盖整页，命中现有索引 ``idx_messages_session_seq``。
        """
        ids = [session_id for session_id in session_ids if session_id]
        if not ids:
            return {}

        placeholders = ",".join("?" for _ in ids)
        sql = (
            "SELECT m.session_id AS session_id, m.content AS content "
            "FROM messages m "
            f"WHERE m.session_id IN ({placeholders}) AND m.role = 'user' "
            "AND m.seq = ("
            "  SELECT MIN(x.seq) FROM messages x "
            "  WHERE x.session_id = m.session_id AND x.role = 'user'"
            ")"
        )

        try:
            rows = self._read_only_rows(sql, ids)
        except sqlite3.Error:
            # 库还不存在 / 结构异常：标题兜底失败不该影响会话列表
            return {}
        return {str(row[0]): str(row[1] or "")[:60] for row in rows}

    def _read_only_rows(
        self, sql: str, params: Sequence[str]
    ) -> list[tuple[Any, ...]]:
        """用只读连接跑查询（不写、不建表、不触发迁移）。"""
        path = Path(self.config.db_path)
        if not path.exists():
            return []
        connection: sqlite3.Connection | None = None
        try:
            try:
                connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            except sqlite3.Error:  # 路径里有特殊字符时 URI 形式可能打不开，退回普通连接
                connection = sqlite3.connect(str(path), timeout=5)
            return list(connection.execute(sql, tuple(params)).fetchall())
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _to_session_info(record: SessionRecord, first_user_message: str = "") -> SessionInfo:
        return SessionInfo(
            id=record.id,
            title=record.title,
            model=record.model,
            created_at=record.created_at,
            updated_at=record.updated_at,
            message_count=record.message_count,
            total_tokens=record.total_tokens,
            first_user_message=first_user_message,
        )