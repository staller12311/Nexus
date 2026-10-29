"""pytest 全局配置：统一隔离测试进程的环境变量。

为什么需要：仓库根存在真实 `.env` 时，某些用例（如 CLI 的 `main()`）会把 `.env` 载入
`os.environ`，而 `load_dotenv(override=False)` 按设计不覆盖既有变量 —— 于是「读 .env」的用例
在别的用例之后跑就会读到别人的值。逐个用例补 `monkeypatch.delenv` 不是长期办法，
这里用一个 autouse fixture 在每个用例开始前统一清掉密钥类变量。
"""

from __future__ import annotations

import os

import pytest

#: 认为「可能装密钥」的环境变量后缀（大小写敏感，与项目里的命名习惯一致）
SECRET_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_ACCESS_KEY")

#: 少数不带上述后缀但同样敏感的变量
SECRET_NAMES = frozenset({"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"})


def is_secret_name(name: str) -> bool:
    return name in SECRET_NAMES or name.upper().endswith(SECRET_SUFFIXES)


@pytest.fixture(autouse=True)
def isolate_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例开始前，把进程里已有的密钥类环境变量删掉（用例结束后 pytest 自动还原）。

    用例若需要某个密钥，自己用 ``monkeypatch.setenv`` 设即可（这发生在本 fixture 之后）。
    """
    for name in list(os.environ):
        if is_secret_name(name):
            monkeypatch.delenv(name, raising=False)