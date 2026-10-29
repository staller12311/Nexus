"""人工审批网关（Human-in-the-loop）。

设计要点：
- Agent 循环跑在 uvicorn 的工作线程里（`asyncio.to_thread`），所以「挂起」用
  **线程安全的同步原语**实现：`ApprovalGate.wait()` 阻塞工作线程，
  `POST /api/chat/approve` 从另一个线程 `resolve()` 唤醒它。
- 一次工具调用对应一个 gate；gate 是一次性的（重复决策返回 `False`）。
- 超时 / 客户端断开 / 取消都走「拒绝」路径：工具不执行，但 tool 结果照常回填，
  历史永远是合法序列（不会留悬空 tool_call）。
- 进程级单例挂在 `AgentService.approvals` 上（本地单用户场景足够）。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

#: 审批默认等待上限（秒）：超时按拒绝处理
DEFAULT_APPROVAL_TIMEOUT = 300.0


class ApprovalError(Exception):
    """审批相关的业务错误基类。"""


class ApprovalNotFound(ApprovalError):
    """审批不存在 / 已过期（超时、流已结束、进程重启）。"""


class ApprovalAlreadyDecided(ApprovalError):
    """同一个审批被重复决策（重复点击、并发请求）。"""


class ApprovalGate:
    """单个审批的「挂起/唤醒」原语。线程安全，一次性。"""

    def __init__(self, approval_id: str, timeout: float) -> None:
        self.approval_id = approval_id
        self.timeout = max(0.1, float(timeout))
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._decision: bool | None = None
        self._timed_out = False
        self._resolved_at: float | None = None

    def wait(self) -> bool:
        """阻塞直到被 resolve 或超时；超时按拒绝返回 False。"""
        self._event.wait(self.timeout)
        with self._lock:
            if self._decision is None:
                self._decision = False
                self._timed_out = True
            return bool(self._decision)

    def resolve(self, approved: bool) -> bool:
        """写入决策并唤醒等待线程；已决策过返回 False（幂等保护）。"""
        with self._lock:
            if self._decision is not None:
                return False
            self._decision = bool(approved)
            self._resolved_at = time.monotonic()
        self._event.set()
        return True

    def cancel(self) -> None:
        """取消（按拒绝处理）：用于客户端断开 / 服务收尾。"""
        self.resolve(False)

    @property
    def decided(self) -> bool:
        with self._lock:
            return self._decision is not None

    @property
    def timed_out(self) -> bool:
        with self._lock:
            return self._timed_out

    @property
    def resolved_at(self) -> float | None:
        with self._lock:
            return self._resolved_at


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    tool_call_id: str
    name: str
    created_at: float
    gate: ApprovalGate = field(repr=False)


class ApprovalRegistry:
    """进程内未决审批的登记表（供 approve 接口查找）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}

    def open(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        name: str,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT,
    ) -> PendingApproval:
        approval_id = f"ap_{uuid.uuid4().hex[:10]}"
        pending = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            name=name,
            created_at=time.monotonic(),
            gate=ApprovalGate(approval_id, timeout),
        )
        with self._lock:
            self._pending[approval_id] = pending
        return pending

    def get(self, approval_id: str) -> PendingApproval | None:
        with self._lock:
            return self._pending.get(approval_id)

    def resolve(self, approval_id: str, approved: bool) -> PendingApproval:
        """接受人工决策；未知 → ApprovalNotFound，已决策 → ApprovalAlreadyDecided。"""
        pending = self.get(approval_id)
        if pending is None:
            raise ApprovalNotFound(f"审批不存在或已过期：{approval_id}")
        if not pending.gate.resolve(approved):
            raise ApprovalAlreadyDecided(f"审批已处理过：{approval_id}")
        return pending

    def discard(self, approval_id: str) -> None:
        """审批结束（无论结果）后从登记表移除。"""
        with self._lock:
            self._pending.pop(approval_id, None)

    def cancel_session(self, session_id: str) -> int:
        """取消某个会话下所有未决审批（客户端断开 / 流结束收尾）。"""
        with self._lock:
            pending = [item for item in self._pending.values() if item.session_id == session_id]
        for item in pending:
            item.gate.cancel()
        return len(pending)

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)