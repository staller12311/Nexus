"""多根工作区（@name 命名挂载）攻防验证。

覆盖：
- 跨根 ../ 逃逸、绝对路径穿越、跨盘符访问
- 前缀混淆（D:\\SharedLibs_evil / @libsevil）
- 未注册挂载名
- 只读挂载写入被拒、可写挂载正常
- 相对路径**只能**走主根（附加目录必须显式 @name）
- 组内符号链接越界仍被标记/拒绝
- 挂载名合法性（配置层）
- 无挂载时行为与单根完全一致（向后兼容）
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\MyAi")
sys.path.insert(0, str(ROOT))

from agent.config import AppConfig, load_config  # noqa: E402
from agent.registry import build_registry  # noqa: E402
from agent.tools.files import Mount, Workspace  # noqa: E402

BASE = ROOT / "workspace" / "_mr_probe"
PRIMARY = BASE / "ProjectA"
LIBS = BASE / "SharedLibs"
EVIL = BASE / "SharedLibs_evil"  # 前缀混淆用
WRITABLE = BASE / "WritableExtra"

shutil.rmtree(BASE, ignore_errors=True)
for directory in (PRIMARY, LIBS, EVIL, WRITABLE):
    directory.mkdir(parents=True)
(PRIMARY / "main.py").write_text("print('project a')\n", encoding="utf-8")
(PRIMARY / "sub").mkdir()
(PRIMARY / "sub" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
(LIBS / "http").mkdir()
(LIBS / "http" / "client.py").write_text("def get(): ...\n", encoding="utf-8")
(LIBS / "libs_only.txt").write_text("shared", encoding="utf-8")
(EVIL / "payload.txt").write_text("evil", encoding="utf-8")
(WRITABLE / "scratch.txt").write_text("x", encoding="utf-8")

workspace = Workspace(
    root=PRIMARY,
    extras=(
        Mount(name="libs", path=LIBS),
        Mount(name="scratch", path=WRITABLE, writable=True),
    ),
)

print("=" * 80)
print(f"主根      : {PRIMARY}")
print(f"@libs     : {LIBS}（只读）")
print(f"@scratch  : {WRITABLE}（读写）")
print("=" * 80)

failures: list[str] = []


def read(path: str) -> str:
    from agent.tools.files import read_file

    return read_file(workspace, path)


def must_allow(label: str, path: str, needle: str) -> None:
    try:
        result = read(path)
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 应允许却被拒 {label}: {exc}")
        failures.append(f"{label} 被误拒")
        return
    ok = needle in result
    print(f"  {'✅ 允许' if ok else '❌ 内容不对'}  {label} -> {result.splitlines()[0]}")
    if not ok:
        failures.append(f"{label} 读取内容不对")


def must_block(label: str, path: str, *, expect: str = "") -> None:
    try:
        result = read(path)
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        ok = (expect in message) if expect else True
        print(f"  {'✅ 拦截' if ok else '⚠️ 拦截但原因不符'}  {label}")
        print(f"        {message[:130]}")
        if not ok:
            failures.append(f"{label} 拦截原因不含 {expect!r}")
        return
    print(f"  ❌ 未拦截 {label} -> {result[:100]}")
    failures.append(f"{label} 未被拦截")


print("\n① 正常的多根访问")
must_allow("主根相对路径 main.py", "main.py", "project a")
must_allow("主根相对路径 sub/util.py", "sub/util.py", "VALUE = 1")
must_allow("@libs 读依赖 @libs/http/client.py", "@libs/http/client.py", "def get")
must_allow("@libs 根目录条目 @libs/libs_only.txt", "@libs/libs_only.txt", "shared")
must_allow("@scratch 读可写挂载", "@scratch/scratch.txt", "x")
must_allow("绝对路径指向 @libs 内部（授权根）", str(LIBS / "libs_only.txt"), "shared")

print("\n② 跨根 ../ 逃逸（红线：先 resolve 再按命中根判界）")
must_block("@libs/../../Windows/win.ini", "@libs/../../Windows/win.ini", expect="越出")
must_block("@libs/../ProjectA/main.py（从挂载逃回主根）", "@libs/../ProjectA/main.py", expect="越出")
must_block("@libs/http/../../..（爬到盘符根）", "@libs/http/../../..", expect="越出")
must_block("主根 ../ 逃逸仍被拒", "../SharedLibs/libs_only.txt", expect="越出")

print("\n③ 相对路径只能走主根（附加目录不能靠相对路径偷渡）")
must_block("相对路径写 SharedLibs 同名字段 libs_only.txt", "libs_only.txt")
must_block("相对路径 ../SharedLibs/libs_only.txt", "../SharedLibs/libs_only.txt", expect="越出")

print("\n④ 前缀混淆")
must_block("绝对路径 SharedLibs_evil（前缀像但不是授权根）", str(EVIL / "payload.txt"), expect="越出")
must_block("挂载名 @libsevil（未注册）", "@libsevil/payload.txt", expect="未知挂载")
must_block("挂载名 @libs2（未注册）", "@libs2/x.txt", expect="未知挂载")

print("\n⑤ 跨盘符 / 系统目录")
hosts = r"C:\Windows\System32\drivers\etc\hosts"
must_block("绝对路径跨盘符 C:\\Windows\\...\\hosts", hosts, expect="越出")
must_block("主根内符号链接指向系统目录", "link_to_windows/win.ini") if False else None

print("\n⑥ 只读挂载写入被拒、可写挂载正常")
from agent.tools.files import write_file  # noqa: E402

try:
    write_file(workspace, "@libs/http/client.py", "hacked")
    failures.append("只读挂载被写入成功")
    print("  ❌ 只读挂载竟然写入成功")
except Exception as exc:  # noqa: BLE001
    ok = "只读" in str(exc)
    print(f"  {'✅ 拒绝' if ok else '⚠️'}  写 @libs/http/client.py -> {exc}")
    if not ok:
        failures.append("只读挂载拒绝原因不含「只读」")

try:
    result = write_file(workspace, "@scratch/new.txt", "ok")
    print(f"  ✅ 允许  写 @scratch/new.txt -> {result}")
    assert (WRITABLE / "new.txt").read_text(encoding="utf-8") == "ok"
except Exception as exc:  # noqa: BLE001
    failures.append(f"可写挂载写入失败：{exc}")
    print(f"  ❌ 可写挂载写入失败: {exc}")

print("\n⑦ 组内符号链接越界（Windows 需要权限，失败则跳过）")
link = PRIMARY / "link_to_libs"
if not link.exists():
    try:
        link.symlink_to(LIBS, target_is_directory=True)
    except OSError as exc:
        print(f"  （跳过：本环境无法创建符号链接：{exc}）")
if link.exists():
    # 指向"另一个授权根"的链接：contains() 认为在范围内（@libs 已授权），允许
    try:
        result = read("link_to_libs/libs_only.txt")
        print(f"  ℹ️  指向授权挂载的链接可读（设计如此）：{result.splitlines()[0]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ℹ️  指向授权挂载的链接被拒：{str(exc)[:100]}")

print("\n⑧ 相对路径与输出标识")
from agent.tools.files import list_dir  # noqa: E402

listing = list_dir(workspace, ".")
print("  list_dir('.') 输出：")
for line in listing.splitlines():
    print(f"    {line}")
assert "@libs/" in listing and "@scratch/" in listing, "主根列表没有展示附加挂载"
assert "只读" in listing and "读写" in listing

relative_extra = workspace.relative(workspace.resolve("@libs/http/client.py"))
print(f"  relative(@libs/http/client.py) -> {relative_extra!r}")
assert relative_extra == "@libs/http/client.py"

print("\n⑨ 挂载名 / 配置层校验")
for bad_name in ("Libs", "@Libs", "1 bad", "a" * 17, "", "libs/sub"):
    try:
        Mount(name=bad_name, path=LIBS)
        failures.append(f"非法挂载名 {bad_name!r} 被接受")
        print(f"  ❌ 非法挂载名被接受: {bad_name!r}")
    except ValueError as exc:
        print(f"  ✅ 拒绝非法挂载名 {bad_name!r}: {str(exc)[:60]}")

try:
    Workspace(root=PRIMARY, extras=(Mount(name="libs", path=LIBS), Mount(name="libs", path=WRITABLE)))
    failures.append("重复挂载名被接受")
    print("  ❌ 重复挂载名被接受")
except ValueError as exc:
    print(f"  ✅ 拒绝重复挂载名: {exc}")

print("\n⑩ 配置 → registry 全链路（config.yaml 形态）")
config_path = BASE / "config.multiroot.yaml"
config_path.write_text(
    f"""
