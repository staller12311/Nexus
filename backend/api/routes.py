"""REST + SSE 路由。

所有端点都只是「取 service → 调 agent → 转成 JSON」，不含任何智能体逻辑。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from dataclasses import asdict
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from agent.config import ConfigError
from agent.logging_setup import register_secret
from agent.storage import StorageError

from ..service import AgentService
from ..service.custom_models import CustomModel, CustomModelError, DuplicateCustomModel
from ..service.knowledge import (
    MAX_FILE_BYTES,
    SUPPORTED_SUFFIXES,
    KnowledgeError,
)
from ..service.fs_browser import FsBrowseError, create_directory, list_directories
from ..service.workspace import WorkspaceError
from ..service.model_fetch import FetchModelsError, fetch_models
from ..service.events import EventBridge, encode_event
from .schemas import (
    ChatRequest,
    CustomModelDeletedOut,
    CustomModelIn,
    CustomModelsOut,
    CustomModelOut,
    DeletedOut,
    FetchModelsIn,
    FetchModelsOut,
    KnowledgeDocOut,
    KnowledgeDocsOut,
    KnowledgeStatusOut,
    MessageOut,
    ModelOut,
    SessionCreateIn,
    SessionDetailOut,
    SessionOut,
    SessionsOut,
    SessionUpdateIn,
    StatsOut,
    SystemPromptIn,
    SystemPromptOut,
    TitleOut,
    ExtraWorkspaceOut,
    FsEntryOut,
    FsListingOut,
    FsMkdirIn,
    WorkspaceIn,
    WorkspaceOut,
    ToolOut,
    ToolsOut,
)

router = APIRouter(prefix="/api")
logger = logging.getLogger("agent.web")

#: 事件队列的轮询粒度：既保证工具事件及时送达，又不至于空转
_POLL_SECONDS = 0.1


def get_service(request: Request) -> AgentService:
    """从 app.state 取单例；首次访问时才加载配置（配置缺失也不影响 /docs 可用）。"""
    service: AgentService | None = getattr(request.app.state, "service", None)
    if service is None:
        try:
            service = AgentService.load()
        except ConfigError as exc:
            raise HTTPException(status_code=503, detail=f"配置不可用：{exc}") from exc
        request.app.state.service = service
    return service


# ---------------------------------------------------------------- 只读
@router.get("/models", response_model=ModelOut | list[ModelOut], summary="已配置的模型")
def list_models(request: Request) -> list[ModelOut]:
    service = get_service(request)
    return [ModelOut(**asdict(info)) for info in service.list_models()]


@router.get("/tools", response_model=ToolsOut, summary="全部工具及启用状态")
def list_tools(request: Request) -> ToolsOut:
    service = get_service(request)
    return ToolsOut(tools=[ToolOut(**asdict(info)) for info in service.list_tools()])


@router.get(
    "/stats",
    response_model=StatsOut,
    summary="token 用量统计（默认按当前会话；不传 session_id 则为全库）",
)
def get_stats(request: Request, session_id: str | None = Query(default=None)) -> StatsOut:
    service = get_service(request)
    try:
        return StatsOut(**service.stats(session_id))
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------- 自定义模型
def _custom_model_out(model: CustomModel) -> CustomModelOut:
    """对外表示：**只给掩码**，原始 Key 不出 backend。"""
    return CustomModelOut(
        name=model.name,
        base_url=model.base_url,
        model=model.model,
        has_key=bool(model.api_key.strip()),
        api_key_masked=model.masked_key,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


@router.get("/models/custom", response_model=CustomModelsOut, summary="自定义模型列表（脱敏）")
def list_custom_models(request: Request) -> CustomModelsOut:
    service = get_service(request)
    return CustomModelsOut(models=[_custom_model_out(item) for item in service.list_custom_models()])


@router.post(
    "/models/custom",
    response_model=CustomModelOut,
    status_code=201,
    summary="添加自定义模型",
)
def create_custom_model(request: Request, payload: CustomModelIn) -> CustomModelOut:
    service = get_service(request)
    try:
        created = service.add_custom_model(
            payload.name, payload.base_url, payload.model, payload.api_key
        )
    except DuplicateCustomModel as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CustomModelError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 密钥登记给日志脱敏器：万一别处把它打进日志，会被替换成 ***（README 第 44 条）
    register_secret(payload.api_key)
    logger.info(
        "添加自定义模型 name=%s model=%s base_url=%s key=%s",
        created.name,
        created.model,
        created.base_url,
        created.masked_key,
    )
    return _custom_model_out(created)


@router.delete(
    "/models/custom/{name}",
    response_model=CustomModelDeletedOut,
    summary="删除自定义模型",
)
def delete_custom_model(request: Request, name: str) -> CustomModelDeletedOut:
    service = get_service(request)
    try:
        result = service.delete_custom_model(name)
    except CustomModelError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("删除自定义模型 name=%s 被 %d 个会话引用", result["name"], result["referenced_sessions"])
    return CustomModelDeletedOut(**result)


@router.post(
    "/models/fetch",
    response_model=FetchModelsOut,
    summary="按 OpenAI 协议拉取上游模型列表（GET {base_url}/models）",
)
def fetch_model_list(payload: FetchModelsIn) -> FetchModelsOut:
    """一次性请求：Key 只在内存里用一次，不落库、不进日志。"""
    try:
        result = fetch_models(payload.base_url, payload.api_key)
    except FetchModelsError as exc:
        # 400：地址/密钥本身有问题；502：上游不接受这个请求
        status = 400 if "不能为空" in str(exc) or "必须是合法" in str(exc) else 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return FetchModelsOut(**result)


@router.get("/sessions", response_model=SessionsOut, summary="会话列表")
def list_sessions(
    request: Request, limit: int = Query(default=20, ge=1, le=100)
) -> SessionsOut:
    service = get_service(request)
    return SessionsOut(
        sessions=[SessionOut(**asdict(info)) for info in service.list_sessions(limit=limit)]
    )


@router.get(
    "/sessions/{session_id}", response_model=SessionDetailOut, summary="会话详情（消息序列）"
)
def get_session(request: Request, session_id: str) -> SessionDetailOut:
    service = get_service(request)
    try:
        info, messages = service.get_session(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    system_prompt = next(
        (str(item.get("content") or "") for item in messages if item.get("role") == "system"),
        "",
    )
    return SessionDetailOut(
        session=SessionOut(**asdict(info)),
        messages=[MessageOut(**message) for message in messages],
        system_prompt=system_prompt,
    )


@router.patch("/sessions/{session_id}", response_model=SessionOut, summary="重命名会话")
def rename_session(request: Request, session_id: str, payload: SessionUpdateIn) -> SessionOut:
    service = get_service(request)
    try:
        info = service.rename_session(session_id, payload.title)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SessionOut(**asdict(info))


@router.delete("/sessions/{session_id}", response_model=DeletedOut, summary="删除会话")
def delete_session(request: Request, session_id: str) -> DeletedOut:
    service = get_service(request)
    try:
        info = service.delete_session(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("删除会话 session=%s，消息级联删除", info.id)
    return DeletedOut(deleted=True, session=SessionOut(**asdict(info)))


@router.post(
    "/sessions/{session_id}/generate_title",
    response_model=TitleOut,
    summary="用一次短超时的模型调用给会话起标题（失败不阻塞主流程）",
)
def generate_title(request: Request, session_id: str, force: bool = False) -> TitleOut:
    service = get_service(request)
    try:
        result = service.generate_title(session_id, force=force)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 起标题失败一律 503，让前端静默降级
        logger.warning("自动起标题失败 session=%s：%s", session_id, exc)
        raise HTTPException(status_code=503, detail="标题生成失败，已保留原标题") from exc
    if result["generated"]:
        logger.info("自动起标题 session=%s title=%r", result["session_id"][:8], result["title"])
    return TitleOut(**result)


@router.patch(
    "/sessions/{session_id}/system_prompt",
    response_model=SystemPromptOut,
    summary="修改当前会话的系统提示词（下一句对话立即生效）",
)
def update_system_prompt(
    request: Request, session_id: str, payload: SystemPromptIn
) -> SystemPromptOut:
    service = get_service(request)
    try:
        result = service.set_system_prompt(session_id, payload.system_prompt)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except OSError as exc:  # 库文件被占用/不可写
        raise HTTPException(status_code=500, detail=f"写入系统提示词失败：{exc}") from exc
    logger.info(
        "更新系统提示词 session=%s created=%s len=%d",
        result["session_id"],
        result["created"],
        len(result["system_prompt"]),
    )
    return SystemPromptOut(**result)


@router.post(
    "/sessions",
    response_model=SessionOut,
    status_code=201,
    summary="新建会话",
)
def create_session(request: Request, payload: SessionCreateIn) -> SessionOut:
    service = get_service(request)
    try:
        info = service.create_session(model=payload.model, title=payload.title)
    except (ConfigError, StorageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SessionOut(**asdict(info))


# ---------------------------------------------------------------- SSE
@router.post(
    "/chat/stream",
    summary="流式对话（SSE）",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
def chat_stream(request: Request, payload: ChatRequest) -> StreamingResponse:
    service = get_service(request)

    # 能在开始推流之前做的校验，就用普通 HTTP 状态码返回（前端更好处理）
    if not (payload.message or "").strip():
        raise HTTPException(status_code=400, detail="消息不能为空")
    try:
        service.get_session(payload.session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    generator = _stream_chat(
        service,
        session_id=payload.session_id,
        message=payload.message.strip(),
        model=payload.model,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉 nginx 之类反代的缓冲，保证事件逐条到达
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/sessions/{session_id}/regenerate",
    summary="重新生成上一轮回答（SSE，覆盖旧回答）",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
def regenerate_stream(request: Request, session_id: str, model: str | None = Query(default=None)):
    """删掉「最后一条 user 及其之后」的消息再重跑（README 第 53 条）。"""
    service = get_service(request)
    try:
        service.get_session(session_id)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    generator = _stream_chat(
        service,
        session_id=session_id,
        message=None,
        model=model,
        regenerate=True,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_chat(
    service: AgentService,
    *,
    session_id: str,
    message: str | None,
    model: str | None,
    regenerate: bool = False,
) -> AsyncIterator[str]:
    """把一次同步的流式问答转成 SSE 事件流。

    断开语义（README 第 35 条）：客户端断开时本生成器结束，但工作线程里的
    ``chat_stream()`` 会继续跑完，消息与 usage 仍然落库 —— 丢的只是实时事件流。

    ``regenerate=True`` 时忽略 ``message``，由 service 自己从库里取最后一条 user 文本
    并在同一事务里清掉旧回答（第 53 条）。
    """
    loop = asyncio.get_running_loop()
    bridge = EventBridge(loop)

    def on_tool_call(call: Any) -> None:
        bridge.emit(
            {
                "type": "tool_call_start",
                "tool_call_id": call.id,
                "name": call.name,
                # 原样透传模型给的 JSON 字符串，后端不解析再序列化（README 第 21 条）
                "arguments": call.arguments,
            }
        )

    def on_tool_result(call: Any, output: str) -> None:
        bridge.emit(
            {
                "type": "tool_call_end",
                "tool_call_id": call.id,
                "name": call.name,
                "ok": not output.startswith("错误："),
                "content": output,
            }
        )

    yield encode_event({"type": "message_start", "session_id": session_id})

    def run_sync() -> None:
        """在工作线程里消费真正的流，逐事件投递到 asyncio 队列。"""
        try:
            events = (
                service.regenerate_stream(
                    session_id,
                    model=model,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                if regenerate
                else service.chat_stream(
                    session_id,
                    message or "",
                    model=model,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
            )
            for event in events:
                bridge.emit(event)
        except Exception as exc:  # noqa: BLE001 - 交给 async 侧转成 error 事件
            bridge.emit(
                {"type": "error", "message": str(exc), "code": type(exc).__name__}
            )

    task = asyncio.create_task(asyncio.to_thread(run_sync))

    try:
        while True:
            event = await bridge.receive(_POLL_SECONDS)
            if event is not None:
                yield encode_event(event)
                continue
            if task.done():
                break
        for leftover in bridge.drain():
            yield encode_event(leftover)
        await task
    except Exception as exc:  # noqa: BLE001 - 任何失败都以 error 事件告知前端
        logger.exception("对话失败")
        yield encode_event(
            {
                "type": "error",
                "message": str(exc),
                "code": type(exc).__name__,
            }
        )


# ---- 目录选择器（README 第 60 条） ----------------------------------------
@router.get("/fs/dirs", response_model=FsListingOut, summary="列出某个目录下的子目录（用于选择工作区）")
def list_fs_dirs(path: str = "") -> FsListingOut:
    try:
        listing = list_directories(path)
    except FsBrowseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FsListingOut(
        path=listing.path,
        parent=listing.parent,
        entries=[FsEntryOut(name=entry.name, path=entry.path) for entry in listing.entries],
        drives=list(listing.drives),
        truncated=listing.truncated,
    )


@router.post("/fs/mkdir", response_model=FsEntryOut, summary="在指定目录下新建文件夹")
def make_fs_dir(payload: FsMkdirIn) -> FsEntryOut:
    try:
        created = create_directory(payload.parent, payload.name)
    except FsBrowseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("已创建文件夹 %s", created)
    return FsEntryOut(name=Path(created).name, path=created)


# ---- 工作区切换（Workspace Switcher，README 第 58/59 条） ------------------
def _workspace_out(service: AgentService) -> WorkspaceOut:
    current = service.workspace
    default = service.default_workspace
    return WorkspaceOut(
        workspace=str(current),
        default_workspace=str(default),
        is_default=current == default,
        extras=[
            ExtraWorkspaceOut(name=mount.name, path=str(mount.path), writable=mount.writable)
            for mount in service.extras
        ],
    )


@router.get("/config/workspace", response_model=WorkspaceOut, summary="当前工作区与附加挂载")
def get_workspace(request: Request) -> WorkspaceOut:
    return _workspace_out(get_service(request))


@router.post(
    "/config/workspace",
    response_model=WorkspaceOut,
    summary="切换工作区（绝对路径，可带 @name 附加挂载）",
)
def set_workspace(request: Request, payload: WorkspaceIn) -> WorkspaceOut:
    service = get_service(request)
    try:
        resolved = service.set_workspace(
            payload.path,
            [
                {"name": extra.name, "path": extra.path, "writable": extra.writable}
                for extra in payload.extras
            ],
        )
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("工作区已切换到 %s（附加挂载 %d 个）", resolved, len(payload.extras))
    return _workspace_out(service)


@router.delete("/config/workspace", response_model=WorkspaceOut, summary="恢复默认工作区")
def reset_workspace(request: Request) -> WorkspaceOut:
    service = get_service(request)
    resolved = service.reset_workspace()
    logger.info("工作区已恢复默认 %s", resolved)
    return _workspace_out(service)


# ---- 知识库（RAG） -------------------------------------------------------
@router.get("/kb/status", response_model=KnowledgeStatusOut, summary="知识库状态与检索管线")
def knowledge_status(request: Request) -> KnowledgeStatusOut:
    service = get_service(request)
    stats = service.knowledge.stats()
    mode = stats["mode"]
    pipeline = (
        "BM25 关键词检索（中文双字 + 英文整词）"
        if mode == "keyword"
        else "混合检索：0.7 向量余弦 + 0.3 BM25"
    )
    try:
        import pypdf  # noqa: F401

        parsed_pdf = True
    except ImportError:
        parsed_pdf = False
    try:
        import docx  # noqa: F401

        parsed_docx = True
    except ImportError:
        parsed_docx = False
    supported = [suffix for suffix in SUPPORTED_SUFFIXES if suffix not in {".pdf", ".docx"}]
    if parsed_pdf:
        supported.append(".pdf")
    if parsed_docx:
        supported.append(".docx")
    return KnowledgeStatusOut(
        docs=stats["docs"],
        chunks=stats["chunks"],
        mode=mode,
        supported=sorted(supported),
        max_file_mb=MAX_FILE_BYTES // (1024 * 1024),
        pipeline=pipeline,
    )


@router.get("/kb/docs", response_model=KnowledgeDocsOut, summary="列出知识库文档")
def knowledge_docs(request: Request) -> KnowledgeDocsOut:
    service = get_service(request)
    return KnowledgeDocsOut(docs=[KnowledgeDocOut(**vars(doc)) for doc in service.knowledge.list_documents()])


@router.post(
    "/kb/docs",
    response_model=KnowledgeDocOut,
    status_code=201,
    summary="上传文档（后台解析+切片，前端轮询状态）",
)
async def upload_knowledge_doc(request: Request, file: UploadFile = File(...)) -> KnowledgeDocOut:
    service = get_service(request)
    data = await file.read()
    try:
        document = service.knowledge.submit(file.filename or "unnamed", data)
    except KnowledgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("上传知识库文档 name=%s size=%d status=%s", document.name, document.size, document.status)
    return KnowledgeDocOut(**vars(document))


@router.delete("/kb/docs/{doc_id}", response_model=KnowledgeDocOut, summary="删除文档及其切片")
def delete_knowledge_doc(request: Request, doc_id: str) -> KnowledgeDocOut:
    service = get_service(request)
    try:
        document = service.knowledge.delete_document(doc_id)
    except KnowledgeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return KnowledgeDocOut(**vars(document))