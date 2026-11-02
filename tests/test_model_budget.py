"""模型输出预算与上下文窗口的回归测试。

对应两个真实问题：

1. **「思考把 max_tokens 吃光、没有正文」**：推理模型的思考与正文共用同一个输出预算，
   而自定义模型以前把它硬编码成 2048，一段长思考就没了。
2. **「填了 1M 的模型却按 128K 算」**：1M 白名单里只有旧模型名，2026-11 之后官方
   只提供 deepseek-flash / deepseek-v4-pro，白名单没跟上就会静默降级到 128K。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import build_config
from backend.service import agent_service as service_module
from backend.service.agent_service import AgentService
from backend.service.custom_models import CustomModelStore
from backend.service.model_runtime import CUSTOM_DEFAULTS, build_custom_provider


def _config(tmp_path: Path):
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
            "tools": {"enabled": ["read_file"]},
            "storage": {"db_path": str(tmp_path / "sessions.db")},
        }
    )


@pytest.fixture()
def store(tmp_path: Path) -> CustomModelStore:
    (tmp_path / "ws").mkdir()
    return CustomModelStore(tmp_path / "sessions.db")


def test_default_output_budget_is_not_tiny(store: CustomModelStore, tmp_path: Path) -> None:
    """默认输出预算必须够一次长思考 + 正文。"""
    assert CUSTOM_DEFAULTS["max_tokens"] >= 8192
    model = store.add("probe", "http://127.0.0.1:8128/v1", "some-model", "sk-test")
    provider = build_custom_provider(_config(tmp_path), model)
    assert provider.model_config.max_tokens == CUSTOM_DEFAULTS["max_tokens"]


def test_explicit_budget_overrides_default(store: CustomModelStore, tmp_path: Path) -> None:
    """用户在模型管理里填了数字就按用户的来（各厂商上限差别很大）。"""
    model = store.add(
        "probe",
        "http://127.0.0.1:8128/v1",
        "some-model",
        "sk-test",
        max_tokens=16384,
        context_window=1_000_000,
    )
    assert model.max_tokens == 16384
    assert model.context_window == 1_000_000

    provider = build_custom_provider(_config(tmp_path), model)
    assert provider.model_config.max_tokens == 16384
    assert provider.model_config.context_window == 1_000_000


def test_legacy_rows_migrate_without_losing_keys(tmp_path: Path) -> None:
    """旧库没有这两列：迁移要能补列，且原有密钥不能丢。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE custom_models (name TEXT PRIMARY KEY, base_url TEXT NOT NULL,"
        " model TEXT NOT NULL, api_key TEXT NOT NULL, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO custom_models VALUES ('old', 'http://127.0.0.1:1/v1', 'm', 'sk-keep',"
        " '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    store = CustomModelStore(db)
    loaded = store.get("old")
    assert loaded is not None
    assert loaded.api_key == "sk-keep"  # ALTER 而不是重建表，密钥还在
    assert loaded.max_tokens is None  # 老数据留空，走默认值


@pytest.mark.parametrize(
    "model_id",
    ["deepseek-flash", "deepseek-v4-pro", "DeepSeek-V4", "qwen3.8-flash"],
)
def test_known_one_million_models_are_recognised(
    model_id: str, store: CustomModelStore, tmp_path: Path
) -> None:
    """现行 1M 模型必须命中白名单，否则会静默按 128K 压缩上下文。"""
    store.add("probe", "http://127.0.0.1:8128/v1", model_id, "sk-test")
    service = AgentService(_config(tmp_path))
    assert service.context_window_for("probe") == 1_000_000


def test_retired_model_names_are_gone_from_whitelist() -> None:
    """deepseek-chat / reasoner 已下线，留在白名单里只会让报错更难懂。"""
    for retired in ("deepseek-chat", "deepseek-reasoner"):
        assert retired not in service_module._ONE_MILLION_PREFIXES


def test_explicit_context_window_beats_name_inference(
    store: CustomModelStore, tmp_path: Path
) -> None:
    """模型名推断不出来时，用户显式填的窗口优先——这就是「换成 1M」的开关。"""
    store.add(
        "probe",
        "http://127.0.0.1:8128/v1",
        "vendor-x-unknown",
        "sk-test",
        context_window=1_000_000,
    )
    service = AgentService(_config(tmp_path))
    assert service.context_window_for("probe") == 1_000_000
