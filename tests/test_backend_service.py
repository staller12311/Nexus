"""Web 后端 API 级冒烟测试（FastAPI ``TestClient``）。

覆盖三块最容易「接错了也能跑、但一用就坏」的接口：

1. 工作区切换（``/api/config/workspace``）：合法目录、系统目录拒绝、恢复默认、``@name`` 附加挂载；
2. 本地知识库（``/api/kb/*``）：上传 → 后台解析 → 轮询 ready → 删除；不支持的格式 400；
3. 自定义模型（``/api/models/custom``）：创建（脱敏）、列表、重名 409、删除 200 / 不存在 404，
   以及 ``GET /api/models`` 的内置 + 自定义合并。

**完全 hermetic**：配置与 SQLite 全部落在 ``tmp_path``，不读仓库 ``config.yaml``、
不碰 ``data/sessions.db``、不联网（模型 base_url 是假的本地地址，测试里从不真正发起模型调用）。
配置用 ``build_config`` 构造（同 ``tests/test_backend_tools.py``），service 在测试主线程里
创建、由 TestClient 从请求线程调用 —— 这正好复现了 uvicorn 下「单例跨线程」的真实形态，
也是 ``test_workspace_settings_concurrent_read_write`` 这条并发回归用例的背景。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.config import AppConfig, build_config
from backend import REPO_ROOT
from backend.main import app
from backend.service.agent_service import AgentService
from backend.service.workspace import WorkspaceSettings

ENABLED = ["read_file", "write_file", "list_dir", "get_time"]


def _make_config(tmp_path: Path) -> AppConfig:
    return build_config(
        {
            "default_model": "fake",
            "models": {
                "fake": {
                    "base_url": "http://127.0.0.1:8128/v1",
                    "model": "fake-model",
                    "api_key_env": "FAKE_API_KEY",
                }
            },
            "agent": {"workspace": str(tmp_path / "ws")},
            "tools": {"enabled": ENABLED},
            "storage": {"db_path": str(tmp_path / "sessions.db")},
        }
    )


@pytest.fixture()
def api(tmp_path: Path):
    """返回 ``(client, service, config)``；配置与库都在 tmp_path 里。

    service 在**测试主线程**创建（模拟 uvicorn 的单例），随后所有请求都由 TestClient
    在请求线程里执行。这正是曾经触发 ``sqlite3.ProgrammingError`` 的形态：修复前
    ``WorkspaceSettings`` 持有一条创建于主线程的长连接，工作区切换请求在别的线程里用它
    就会 500；现在是每次操作短连接，跨线程安全（见 ``workspace.py`` 与并发回归用例）。
    """
    (tmp_path / "ws").mkdir()
    config = _make_config(tmp_path)
    service = AgentService(config)
    app.state.service = service
    try:
        with TestClient(app) as client:
            yield client, service, config
    finally:
        app.state.service = None


@pytest.fixture()
def allowed_root(tmp_path: Path):
    """可被 ``validate_workspace`` 接受的目标目录。

    注意：pytest 的 ``tmp_path`` 位于用户主目录的 ``AppData`` 下，而工作区校验
    明确拒绝 ``AppData``（凭据/应用数据目录），所以切换目标得建在仓库的 ``workspace/``
    （已被 .gitignore 忽略）下，用例结束即删，避免污染工作树。
    """
    base = REPO_ROOT / "workspace" / f".pytest-ws-{tmp_path.name}"
    base.mkdir(parents=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- 元信息
def test_health_reports_runtime_diagnostics(api) -> None:
    client, _service, _config = api
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # 运行时诊断字段（README 第 62 条②）：诊断失败也应有一份结构化输出，而不是 500
    assert {"python", "integrity", "write_warning"} <= set(body)


def test_tools_include_injected_knowledge_and_web_search(api) -> None:
    client, _service, _config = api
    response = client.get("/api/tools")

    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()["tools"]}
    for name in ("search_knowledge_base", "web_search"):
        assert name in tools, f"{name} 由 AgentService 动态注入，接口里应该能看到"
        assert tools[name]["enabled"] is True
    # 内置工具仍按 tools.enabled 如实标记
    assert tools["read_file"]["enabled"] is True
    assert tools["run_shell"]["enabled"] is False


# ---------------------------------------------------------------- 工作区
def test_workspace_switch_and_get(api, allowed_root: Path) -> None:
    client, _service, _config = api
    project = allowed_root / "project_a"
    project.mkdir()

    response = client.post("/api/config/workspace", json={"path": str(project)})
    assert response.status_code == 200
    body = response.json()
    assert body["workspace"] == str(project.resolve())
    assert body["is_default"] is False
    assert body["extras"] == []

    # GET 返回的是同一个当前工作区
    assert client.get("/api/config/workspace").json()["workspace"] == str(project.resolve())


def test_workspace_rejects_system_directory(api) -> None:
    client, service, _config = api
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("windir")
    if not system_root:  # pragma: no cover - 非 Windows 环境
        pytest.skip("没有 SYSTEMROOT/windir，无法构造系统目录用例")

    before = service.workspace
    response = client.post("/api/config/workspace", json={"path": system_root})

    assert response.status_code == 400
    assert "工作区" in response.json()["detail"]
    # 拒绝时不做部分生效：工作区保持原样
    assert service.workspace == before


def test_workspace_reset_restores_default(api, allowed_root: Path) -> None:
    client, service, config = api
    project = allowed_root / "project_b"
    project.mkdir()

    assert client.post("/api/config/workspace", json={"path": str(project)}).status_code == 200
    response = client.delete("/api/config/workspace")

    assert response.status_code == 200
    body = response.json()
    assert body["workspace"] == str(config.workspace_path)
    assert body["is_default"] is True
    assert service.workspace == config.workspace_path


def test_workspace_extras_named_mount_roundtrip(api, allowed_root: Path) -> None:
    """``extras`` 多根挂载：POST 带 [{name,path,writable}] → 返回脱敏路径与只读标记。"""
    client, service, _config = api
    project = allowed_root / "project_c"
    libs = allowed_root / "shared_libs"
    project.mkdir()
    libs.mkdir()

    response = client.post(
        "/api/config/workspace",
        json={
            "path": str(project),
            "extras": [{"name": "libs", "path": str(libs), "writable": False}],
        },
    )

    assert response.status_code == 200
    extras = response.json()["extras"]
    assert extras == [{"name": "libs", "path": str(libs.resolve()), "writable": False}]
    # 内存里的服务状态与响应一致（挂载已生效，读工具会按 @libs/ 前缀解析）
    assert [mount.name for mount in service.extras] == ["libs"]


def test_workspace_settings_concurrent_read_write(tmp_path: Path) -> None:
    """并发回归：多线程读写工作区设置，不能抛 ``sqlite3.ProgrammingError``。

    背景：``WorkspaceSettings`` 曾经持有一条创建于主线程的长连接，而 Web 路由跑在
    uvicorn/TestClient 的线程池里；请求线程一换就会抛
    "SQLite objects created in a thread can only be used in that same thread"，
    并发切工作区直接 500。现在改成每次操作短连接，这里用 4 个线程并发读写作回归。
    """
    settings = WorkspaceSettings(tmp_path / "concurrent.db")
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)
    writes_per_thread = 20

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)  # 尽量让 4 个线程同时开跑
            for _ in range(writes_per_thread):
                settings.set(json.dumps({"root": f"D:\\ws{index}", "extras": []}))
                assert settings.get() is not None
        except BaseException as exc:  # noqa: BLE001 - 线程里的任何异常都要带回主线程断言
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,), name=f"ws-{index}") for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "工作区读写线程超时未结束"

    assert errors == [], f"并发读写抛出了异常：{errors!r}"

    # 并发结束后仍然能正确落库、读回（数据最终一致）
    final_value = json.dumps({"root": "D:\\final", "extras": []})
    settings.set(final_value)
    assert settings.get() == final_value
    settings.close()


# ---------------------------------------------------------------- 知识库
def _wait_for_kb_doc(client: TestClient, doc_id: str, timeout: float = 10.0) -> dict:
    """轮询到文档 ready（后台线程解析），失败则给出状态里的 error 信息。"""
    deadline = time.time() + timeout
    last_status = "unknown"
    while time.time() < deadline:
        docs = client.get("/api/kb/docs").json()["docs"]
        for doc in docs:
            if doc["id"] == doc_id:
                last_status = doc["status"]
                if doc["status"] == "ready":
                    return doc
                if doc["status"] == "failed":
                    raise AssertionError(f"文档解析失败：{doc.get('error')}")
        time.sleep(0.1)
    raise AssertionError(f"文档 {doc_id} 在 {timeout}s 内没有 ready（最后状态 {last_status}）")


def test_knowledge_upload_parse_then_delete(api) -> None:
    client, _service, _config = api

    response = client.post(
        "/api/kb/docs",
        files={
            "file": (
                "demo.txt",
                b"Project notes.\n\nThe scheduler stores tasks in SQLite, "
                b"and the web layer is FastAPI.",
                "text/plain",
            )
        },
    )
    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "demo.txt"
    assert created["status"] in {"parsing", "ready"}

    doc = _wait_for_kb_doc(client, created["id"])
    assert doc["chunk_count"] >= 1

    status = client.get("/api/kb/status").json()
    assert status["docs"] == 1
    assert status["chunks"] >= 1
    assert status["mode"] == "keyword"
    assert ".txt" in status["supported"]

    deleted = client.delete(f"/api/kb/docs/{created['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["id"] == created["id"]
    assert client.get("/api/kb/docs").json()["docs"] == []
    assert client.get("/api/kb/status").json()["docs"] == 0


def test_knowledge_rejects_unsupported_format(api) -> None:
    client, _service, _config = api

    response = client.post(
        "/api/kb/docs",
        files={"file": ("virus.exe", b"MZ fake executable", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert "不支持的文件类型" in response.json()["detail"]
    assert client.get("/api/kb/docs").json()["docs"] == []


# ---------------------------------------------------------------- 自定义模型
MODEL_PAYLOAD = {
    "name": "my-gateway",
    "base_url": "http://127.0.0.1:8128/v1/",
    "model": "gateway-model",
    "api_key": "sk-test-abcdef123456",
}


def test_custom_model_crud_and_masking(api) -> None:
    client, _service, _config = api

    response = client.post("/api/models/custom", json=MODEL_PAYLOAD)
    assert response.status_code == 201
    created = response.json()
    assert created["name"] == "my-gateway"
    assert created["base_url"] == "http://127.0.0.1:8128/v1"  # 尾部斜杠被去掉
    assert created["has_key"] is True
    # 明文 Key 只存库、绝不出现在响应里；对外只有掩码
    assert created["api_key_masked"] == "sk-****3456"
    assert MODEL_PAYLOAD["api_key"] not in response.text

    listed = client.get("/api/models/custom").json()["models"]
    assert [item["name"] for item in listed] == ["my-gateway"]
    assert listed[0]["api_key_masked"] == "sk-****3456"
    assert MODEL_PAYLOAD["api_key"] not in client.get("/api/models/custom").text

    # /api/models 合并内置 + 自定义
    models = {item["name"]: item for item in client.get("/api/models").json()}
    assert {"fake", "my-gateway"} <= set(models)
    assert models["fake"]["is_custom"] is False
    assert models["my-gateway"]["is_custom"] is True
    assert models["my-gateway"]["api_key_masked"] == "sk-****3456"
    assert models["my-gateway"]["key_set"] is True

    deleted = client.delete("/api/models/custom/my-gateway")
    assert deleted.status_code == 200
    assert deleted.json() == {
        "deleted": True,
        "name": "my-gateway",
        "referenced_sessions": 0,
    }
    assert client.get("/api/models/custom").json()["models"] == []


def test_custom_model_duplicate_and_missing_delete(api) -> None:
    client, _service, _config = api

    assert client.post("/api/models/custom", json=MODEL_PAYLOAD).status_code == 201
    duplicate = client.post("/api/models/custom", json=MODEL_PAYLOAD)
    assert duplicate.status_code == 409
    assert "已存在" in duplicate.json()["detail"]

    missing = client.delete("/api/models/custom/no-such-model")
    assert missing.status_code == 404
    assert "找不到" in missing.json()["detail"]

# ---------------------------------------------------------------- 用量统计
def test_stats_endpoint_aggregates_usage_and_cache_rate(api) -> None:
    """GET /api/stats：跨会话聚合 token 用量，并按 prompt 算整体缓存命中率。"""
    client, service, _config = api
    with service._storage() as storage:
        first = storage.create_session("fake")
        storage.add_usage(
            first.id,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cache_hit_tokens=80,
        )
        second = storage.create_session("fake")
        storage.add_usage(
            second.id,
            prompt_tokens=50,
            completion_tokens=10,
            total_tokens=60,
            cache_hit_tokens=25,
        )

    body = client.get("/api/stats").json()
    assert body["sessions"] == 2
    assert body["prompt_tokens"] == 150
    assert body["completion_tokens"] == 30
    assert body["total_tokens"] == 180
    assert body["cache_hit_tokens"] == 105
    assert body["cache_hit_rate"] == 0.7  # 105 / 150


def test_session_detail_returns_persisted_usage(api) -> None:
    """会话详情要带每条 assistant 消息的当轮 usage（含缓存命中），供前端回放。"""
    client, service, _config = api
    with service._storage() as storage:
        session = storage.create_session("fake")
        storage.append_message(session.id, {"role": "user", "content": "hi"})
        storage.append_message(
            session.id,
            {"role": "assistant", "content": "yo"},
            usage={
                "prompt_tokens": 24,
                "completion_tokens": 12,
                "total_tokens": 36,
                "cache_hit_tokens": 18,
            },
        )

    messages = client.get(f"/api/sessions/{session.id}").json()["messages"]
    assistant = [message for message in messages if message["role"] == "assistant"][0]
    assert assistant["usage"]["cache_hit_tokens"] == 18
    assert assistant["usage"]["prompt_tokens"] == 24


def test_models_expose_context_window(api) -> None:
    client, _service, _config = api
    models = {item["name"]: item for item in client.get("/api/models").json()}
    assert models["fake"]["context_window"] > 0

def test_stats_session_scoped_uses_prompt_only_denominator(api) -> None:
    """命中率分母只能是 prompt_tokens：completion 再大也不能把命中率稀释。"""
    client, service, _config = api
    with service._storage() as storage:
        session = storage.create_session("fake")
        storage.add_usage(
            session.id,
            prompt_tokens=1000,
            completion_tokens=500,
            total_tokens=1500,
            cache_hit_tokens=940,
        )

    body = client.get(f"/api/stats?session_id={session.id}").json()
    assert body["session_id"] == session.id
    assert body["sessions"] == 1
    assert body["prompt_tokens"] == 1000
    assert body["completion_tokens"] == 500
    assert body["total_tokens"] == 1500
    assert body["cache_hit_tokens"] == 940
    # 940 / 1000 = 0.94，而不是 940 / 1500 = 0.63
    assert body["cache_hit_rate"] == 0.94


def test_stats_unknown_session_returns_404(api) -> None:
    client, _service, _config = api
    assert client.get("/api/stats?session_id=nope").status_code == 404

# ---------------------------------------------------------------- 人工审批
class _FakeProvider:
    """最小 provider 替身：按脚本返回 assistant(tool_calls) → 最终文本。"""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.closed = False

    def chat_stream(self, messages, tools=None, **kwargs):
        step = self.script.pop(0)
        if step.content:
            yield {"type": "text", "text": step.content}
        yield {
            "type": "done",
            "content": step.content,
            "reasoning": None,
            "tool_calls": step.tool_calls,
            "usage": step.usage,
        }

    def close(self) -> None:
        self.closed = True


def test_approve_unknown_id_returns_404(api) -> None:
    client, _service, _config = api
    response = client.post(
        "/api/chat/approve", json={"approval_id": "ap_missing", "approved": True}
    )
    assert response.status_code == 404


def test_approve_twice_returns_409(api) -> None:
    client, service, _config = api
    pending = service.approvals.open(
        session_id="s-approve",
        tool_call_id="call_1",
        name="write_file",
        timeout=5,
    )
    first = client.post(
        "/api/chat/approve", json={"approval_id": pending.approval_id, "approved": True}
    )
    assert first.status_code == 200
    assert first.json()["approved"] is True
    assert pending.gate.wait() is True

    second = client.post(
        "/api/chat/approve", json={"approval_id": pending.approval_id, "approved": False}
    )
    assert second.status_code == 409
    service.approvals.discard(pending.approval_id)


def test_rejected_tool_is_not_executed_but_history_stays_valid(api, monkeypatch) -> None:
    """服务层串起来：审批拒绝 → 工具不执行 → tool 消息回灌 → 历史合法可继续。"""
    from agent.provider import ChatResult, ToolCall

    _client, service, config = api
    service.config.tools.require_approval = True
    service.config.tools.approval_required = ["write_file"]

    script = [
        ChatResult(
            content=None,
            tool_calls=(
                ToolCall("call_1", "write_file", '{"path": "blocked.txt", "content": "x"}'),
            ),
        ),
        ChatResult(content="好的，我不写了。"),
    ]
    monkeypatch.setattr(service, "_provider_for", lambda name: _FakeProvider(script))

    session = service.create_session("fake")
    handled: list[str] = []

    def approval(call) -> bool:
        handled.append(call.name)
        return False

    events = list(service.chat_stream(session.id, "写个文件", approval_handler=approval))
    assert handled == ["write_file"]
    assert any(event["type"] == "message_end" for event in events)
    assert not (config.workspace_path / "blocked.txt").exists()

    _info, messages = service.get_session(session.id)
    tool_messages = [
        message for message in messages if message.get("role") == "tool"
    ]
    assert tool_messages[-1]["content"] == "用户已拒绝此操作，工具未执行。"
    assistant = [message for message in messages if message.get("role") == "assistant"]
    assert assistant[0]["tool_calls"][0]["id"] == "call_1"


def test_optional_dependency_warning(capsys, monkeypatch) -> None:
    from backend import main as backend_main

    monkeypatch.setattr(
        backend_main, "check_optional_dependencies", lambda: ["python-docx", "pypdf"]
    )
    missing = backend_main.warn_missing_optional_dependencies()
    captured = capsys.readouterr()
    assert missing == ["python-docx", "pypdf"]
    assert "缺少依赖" in captured.err
    assert "pip install -r requirements.txt" in captured.err

    # 依赖齐全时保持安静
    monkeypatch.setattr(backend_main, "check_optional_dependencies", lambda: [])
    assert backend_main.warn_missing_optional_dependencies() == []
    assert "缺少依赖" not in capsys.readouterr().err
