"""真实联调：目录选择器接口（/api/fs/dirs、/api/fs/mkdir）与安全边界。"""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(r"D:\MyAi")
PROBE = ROOT / "workspace" / "_fs_probe"


def call(method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=body, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def dirs(path: str = ""):
    query = f"?path={urllib.parse.quote(path)}" if path else ""
    return call("GET", f"/api/fs/dirs{query}")


shutil.rmtree(PROBE, ignore_errors=True)
PROBE.mkdir(parents=True)
(PROBE / "subfolder").mkdir()
(PROBE / "file.txt").write_text("not a dir", encoding="utf-8")

print("=" * 78)
print("① 根列表：只返回盘符")
print("=" * 78)
status, body = dirs()
print(f"  {status} drives={body['drives']} path={body['path']!r}")
assert status == 200 and body["path"] == "" and body["drives"]
assert all(drive.endswith(":\\") for drive in body["drives"])

print("\n② 列 D:\\ —— 只返回子目录（不返回文件）")
print("=" * 78)
status, body = dirs("D:\\")
print(f"  {status} entries={[e['name'] for e in body['entries']][:6]}")
assert status == 200
names = [entry["name"] for entry in body["entries"]]
assert all(Path(entry["path"]).is_dir() for entry in body["entries"])
assert "_fs_probe" not in names  # 探针在 workspace 里，不在 D 根

print("\n③ 进入探针目录：能看到 subfolder，看不到 file.txt")
print("=" * 78)
status, body = dirs(str(PROBE))
names = [entry["name"] for entry in body["entries"]]
print(f"  {status} path={body['path']} entries={names} parent={body['parent']}")
assert status == 200 and "subfolder" in names and "file.txt" not in names

print("\n④ 新建文件夹")
print("=" * 78)
status, body = call("POST", "/api/fs/mkdir", {"parent": str(PROBE), "name": "MyProject"})
print(f"  {status} {body}")
assert status == 200
created = Path(body["path"])
assert created.is_dir() and created.name == "MyProject"

print("\n⑤ 非法/危险请求必须 400")
print("=" * 78)
cases = [
    ("列 C:\\Windows", lambda: dirs(r"C:\Windows")),
    ("列 C:\\Windows\\System32", lambda: dirs(r"C:\Windows\System32")),
    ("列用户主目录整体", lambda: dirs(str(Path.home()))),
    ("列 AppData", lambda: dirs(str(Path.home() / "AppData"))),
    ("列不存在的目录", lambda: dirs(str(PROBE / "nope"))),
    ("列一个文件", lambda: dirs(str(PROBE / "file.txt"))),
    ("UNC 路径", lambda: dirs(r"\\localhost\C$")),
    ("相对路径", lambda: dirs("workspace")),
    ("在 C:\\Windows 新建", lambda: call("POST", "/api/fs/mkdir", {"parent": r"C:\Windows", "name": "x"})),
    ("文件夹名含分隔符", lambda: call("POST", "/api/fs/mkdir", {"parent": str(PROBE), "name": "a/b"})),
    ("文件夹名 ..", lambda: call("POST", "/api/fs/mkdir", {"parent": str(PROBE), "name": ".."})),
    ("重名已存在", lambda: call("POST", "/api/fs/mkdir", {"parent": str(PROBE), "name": "MyProject"})),
]
for label, runner in cases:
    status, body = runner()
    detail = body.get("detail", "")
    print(f"  {status}  {label}: {detail[:92]}")
    assert status == 400, f"{label} 应该是 400"

print("\n⑥ 新建的目录可作为工作区（与第 58 条校验打通）")
print("=" * 78)
status, body = call("POST", "/api/config/workspace", {"path": str(created)})
print(f"  {status} {json.dumps(body, ensure_ascii=False)}")
assert status == 200 and Path(body["workspace"]) == created.resolve()

print("\n⑦ 清理")
print("=" * 78)
status, body = call("DELETE", "/api/config/workspace")
print(f"  恢复默认: {status} {body['workspace']}")
shutil.rmtree(PROBE, ignore_errors=True)
print("\n全部通过 ✅ 目录浏览/新建可用，且系统目录/文件/UNC/非法名一律 400")