"""Web 后端：``/api/tools`` 的清单必须与 registry 实际注册的工具一致。

回归背景：``search_knowledge_base`` / ``web_search`` 由 ``AgentService._build_registry()``
动态注入，不在 ``agent.config.KNOWN_TOOLS`` 里；早先 ``list_tools()`` 只遍历
``KNOWN_TOOLS``，导致这两个确实生效的工具在接口里「消失」。
"""

from __future__ import annotations

from pathlib import Path

from agent.config import KNOWN_TOOLS, build_config
from backend.service.agent_service import AgentService

ENABLED = ["read_file", "write_file", "list_dir", "get_time"]


def _make_service(tmp_path: Path) -> AgentService:
    config = build_config(
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
    return AgentService(config)


def test_list_tools_includes_dynamically_injected_tools(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    tools = {tool.name: tool for tool in service.list_tools()}

    for name in ("search_knowledge_base", "web_search"):
        assert name in tools, f"{name} 已注册进 registry，但接口里没有"
        assert tools[name].enabled is True  # 注入工具注册即生效，不受 tools.enabled 控制
        assert tools[name].description, f"{name} 缺少 description"

    # 内置工具仍按 config.yaml 的 tools.enabled 如实标记
    assert tools["read_file"].enabled is True
    assert tools["run_shell"].enabled is False


def test_list_tools_matches_registry(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    listed = {tool.name for tool in service.list_tools()}
    # 清单 = 全部内置工具（含未启用，用 enabled 标记）+ registry 里真正注册的工具
    assert listed == set(KNOWN_TOOLS) | set(service.registry.names())