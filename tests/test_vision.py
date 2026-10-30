"""多模态（图片）第一批：上传、Vision 协议、模型能力拦截、历史兼容。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.config import AppConfig, build_config
from agent.loop import AgentLoop
from agent.provider import Usage
from agent.storage import Storage
from backend.main import app
from backend.service.agent_service import AgentService
from backend.service.uploads import MAX_IMAGE_BYTES, UploadStore
from backend.service.vision import build_vision_transform

#: 合法 PNG 文件头 + 足够被前端 <img> 识别的最小数据（后端只校验魔数；不依赖 Pillow）
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\x00IEND\xaeB`\x82"
)

ENABLED = ["read_file", "write_file", "list_dir", "get_time"]


def _config(tmp_path: Path) -> AppConfig:
    return build_config(
        {
            "default_model": "fake",
            "models": {
                "fake": {
                    "base_url": "http://127.0.0.1:8128/v1",
                    "model": "fake-model",
                    "api_key_env": "FAKE_API_KEY",
                },
                "vision": {
                    "base_url": "http://127.0.0.1:8128/v1",
                    "model": "qwen-vl-plus",
                    "api_key_env": "FAKE_VISION_KEY",
                    "supports_vision": True,
                },
            },
            "agent": {"workspace": str(tmp_path / "ws")},
            "tools": {"enabled": ENABLED},
            "storage": {"db_path": str(tmp_path / "sessions.db")},
        }
    )


@pytest.fixture()
def api(tmp_path: Path):
    (tmp_path / "ws").mkdir()
    config = _config(tmp_path)
    service = AgentService(config)
    app.state.service = service
    try:
        with TestClient(app) as client:
            yield client, service
    finally:
        app.state.service = None


class FakeVisionProvider:
    """只验证请求形状，不联网。"""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.closed = False

    def chat_stream(self, messages, tools=None, **kwargs: Any):
        self.requests.append([dict(message) for message in messages])
        yield {"type": "text", "text": "已看到图片"}
        yield {
            "type": "done",
            "content": "已看到图片",
            "reasoning": None,
            "tool_calls": (),
            "usage": Usage(prompt_tokens=12, completion_tokens=4, total_tokens=16),
        }

    def chat(self, messages, tools=None, **kwargs: Any):
        raise AssertionError("本用例不应走非流式路径")

    def close(self) -> None:
        self.closed = True


def _upload(client: TestClient) -> dict:
    response = client.post(
        "/api/uploads",
        files={"file": ("cat.png", PNG_BYTES, "image/png")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _new_session(client: TestClient, model: str) -> str:
    response = client.post("/api/sessions", json={"model": model})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_storage_roundtrips_attachments_without_touching_old_messages(tmp_path: Path) -> None:
    with Storage(tmp_path / "vision.db") as storage:
        session = storage.create_session("fake")
        attachment = {
            "id": "a" * 32 + ".png",
            "url": f"/api/uploads/{'a' * 32}.png",
            "mime": "image/png",
            "name": "cat.png",
            "size": 123,
        }
        storage.append_message(
            session.id,
            {"role": "user", "content": "看图", "attachments": [attachment]},
        )
        storage.append_message(session.id, {"role": "user", "content": "旧消息"})

        messages = storage.get_messages(session.id, with_attachments=True)
        assert messages[0]["attachments"] == [attachment]
        assert "attachments" not in messages[1]

        # 默认回放形状保持旧行为：不带 attachments，CLI/旧测试不受影响
        plain = storage.get_messages(session.id)
        assert "attachments" not in plain[0]
        assert plain[0]["content"] == "看图"


def test_vision_transform_builds_openai_content_array_and_caps_old_images(tmp_path: Path) -> None:
    uploads = UploadStore(tmp_path / "uploads")
    saved = uploads.save(PNG_BYTES, "cat.png", "image/png")
    transform = build_vision_transform(uploads, max_image_turns=1)

    messages = [
        {
            "role": "user",
            "content": "第一张",
            "attachments": [{"id": saved["id"]}],
        },
        {"role": "assistant", "content": "好"},
        {
            "role": "user",
            "content": "第二张",
            "attachments": [{"id": saved["id"]}],
        },
    ]
    transformed = transform(messages)
    assert "attachments" not in transformed[0]
    assert transformed[0]["content"] == "第一张 [图片]"
    content = transformed[2]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "第二张"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_agent_loop_stores_user_attachments_and_transform_receives_them(tmp_path: Path) -> None:
    uploads = UploadStore(tmp_path / "uploads")
    saved = uploads.save(PNG_BYTES, "cat.png", "image/png")
    seen: list[list[dict[str, Any]]] = []

    class Provider:
        def chat_stream(self, messages, tools=None, **kwargs):
            seen.append([dict(message) for message in messages])
            yield {"type": "text", "text": "ok"}
            yield {
                "type": "done",
                "content": "ok",
                "reasoning": None,
                "tool_calls": (),
                "usage": Usage(),
            }

    class Registry:
        def schemas(self):
            return []

    with Storage(tmp_path / "loop.db") as storage:
        session = storage.create_session("vision")
        loop = AgentLoop(
            Provider(),  # type: ignore[arg-type]
            registry=Registry(),  # type: ignore[arg-type]
            storage=storage,
            session_id=session.id,
            history=storage.get_messages(session.id, with_attachments=True),
            request_transform=build_vision_transform(uploads),
        )
        result = loop.run("看图", attachments=[{"id": saved["id"]}])
        assert result.content == "ok"
        messages = storage.get_messages(session.id, with_attachments=True)
        user = next(message for message in messages if message["role"] == "user")
        assert user["attachments"][0]["id"] == saved["id"]

    assert isinstance(seen[0][-1]["content"], list)  # type: ignore[index]


def test_upload_serve_and_validation(api) -> None:
    client, _service = api
    uploaded = _upload(client)
    assert uploaded["mime"] == "image/png"
    assert uploaded["url"].startswith("/api/uploads/")

    image = client.get(uploaded["url"])
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.content == PNG_BYTES

    bad = client.post("/api/uploads", files={"file": ("a.txt", b"not an image", "text/plain")})
    assert bad.status_code == 400

    huge = client.post(
        "/api/uploads",
        files={"file": ("huge.png", PNG_BYTES + b"\x00" * MAX_IMAGE_BYTES, "image/png")},
    )
    assert huge.status_code == 413


def test_models_expose_supports_vision(api) -> None:
    client, _service = api
    payload = client.get("/api/models").json()
    by_name = {item["name"]: item for item in payload}
    assert by_name["vision"]["supports_vision"] is True
    assert by_name["fake"]["supports_vision"] is False


def test_non_vision_model_rejects_image_before_stream(api) -> None:
    client, _service = api
    session = _new_session(client, "fake")
    uploaded = _upload(client)
    response = client.post(
        "/api/chat/stream",
        json={
            "session_id": session,
            "message": "看看这张图",
            "attachments": [{"id": uploaded["id"]}],
        },
    )
    assert response.status_code == 422
    assert "不支持图片" in response.json()["detail"]


def test_vision_chat_sends_content_array_and_history_returns_attachment(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, service = api
    session = _new_session(client, "vision")
    uploaded = _upload(client)
    provider = FakeVisionProvider()
    monkeypatch.setattr(service, "_provider_for", lambda name: provider)

    response = client.post(
        "/api/chat/stream",
        json={
            "session_id": session,
            "message": "看看这张图",
            "attachments": [{"id": uploaded["id"]}],
        },
    )
    assert response.status_code == 200, response.text
    assert '"type": "message_end"' in response.text
    assert provider.requests, "provider 应该收到请求"
    content = provider.requests[0][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看看这张图"}
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    detail = client.get(f"/api/sessions/{session}").json()
    user = next(message for message in detail["messages"] if message["role"] == "user")
    assert user["attachments"][0]["id"] == uploaded["id"]
    assert user["content"] == "看看这张图"


def test_image_only_message_uses_placeholder_text(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, service = api
    session = _new_session(client, "vision")
    uploaded = _upload(client)
    provider = FakeVisionProvider()
    monkeypatch.setattr(service, "_provider_for", lambda name: provider)

    response = client.post(
        "/api/chat/stream",
        json={"session_id": session, "message": "", "attachments": [{"id": uploaded["id"]}]},
    )
    assert response.status_code == 200, response.text
    stored = client.get(f"/api/sessions/{session}").json()["messages"][-2]
    assert stored["role"] == "user"
    assert stored["content"] == "[图片]"
    assert stored["attachments"][0]["id"] == uploaded["id"]

def test_regenerate_keeps_attachments(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, service = api
    session = _new_session(client, "vision")
    uploaded = _upload(client)
    provider = FakeVisionProvider()
    monkeypatch.setattr(service, "_provider_for", lambda name: provider)

    first = client.post(
        "/api/chat/stream",
        json={
            "session_id": session,
            "message": "看看这张图",
            "attachments": [{"id": uploaded["id"]}],
        },
    )
    assert first.status_code == 200, first.text

    regenerated = client.post(f"/api/sessions/{session}/regenerate?model=vision")
    assert regenerated.status_code == 200, regenerated.text
    assert len(provider.requests) >= 2
    assert provider.requests[-1][-1]["role"] == "user"
    assert isinstance(provider.requests[-1][-1]["content"], list)

    detail = client.get(f"/api/sessions/{session}").json()
    user = next(message for message in detail["messages"] if message["role"] == "user")
    assert user["attachments"][0]["id"] == uploaded["id"]

def test_old_messages_table_is_migrated_with_attachments_column(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, role TEXT NOT NULL, content TEXT,
            tool_calls TEXT, tool_call_id TEXT, name TEXT, created_at TEXT NOT NULL,
            UNIQUE (session_id, seq)
        );
        INSERT INTO sessions VALUES ('old-session', '旧会话', 'fake', '2026-01-01', '2026-01-01');
        INSERT INTO messages (session_id, seq, role, content, created_at)
            VALUES ('old-session', 1, 'user', '老消息', '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    with Storage(db) as storage:
        old = storage.get_messages("old-session")
        assert old[0]["content"] == "老消息"
        columns = {row["name"] for row in storage._conn.execute("PRAGMA table_info(messages)")}
        assert "attachments_json" in columns
        storage.append_message(
            "old-session",
            {"role": "user", "content": "新图", "attachments": [{"id": "x"}]},
        )
        assert storage.get_messages("old-session", with_attachments=True)[-1]["attachments"] == [
            {"id": "x"}
        ]

def test_regenerate_with_text_model_rejects_image_session(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, service = api
    session = _new_session(client, "vision")
    uploaded = _upload(client)
    provider = FakeVisionProvider()
    monkeypatch.setattr(service, "_provider_for", lambda name: provider)
    client.post(
        "/api/chat/stream",
        json={
            "session_id": session,
            "message": "看图",
            "attachments": [{"id": uploaded["id"]}],
        },
    )
    response = client.post(f"/api/sessions/{session}/regenerate?model=fake")
    assert response.status_code == 422
    assert "不支持视觉" in response.json()["detail"]

