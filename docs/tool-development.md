# 扩展开发：新增工具

> 返回 [README](../README.md)

## 最新示例：`search_code`（代码搜索）

`search_code` 是 v1.1.0 新增的只读工具：在 workspace 内按关键字搜索，返回
`相对路径:行号: 命中内容`，支持 `file_extension` 过滤。它与 `get_time` 这种无副作用
最小模板不同，额外演示了 **只读遍历 + 安全跳过目录 + 输出上限**。

### 第 1 步：实现（`agent/tools/search.py`）

```python
def search_code(workspace: Workspace, keyword: str, file_extension: str = "") -> str:
    text = (keyword or "").strip()
    if not text:
        raise ToolError("keyword 不能为空")
    extension = _normalize_extension(file_extension)
    hits: list[str] = []
    for current_dir, dirs, files in os.walk(workspace.root, followlinks=False):
        dirs[:] = [name for name in dirs if name.lower() not in SKIP_DIRS]
        for filename in files:
            path = Path(current_dir) / filename
            if extension and path.suffix.lower() != f".{extension}":
                continue
            if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
                continue
            if _is_binary(path):
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if text.casefold() in line.casefold():
                    hits.append(f"{path.relative_to(workspace.root).as_posix()}:{line_number}: {line.strip()}")
                    if len(hits) >= MAX_RESULTS:
                        return "\n".join(hits)
    return "未找到匹配内容。" if not hits else "\n".join(hits)
```

关键约束：

- 只遍历主 workspace，**不跟随符号链接**，跳过 `.git` / `.venv` / `node_modules` / `.agent_tmp`；
- 二进制文件（含 `\x00`）跳过，单文件 >1MB 跳过，最多 200 条；
- `file_extension` 支持 `py` / `.py`，省略表示全部文本文件；
- 只读，不需要审批。

### 第 2 步：导出（`agent/tools/__init__.py`）

```python
from .search import search_code
__all__ = [..., "search_code"]
```

### 第 3 步：在 registry 注册（`agent/registry.py`）

```python
"search_code": lambda: ToolSpec(
    name="search_code",
    description="在 workspace 内按行搜索关键字，返回「相对路径:行号: 命中内容」。",
    parameters=_object_schema(
        {
            "keyword": {"type": "string", "description": "要搜索的关键字（1–200 字符）"},
            "file_extension": {"type": "string", "description": "可选扩展名，例如 py、.ts"},
        },
        ["keyword"],
    ),
    handler=partial(search_code, workspace),
),
```

### 第 4 步：加入白名单（`agent/config.py`）

```python
KNOWN_TOOLS = (
    "read_file", "write_file", "create_dir", "delete_path", "move_path",
    "list_dir", "run_shell", "run_python_code", "http_get", "get_time",
    "search_code", "git_status",
)
```

### 第 5 步：启用并验证

```yaml
tools:
  enabled: [read_file, write_file, create_dir, delete_path, move_path,
            list_dir, get_time, search_code, git_status]
```

```console
$ python -m agent tools list
$ python -m agent run "在 workspace 里搜索 TODO"
```

测试见 `tests/test_tools_search.py`：大小写、扩展名过滤、跳过 `.git` 与二进制、无命中、结果上限、
以及 `git_status` 在真实仓库中的输出。

---

## 如何新增一个工具（最小模板 `get_time`）

### 第 1 步：写工具函数（`agent/tools/time.py`）

```python
# agent/tools/time.py —— 完整代码
from datetime import datetime, timezone

from . import ToolError          # 可预期的失败：抛它，registry 会转成给模型看的文本


def get_time(zone: str = "UTC") -> str:
    """返回当前时间；zone 只支持 UTC 或 LOCAL。"""
    key = (zone or "UTC").strip().upper()
    now = datetime.now(timezone.utc)
    if key in ("UTC", "Z", "GMT"):
        return f"当前时间（UTC）：{now.isoformat(timespec='seconds')}"
    if key in ("LOCAL", "本地"):
        return f"当前时间（本地）：{now.astimezone().isoformat(timespec='seconds')}"
    raise ToolError(f"zone 只支持 UTC / LOCAL，收到 {zone!r}")
```

约定：**返回值必须是字符串**（要返回 dict/list 会被自动 `json.dumps`）；参数非法、资源不存在这类
「可预期失败」抛 `ToolError`，registry 会把它变成 `错误：…` 文本回灌给模型，主循环不会崩。