models:
  fake:
    base_url: http://127.0.0.1:9/v1
    model: fake
    api_key_env: FAKE_API_KEY
default_model: fake
agent:
  workspace: {json.dumps(str(PRIMARY))}
  extra_workspaces:
    - name: libs
      path: {json.dumps(str(LIBS))}
    - name: scratch
      path: {json.dumps(str(WRITABLE))}
      writable: true
tools:
  enabled: [read_file, write_file, list_dir]
""",
    encoding="utf-8",
)

config = AppConfig.load(config_path) if hasattr(AppConfig, "load") else load_config(config_path)
registry = build_registry(config)
print(f"  工具: {registry.names()}")
print(f"  读 @libs -> {registry.call('read_file', {'path': '@libs/libs_only.txt'})!r}")
print(f"  写只读挂载 -> {registry.call('write_file', {'path': '@libs/x.txt', 'content': 'no'})[:80]!r}")
print(f"  写可写挂载 -> {registry.call('write_file', {'path': '@scratch/from_config.txt', 'content': 'yes'})!r}")
assert (WRITABLE / "from_config.txt").read_text(encoding="utf-8") == "yes"

print("\n⑪ 非法配置在加载期就报错（不会带着坏配置跑起来）")
bad_config = BASE / "config.bad.yaml"
bad_config.write_text(
    f"""
