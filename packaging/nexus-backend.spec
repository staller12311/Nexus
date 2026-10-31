# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the Nexus local backend.

Build:
    .venv\\Scripts\\pyinstaller.exe -y packaging\\nexus-backend.spec

The resulting folder is copied into
``frontend/src-tauri/resources/backend`` by ``scripts/build-desktop.ps1``.
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
sys.path.insert(0, str(ROOT))

datas = [
    (str(ROOT / "config.example.yaml"), "."),
]

# python-docx 自带模板（templates/default.docx）；不显式收集时，某些
# PyInstaller 版本/环境会漏掉它，导致 Word 解析在桌面版才报错。
datas += collect_data_files("docx")
prompts_dir = ROOT / "prompts"
if prompts_dir.is_dir():
    datas.append((str(prompts_dir), "prompts"))

hiddenimports = [
    # uvicorn 用字符串动态加载这些实现，PyInstaller 静态分析看不到
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]
hiddenimports += collect_submodules("backend")
hiddenimports += collect_submodules("agent")

# 知识库可选依赖：import 名分别是 docx / pypdf；lxml 是 python-docx 的
# C 扩展依赖，必须显式列上，避免桌面版静态分析漏包。
hiddenimports += [
    "docx",
    "pypdf",
    "lxml",
    "lxml.etree",
    "lxml._elementpath",
]

a = Analysis(
    [str(ROOT / "packaging" / "nexus_backend.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pytest_asyncio", "tests", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="nexus-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="nexus-backend",
)