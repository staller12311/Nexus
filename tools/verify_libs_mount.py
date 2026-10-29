import json, sys, urllib.request, urllib.parse
sys.path.insert(0, r"D:\MyAi")
BASE = "http://127.0.0.1:8000"

def call(method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return r.status, (json.loads(raw.decode()) if raw else None)

print("=" * 74)
print("① 目录选择器能看到 D:\\SharedLibs（workspace 之外、可读）")
print("=" * 74)
status, body = call("GET", "/api/fs/dirs?path=" + urllib.parse.quote("D:\\"))
names = [e["name"] for e in body["entries"]]
print(f"  {status} D:\\ 下包含 SharedLibs: {'SharedLibs' in names}")
assert status == 200 and "SharedLibs" in names

print("\n② 挂载为只读 @libs")
print("=" * 74)
status, info = call("POST", "/api/config/workspace", {
    "path": r"D:\MyAi\workspace",
    "extras": [{"name": "libs", "path": r"D:\SharedLibs", "writable": False}],
})
print(f"  {status} {json.dumps(info, ensure_ascii=False)}")
assert status == 200 and info["extras"][0]["path"] == r"D:\SharedLibs"

print("\n③ 工具层：读 @libs/_probe_lib.py（跨出 workspace，但已授权）")
print("=" * 74)
from agent.config import load_config
from backend.service.agent_service import AgentService
service = AgentService(load_config(r"D:\MyAi\config.yaml"))
print(f"  服务工作区: {service.workspace}  挂载: {[m.name for m in service.extras]}")
read = service.registry.call("read_file", {"path": "@libs/_probe_lib.py"})
print("  read_file:", read.splitlines()[0], "| 含 SHARED_VALUE:", "SHARED_VALUE = 4242" in read)
assert "SHARED_VALUE = 4242" in read

print("\n④ 只读挂载写入被拒")
print("=" * 74)
write = service.registry.call("write_file", {"path": "@libs/hacked.py", "content": "x"})
print("  write_file:", write[:100])
assert write.startswith("错误：") and "只读" in write

print("\n⑤ 未授权目录仍然被拒（不是变相开放 D 盘）")
print("=" * 74)
for p in (r"D:\AI\Trae CN", r"D:\SharedLibs_evil\x.txt", r"C:\Windows\win.ini"):
    result = service.registry.call("read_file", {"path": p})
    print(f"  {p} -> {result[:70]}")
    assert result.startswith("错误：")

print("\n⑥ 清理：恢复默认工作区")
print("=" * 74)
status, info = call("DELETE", "/api/config/workspace")
print(f"  {status} {json.dumps(info, ensure_ascii=False)}")
print("\n全部通过 ✅ D:\\SharedLibs 可被选择、可作为只读挂载、越界仍被拦")
