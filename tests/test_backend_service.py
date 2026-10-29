"""Web 后端 API 级冒烟测试（FastAPI ``TestClient``）。

覆盖三块最容易「接错了也能跑、但一用就坏」的接口：

1. 工作区切换（``/api/config/workspace``）：合法目录、系统目录拒绝、恢复默认、``@name`` 附加挂载；
2. 本地知识库（``/api/kb/*``）：上传 → 后台解析 → 轮询 ready → 删除；不支持的格式 400；
3. 自定义模型（``/api/models/custom``）：创建（脱敏）、列表、重名 409、删除 200 / 不存在 404，
   以及 ``GET /api/models`` 的内置 + 自定义合并。

**完全 hermetic**：配置与 SQLite 全部落在 ``tmp_path``，不读仓库 ``config.yaml``、
不碰 ``data/sessions.db``、不联网（模型 base_url 是假的本地地址，测试里从不真正发起模型调用）。
配置用 ``build_config`` 构造（同 ``tests/test_backend_tools.py``），但 ``AgentService``
故意在**请求线程**里懒创建（原因见下面 ``api`` fixture 的注释），而不是在主线程预建。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.config import AppConfig, build_config
from backend import REPO_ROOT
from backend.main import app
from backend.service.agent_service import AgentService

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
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """返回 ``(client, service, config)``；配置与库都在 tmp_path 里。

    注意构造时机：``TestClient`` 的同步路由跑在 anyio 工作线程里，而
    ``AgentService`` 内部有一个长期存活的 sqlite 连接（``WorkspaceSettings``，
    默认 ``check_same_thread=True``）。如果像普通单测那样在主线程构造 service，
    工作区切换接口会在 sqlite 层因「跨线程使用连接」而 500。这里用 context manager
    固定一个 portal（实测所有请求落在同一工作线程），并把 ``AgentService.load``
    替换成「用本用例的 tmp 配置构造」，让 service 在**请求线程**里懒创建 ——
    既绕开 ``load_config()`` 读仓库配置，也复现了 uvicorn 下真实的服务创建路径。
    """
    (tmp_path / "ws").mkdir()
    config = _make_config(tmp_path)
    created: dict[str, AgentService] = {}

    def fake_load(cls, config_path: str | None = None) -> AgentService:
        service = AgentService(config)
        created["service"] = service
        return service

    monkeypatch.setattr(AgentService, "load", classmethod(fake_load))
    app.state.service = None
    try:
        with TestClient(app) as client:
            client.get("/api/tools")  # 预热：在请求线程里把 service 建出来
            yield client, created["service"], config
    finally:
        app.state.service = None
        service = created.get("service")
        if service is not None:
            try:
                service.workspace_settings.close()
            except sqlite3.ProgrammingError:
                # 连接是在请求线程里创建的；在测试主线程 close 会触发 sqlite 的
                # 跨线程检查。不在这里强行绕过（那属于业务代码的线程模型问题），
                # 留下连接交给 GC 回收即可 —— tmp 库随 tmp_path 生命周期结束。
                pass


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