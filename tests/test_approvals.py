"""人工审批网关（ApprovalGate / ApprovalRegistry）的单元测试。"""

from __future__ import annotations

import threading
import time

import pytest

from backend.service.approvals import (
    ApprovalAlreadyDecided,
    ApprovalGate,
    ApprovalNotFound,
    ApprovalRegistry,
)


def test_gate_resolve_wakes_waiter() -> None:
    gate = ApprovalGate("ap_test", timeout=5)
    result: dict[str, bool] = {}

    def waiter() -> None:
        result["approved"] = gate.wait()

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    assert gate.resolve(True) is True
    thread.join(timeout=2)
    assert result == {"approved": True}
    assert gate.decided is True
    assert gate.timed_out is False


def test_gate_timeout_means_reject() -> None:
    gate = ApprovalGate("ap_slow", timeout=0.1)
    started = time.monotonic()
    assert gate.wait() is False
    # 计时器粒度可能导致略小于 0.1s；只需确认确实等过（而不是立刻返回）
    assert time.monotonic() - started >= 0.05
    assert gate.timed_out is True
    # 超时后再来的 approve 也不能改结果（一次性）
    assert gate.resolve(True) is False


def test_gate_cancel_means_reject() -> None:
    gate = ApprovalGate("ap_cancel", timeout=5)
    gate.cancel()
    assert gate.wait() is False
    assert gate.timed_out is False  # 是取消，不是超时


def test_gate_resolve_is_idempotent() -> None:
    gate = ApprovalGate("ap_twice", timeout=5)
    assert gate.resolve(False) is True
    assert gate.resolve(True) is False
    assert gate.wait() is False


def test_registry_open_resolve_discard() -> None:
    registry = ApprovalRegistry()
    pending = registry.open(
        session_id="s1",
        tool_call_id="call_1",
        name="write_file",
        timeout=5,
    )
    assert pending.approval_id.startswith("ap_")
    assert len(registry) == 1

    resolved = registry.resolve(pending.approval_id, True)
    assert resolved.approval_id == pending.approval_id
    assert pending.gate.wait() is True

    # 重复决策 → 409 语义
    with pytest.raises(ApprovalAlreadyDecided):
        registry.resolve(pending.approval_id, False)

    registry.discard(pending.approval_id)
    assert len(registry) == 0
    # 结束后再点 → 404 语义
    with pytest.raises(ApprovalNotFound):
        registry.resolve(pending.approval_id, True)


def test_registry_unknown_id_raises_not_found() -> None:
    with pytest.raises(ApprovalNotFound):
        ApprovalRegistry().resolve("ap_missing", True)


def test_registry_cancel_session_wakes_all_gates() -> None:
    registry = ApprovalRegistry()
    first = registry.open(session_id="s1", tool_call_id="c1", name="write_file", timeout=5)
    second = registry.open(session_id="s1", tool_call_id="c2", name="delete_path", timeout=5)
    other = registry.open(session_id="s2", tool_call_id="c3", name="write_file", timeout=5)

    assert registry.cancel_session("s1") == 2
    assert first.gate.wait() is False
    assert second.gate.wait() is False
    assert other.gate.decided is False  # 别的会话不受影响

    registry.discard(first.approval_id)
    registry.discard(second.approval_id)
    registry.discard(other.approval_id)