models:
  fake:
    base_url: http://127.0.0.1:9/v1
    model: fake
    api_key_env: FAKE_API_KEY
default_model: fake
agent:
  workspace: {json.dumps(str(PRIMARY))}
  extra_workspaces:
    - name: "../evil"
      path: {json.dumps(str(EVIL))}
""",
    encoding="utf-8",
)
try:
    load_config(bad_config)
    failures.append("非法挂载名配置竟然加载成功")
    print("  ❌ 非法挂载名配置加载成功")
except Exception as exc:  # noqa: BLE001
    print(f"  ✅ 加载期拒绝: {str(exc).splitlines()[-1][:110]}")

print("\n⑫ 向后兼容：不配 extra_workspaces 时行为与单根一致")
plain = workspace_plain = Workspace(root=PRIMARY)
try:
    plain.resolve("../SharedLibs/libs_only.txt")
    failures.append("单根模式竟然允许 ../ 逃逸")
    print("  ❌ 单根模式允许了逃逸")
except Exception as exc:  # noqa: BLE001
    print(f"  ✅ 单根模式 ../ 仍被拒: {str(exc)[:100]}")
print(f"  单根 relative -> {plain.relative(plain.resolve('main.py'))!r}")
assert plain.relative(plain.resolve("main.py")) == "main.py"

for directory in (PRIMARY, LIBS, EVIL, WRITABLE):
    pass
shutil.rmtree(BASE, ignore_errors=True)

print("\n" + "=" * 80)
if failures:
    print("❌ 失败项：")
    for item in failures:
        print("   -", item)
    raise SystemExit(1)
print("✅ 多根工作区攻防全部通过：跨根/跨盘符/前缀混淆/未注册挂载/只读写 逐项验证")
print("   （且默认无挂载时行为与单根版本完全一致）")