### 第 2 步：导出（`agent/tools/__init__.py`）

```python
from .time import get_time

__all__ = [..., "get_time"]
```

### 第 3 步：在 registry 注册并给出 JSON Schema（`agent/registry.py`）

```python
# build_registry() 的 builders 字典里加一项
"get_time": lambda: ToolSpec(
    name="get_time",
    description="返回当前时间。示例工具，用于演示如何新增工具，无副作用。",
    parameters=_object_schema(
        {"zone": {"type": "string", "description": "UTC 或 LOCAL；省略则按 UTC 处理"}},
        [],          # 可选参数：required 显式声明为空列表，不要省略这个字段
    ),
    handler=get_time,
),
```

Schema 三件事别省：**每个参数写死 `type` 与 `description`**、**`required` 显式列出**
（哪怕是空的）、`additionalProperties: false`（`_object_schema()` 已带上）。
这样模型不会猜错类型，也不会多传字段。

### 第 4 步：把名字加进白名单（`agent/config.py` 的 `KNOWN_TOOLS`）

```python
KNOWN_TOOLS = ("read_file", "write_file", "list_dir", "run_shell", "http_get", "get_time", "search_code", "git_status")
```

不加进这里，配置里写 `get_time` 会被校验直接拒掉（`存在未知工具名 [...]`）。

### 第 5 步：在 `config.yaml` 里启用

```yaml
tools:
  enabled: [read_file, write_file, list_dir, get_time, search_code, git_status]
```

> 还有一个**容易忘的展示项**：`agent/cli.py` 的 `render_tools_table()` 里有一张
> 「说明」文字表，新增工具要跟着加一行，否则 `tools list` 的说明列是空的。
> （这是当前实现的一个小瑕疵：说明文字没有和 registry 放在一起。已记在设计决策里。）

### 验证（真实输出）

```console
$ python -m agent tools list
     工具           状态                 说明
 ────────────────────────────────────────────────────────────────
     read_file      ✓ 启用               读取 workspace 内文件
     write_file     ✓ 启用               写入 workspace 内文件
     list_dir       ✓ 启用               列出 workspace 内目录
     run_shell      ✗ 未启用  默认禁用   执行命令（白名单：空）
     http_get       ✗ 未启用  默认禁用   GET 白名单域名（空）
     get_time       ✓ 启用               返回当前时间（示例工具）
     search_code    ✓ 启用               在 workspace 内按行搜索代码（只读）
     git_status     ✓ 启用               查看 git status --short（只读）

$ python -m agent run 现在几点了
🔧 调用 get_time({"zone": "UTC"})
   ↳ 当前时间（UTC）：2026-10-27T03:38:51+00:00
我查到了：当前时间（UTC）：2026-10-27T03:38:51+00:00
   tokens: prompt=20 completion=10 total=30  用时 0.1s
```

### 这个示例工具保留还是删除？

**保留**：代码留在 `agent/tools/time.py`，并且**默认启用**（`config.example.yaml` 与
`ToolsConfig` 的默认值里都有 `get_time`）—— 它无副作用、不需要配置，正好当模板。

不想看到它就改一行：把 `get_time` 从 `config.yaml` 的 `tools.enabled` 里删掉即可，
代码留着不会被加载、也不会出现在工具列表里。

---

## 当前可用工具（v1.1.0）

| 工具 | 默认 | 审批 | 说明 |
|---|---|---|---|
| `read_file` / `write_file` / `list_dir` | 启用 | 否 | workspace 内文件读写/列目录 |
| `create_dir` / `delete_path` / `move_path` | 启用 | 删除/移动按配置 | 文件 CRUD |
| `search_code` | 启用 | 否 | 只读代码搜索，支持扩展名过滤 |
| `git_status` | 启用 | 否 | 只读 `git status --short` |
| `get_time` | 启用 | 否 | 时间示例工具 |
| `run_python_code` | 默认关闭 | 是 | 进程级轻量沙箱，**非 OS 隔离** |
| `run_shell` | 默认关闭 | 是 | 默认白名单裸命令；`allow_unsafe_shell=true` 时 `shell=True` 且强制审批 |
| `http_get` | 默认关闭 | 否 | 域名白名单 GET |
