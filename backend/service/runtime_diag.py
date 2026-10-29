"""运行时诊断：检测进程完整性级别（Windows），用于解释「工作区外写不进去」。

背景（README 第 62 条）：某些安全策略会把 venv 里的 ``python.exe`` 打上
``Mandatory Label\\Low``。低完整性进程能读大部分文件，但**写**会被系统限制在
「同完整性或更低」的目录里 —— 表现就是：工作区内正常、外部工作区一律
``PermissionError: [WinError 5]``。

这不是代码能绕过的（需要管理员改标签，或用未降权的 Python 启动后端），
所以这里只做**检测 + 明确告知**，让用户知道该换系统 Python 或清除只读属性。
"""

from __future__ import annotations

import os
import sys

_SID_LABELS = {
    "S-1-16-4096": "low",
    "S-1-16-8192": "medium",
    "S-1-16-8448": "medium-plus",
    "S-1-16-12288": "high",
    "S-1-16-16384": "system",
}

_CACHE: str | None = None
_CACHED = False


def _via_ctypes() -> str | None:
    """用 Win32 API 读 TokenIntegrityLevel；失败返回 None。"""
    try:
        import ctypes
        from ctypes import wintypes

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", SID_AND_ATTRIBUTES)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        token_query = 0x0008
        token_integrity_level = 9

        handle = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(handle)
        ):
            return None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                handle, token_integrity_level, None, 0, ctypes.byref(needed)
            )
            if not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                handle, token_integrity_level, buffer, needed, ctypes.byref(needed)
            ):
                return None
            label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
            sid_text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(label.Label.Sid, ctypes.byref(sid_text)):
                return None
            try:
                text = sid_text.value or ""
                return _SID_LABELS.get(text, text)
            finally:
                kernel32.LocalFree(sid_text)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 诊断失败不该影响主流程
        return None


def _via_whoami() -> str | None:
    """兜底方案：解析 ``whoami /groups`` 里的 Mandatory Label 行。"""
    try:
        import subprocess

        completed = subprocess.run(
            ["whoami", "/groups"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
        output = completed.stdout or ""
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return None
    for line in output.splitlines():
        if "Mandatory Label" not in line and "强制标签" not in line:
            continue
        for sid, label in _SID_LABELS.items():
            if sid in line:
                return label
        lowered = line.lower()
        for word in ("low", "medium", "high", "system"):
            if word in lowered:
                return word
    return None


def process_integrity_level() -> str | None:
    """返回 ``low`` / ``medium`` / ``high`` / ``system``；非 Windows 或失败返回 None。"""
    global _CACHE, _CACHED
    if _CACHED:
        return _CACHE
    if os.name != "nt":
        _CACHE, _CACHED = None, True
        return None
    _CACHE = _via_ctypes() or _via_whoami()
    _CACHED = True
    return _CACHE


def write_scope_warning() -> str | None:
    """低完整性进程给一句人话警告（否则返回 None）。"""
    level = process_integrity_level()
    if level != "low":
        return None
    return (
        f"当前后端进程处于 Low 完整性级别（python={sys.executable}）：它只能写工作区内的目录，"
        "写外部工作区（例如 D:\\ProjectA）会报 WinError 5。"
        "请改用系统全局 Python 启动后端（不要用被降权的 venv python），"
        "或给该目录关闭只读属性（attrib -R）。"
    )