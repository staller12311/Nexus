"""示例工具：``get_time``（返回当前时间）。

这是一个**最小可照抄的模板**：只依赖标准库、没有副作用（不碰文件/网络/命令），
用来演示「新增一个工具」的完整步骤（见 README「如何新增一个工具」）。
若不需要，从 ``config.yaml`` 的 ``tools.enabled`` 里删掉 ``get_time`` 即可，代码留着也无害。
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import ToolError

SUPPORTED_ZONES = ("UTC", "LOCAL")


def get_time(zone: str = "UTC") -> str:
    """返回当前时间；``zone`` 只支持 UTC 或 LOCAL。"""
    key = (zone or "UTC").strip().upper()
    now = datetime.now(timezone.utc)
    if key in ("UTC", "Z", "GMT"):
        return f"当前时间（UTC）：{now.isoformat(timespec='seconds')}"
    if key in ("LOCAL", "本地"):
        return f"当前时间（本地）：{now.astimezone().isoformat(timespec='seconds')}"
    raise ToolError(f"zone 只支持 {' / '.join(SUPPORTED_ZONES)}，收到 {zone!r}")