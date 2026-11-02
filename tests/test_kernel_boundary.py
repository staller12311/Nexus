"""内核边界回归：`agent` 包不得依赖 Web 层。

为什么值得单独钉住：这份内核的价值就在于「能被别的应用当框架用」。一旦有人图省事在
`agent/` 里 import 了 fastapi / starlette，内核就变成「只能被自己的 backend 用」，
垂直应用再想复用它就得连一整个 Web 服务一起拖进来 —— 这条线一旦破，后面很难收回来。

同时跑一遍 `examples/standalone_agent.py`，防止这个模板随着内核演进而失效。
用子进程而不是直接 import：pytest 进程里早就 loaded 了 fastapi（tests 会 import
backend），进程内是测不出「干净启动」的。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_PROBE = """
import sys
from agent.loop import AgentLoop
from agent.registry import ToolRegistry, ToolSpec
from agent.provider import ToolCall, Usage

banned = {"fastapi", "starlette", "uvicorn"}
print(",".join(sorted({name.split(".")[0] for name in sys.modules} & banned)))
"""


def _run(code_or_path: str, *, as_file: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, code_or_path if as_file else "-c"]
    if not as_file:
        cmd.append(code_or_path)
    # 显式指定子进程输出编码：英文 locale 的 Windows runner 默认 cp1252，
    # 脚本里的中文一 print 就 UnicodeEncodeError（CI #11 就是这么挂的）
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        cmd,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )


def test_agent_package_does_not_pull_web_stack() -> None:
    completed = _run(_PROBE)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        "agent 包不应依赖 Web 层，实际拉起了：" + completed.stdout.strip()
    )


def test_standalone_example_still_runs() -> None:
    """examples/standalone_agent.py 是垂直应用的模板，必须能直接跑通。

    断言只看 ASCII 标记：「有没有中文被正确打印」属于脚本自己的事，
    不该让测试依赖控制台 locale。
    """
    completed = _run(str(REPO / "examples" / "standalone_agent.py"), as_file=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "standalone_ok = True" in completed.stdout
