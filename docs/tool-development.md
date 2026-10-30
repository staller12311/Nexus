# 扩展开发：新增工具

> 返回 [README](../README.md)

## 如何新增一个工具

下面是**真的加进去了**的一个最小工具 `get_time`：只依赖标准库、无副作用（不碰文件、网络、命令），
可以直接当模板照抄。完整步骤一共改 5 个地方，最后跑两条命令验证。

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
KNOWN_TOOLS = ("read_file", "write_file", "list_dir", "run_shell", "http_get", "get_time")
```

不加进这里，配置里写 `get_time` 会被校验直接拒掉（`存在未知工具名 [...]`）。

### 第 5 步：在 `config.yaml` 里启用

```yaml
tools:
  enabled: [read_file, write_file, list_dir, get_time]
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
