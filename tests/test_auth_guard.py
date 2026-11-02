"""全局访问控制的两层语义测试。

设计意图（也是这份测试存在的理由）：

- **配了 `server.auth_token`** → 所有 /api 请求必须带 `Authorization: Bearer <token>`；
- **没配** → 只接受回环地址，「忘记配置」不会变成「对外开放」。

两个容易做错的地方，正是这里重点钉住的：

1. Docker 部署时后端看到的是 nginx 容器的内网 IP，只认回环会把正常访问也挡掉 ——
   所以要采信 `X-Real-IP`，但只在**对端本身是回环/内网**时采信。
2. 如果后端被直接暴露在公网，任何人都能伪造 `X-Real-IP: 127.0.0.1` 绕过限制 ——
   所以对端是公网地址时必须忽略这个头。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.config import build_config
from backend.main import app
from backend.service.agent_service import AgentService

TOKEN = "test-token-0123456789abcdef"  # 需 ≥16 字符，否则 ServerConfig 会拒绝

ENABLED = ["read_file", "list_dir", "get_time"]


def _config(tmp_path: Path, token: str = ""):
    payload: dict = {
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
    if token:
        payload["server"] = {"auth_token": token}
    return build_config(payload)


def _client(tmp_path: Path, *, token: str = "", peer=("testclient", 50000), headers=None):
    """构造一个带独立 service 的 TestClient；peer 决定后端看到的客户端地址。"""
    (tmp_path / "ws").mkdir(exist_ok=True)
    service = AgentService(_config(tmp_path, token))
    app.state.service = service
    return TestClient(app, client=peer, headers=headers or {})


@pytest.fixture(autouse=True)
def _reset_state():
    yield
    app.state.service = None


# ---------------------------------------------------------------- 无令牌：仅回环
def test_no_token_allows_loopback_testclient(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/api/models").status_code == 200


def test_no_token_rejects_public_peer(tmp_path: Path) -> None:
    with _client(tmp_path, peer=("8.8.8.8", 50000)) as client:
        response = client.get("/api/models")

    assert response.status_code == 403
    assert "auth_token" in response.json()["detail"]


def test_no_token_allows_private_peer_behind_nginx(tmp_path: Path) -> None:
    """Docker 形态：对端是 nginx 内网 IP，真实来源写在 X-Real-IP（本机）。"""
    with _client(
        tmp_path,
        peer=("172.18.0.3", 50000),
        headers={"X-Real-IP": "127.0.0.1"},
    ) as client:
        assert client.get("/api/models").status_code == 200


def test_no_token_rejects_spoofed_forward_header_from_public_peer(tmp_path: Path) -> None:
    """防伪造：公网直连时声称自己是 127.0.0.1 不作数。"""
    with _client(
        tmp_path,
        peer=("8.8.8.8", 50000),
        headers={"X-Real-IP": "127.0.0.1"},
    ) as client:
        assert client.get("/api/models").status_code == 403


# ---------------------------------------------------------------- 有令牌：必须带 Bearer
def test_token_required_when_configured(tmp_path: Path) -> None:
    with _client(tmp_path, token=TOKEN) as client:
        response = client.get("/api/models")

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_token_accepts_correct_bearer(tmp_path: Path) -> None:
    with _client(tmp_path, token=TOKEN, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
        assert client.get("/api/models").status_code == 200


def test_token_rejects_wrong_bearer(tmp_path: Path) -> None:
    with _client(tmp_path, token=TOKEN, headers={"Authorization": "Bearer nope"}) as client:
        assert client.get("/api/models").status_code == 401


def test_token_rejects_non_bearer_scheme(tmp_path: Path) -> None:
    with _client(tmp_path, token=TOKEN, headers={"Authorization": f"Basic {TOKEN}"}) as client:
        assert client.get("/api/models").status_code == 401


def test_token_still_allows_health_without_header(tmp_path: Path) -> None:
    """健康检查必须免鉴权：Docker healthcheck 在容器内直接打这个地址。"""
    with _client(tmp_path, token=TOKEN) as client:
        assert client.get("/api/health").status_code == 200


def test_short_token_is_rejected_by_config(tmp_path: Path) -> None:
    """过短的令牌等于没设，配置期就报错而不是运行时才发现。"""
    with pytest.raises(Exception) as excinfo:
        _config(tmp_path, "short")
    assert "auth_token" in str(excinfo.value)
