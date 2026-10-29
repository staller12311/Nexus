"""HTTP 契约（pydantic 模型）。响应模型写全，``/docs`` 才有的看。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelOut(BaseModel):
    name: str = Field(description="配置里的模型键名，例如 deepseek")
    model: str = Field(description="上游真实模型名")
    base_url: str
    api_key_env: str = Field(description="密钥所在的环境变量名（永远不返回密钥值）")
    key_set: bool = Field(description="该环境变量是否已设置")
    is_default: bool = Field(description="是否为 default_model")
    is_custom: bool = Field(default=False, description="是否是 Web 端添加的自定义模型")
    has_key: bool = Field(default=True, description="是否已有可用密钥")
    api_key_masked: str = Field(
        default="", description="脱敏后的密钥，例如 sk-****abcd；内置模型为空"
    )


class CustomModelIn(BaseModel):
    name: str = Field(description="唯一标识，用于下拉框与会话记录")
    base_url: str = Field(description="OpenAI 兼容端点，尾部斜杠会被自动去掉")
    model: str = Field(description="上游真实 model id")
    api_key: str = Field(description="完整密钥；只存库，绝不回传")


class CustomModelOut(BaseModel):
    name: str
    base_url: str
    model: str
    has_key: bool
    api_key_masked: str
    created_at: str
    updated_at: str


class CustomModelsOut(BaseModel):
    models: list[CustomModelOut]


class CustomModelDeletedOut(BaseModel):
    deleted: bool
    name: str
    referenced_sessions: int = Field(
        default=0, description="有多少会话正在用这个模型（删除前提示用）"
    )


class FetchModelsIn(BaseModel):
    base_url: str = Field(description="OpenAI 兼容端点，会拼成 {base_url}/models")
    api_key: str = Field(description="只用于这一次拉取请求，不落库、不打日志")


class FetchModelsOut(BaseModel):
    base_url: str = Field(description="实际请求的 /models 地址")
    models: list[str]
    count: int


class KnowledgeDocOut(BaseModel):
    id: str
    name: str
    size: int
    pages: int
    chunk_count: int
    status: str = Field(description="parsing / ready / failed")
    mode: str = Field(description="keyword（BM25）或 hybrid（含向量）")
    error: str | None = None
    created_at: str


class KnowledgeDocsOut(BaseModel):
    docs: list[KnowledgeDocOut]


class KnowledgeStatusOut(BaseModel):
    docs: int
    chunks: int
    mode: str
    supported: list[str] = Field(description="允许上传的扩展名")
    max_file_mb: int
    pipeline: str = Field(description="当前检索管线的人话说明")


class ExtraWorkspaceIn(BaseModel):
    name: str = Field(description="挂载名（小写字母/数字开头，可含 - 和 _，最长 16）")
    path: str = Field(description="附加目录绝对路径，例如 D:\\\\SharedLibs")
    writable: bool = Field(default=False, description="是否允许写；默认只读")


class ExtraWorkspaceOut(BaseModel):
    name: str
    path: str
    writable: bool


class WorkspaceIn(BaseModel):
    path: str = Field(description="工作区绝对路径，例如 D:\\\\MyProject")
    extras: list[ExtraWorkspaceIn] = Field(
        default_factory=list,
        description="附加工作区（@name 命名挂载，默认只读）",
    )


class WorkspaceOut(BaseModel):
    workspace: str = Field(description="当前生效的工作区绝对路径")
    default_workspace: str = Field(description="config.yaml 里的默认工作区")
    is_default: bool
    extras: list[ExtraWorkspaceOut] = Field(
        default_factory=list, description="附加工作区（命名挂载）"
    )


class ToolsOut(BaseModel):
    tools: list["ToolOut"]


class ToolOut(BaseModel):
    name: str
    enabled: bool
    default_disabled: bool = Field(description="是否属于「默认禁用、需配白名单」的工具")
    description: str


class SessionOut(BaseModel):
    id: str
    title: str
    model: str
    created_at: str
    updated_at: str
    message_count: int
    total_tokens: int
    first_user_message: str = Field(
        default="", description="title 为空时给前端的标题兜底：第一条 user 消息（截断）"
    )


class SessionsOut(BaseModel):
    sessions: list[SessionOut]


class MessageOut(BaseModel):
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    #: 思维链（旧库/旧消息为 None）；只在会话详情里返回，不会回传给模型
    reasoning_content: str | None = None


class SessionDetailOut(BaseModel):
    session: SessionOut
    messages: list[MessageOut]
    system_prompt: str = Field(
        default="", description="当前会话生效的 system 消息（供前端预填设置框）"
    )


class SessionCreateIn(BaseModel):
    model: str | None = Field(default=None, description="缺省用 default_model")
    title: str = ""


class SessionUpdateIn(BaseModel):
    title: str = Field(default="", description="新标题；空串表示清空（前端会退回用首条消息兜底）")


class DeletedOut(BaseModel):
    deleted: bool
    session: SessionOut | None = None


class ChatRequest(BaseModel):
    session_id: str
    message: str
    model: str | None = Field(default=None, description="本轮临时切换模型，缺省用会话/默认模型")


class SystemPromptIn(BaseModel):
    system_prompt: str = Field(
        default="", description="新的系统提示词；空串表示显式置空（下一句生效）"
    )


class SystemPromptOut(BaseModel):
    session_id: str
    system_prompt: str
    created: bool = Field(description="True 表示原本没有 system 消息，这次是新建的")
    seq: int = Field(description="system 消息的 seq（已存在时为 -1）")


class TitleOut(BaseModel):
    session_id: str
    title: str
    generated: bool = Field(description="False 表示会话已有标题、这次没有重新生成")
    model: str


ToolsOut.model_rebuild()

class FsEntryOut(BaseModel):
    name: str
    path: str


class FsListingOut(BaseModel):
    path: str = Field(description="当前目录（空字符串表示还没进入任何目录）")
    parent: str | None = Field(default=None, description="上一级目录；到顶为 null")
    entries: list[FsEntryOut] = Field(default_factory=list)
    drives: list[str] = Field(default_factory=list)
    truncated: bool = False


class FsMkdirIn(BaseModel):
    parent: str = Field(description="父目录绝对路径")
    name: str = Field(description="新建的文件夹名（单段，不含分隔符）")
