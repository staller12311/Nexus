"""Web 后端包：只**调用** ``agent`` 模块，不重写任何智能体逻辑。

把仓库根加进 ``sys.path``，这样 ``uvicorn backend.main:app`` 可以直接 ``import agent.*``，
不需要 ``pip install -e .``（项目里没有打包元数据，为了一次导入凭空加一份不值得）。
用 ``Path(__file__).resolve().parent.parent`` 推仓库根，而不是 ``abspath``。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

__all__ = ["REPO_ROOT"]