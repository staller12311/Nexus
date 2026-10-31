"""工具注册表：注册、生成 JSON Schema、安全调用。

设计要点（README「设计决策」）：
- **Schema 写死**：每个参数的 ``type`` 与每个工具的 ``required`` 都显式声明，
  ``additionalProperties: false``；不让模型去猜参数类型或漏传必填项；
- **参数校验在本地做**：类型不符、缺必填、多传字段，都在调用前拦下来并返回
  一段模型能看懂的错误文本，而不是把脏参数喂给工具或直接抛异常；
- **可预期失败不炸主循环**：工具抛 ``ToolError`` 时，把它转成 ``错误：...`` 文本
  作为工具结果回灌给模型，让模型自己纠正重试。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Mapping, Sequence

from .config import AppConfig
from .logging_setup import get_logger
from .tools import (
    Mount,
    create_dir,
    delete_path,
    move_path,
    ToolError,
    Workspace,
    get_time,
    git_status,
    http_get,
    list_dir,
    read_file,
    run_python_code,
    run_shell,
    search_code,
    write_file,
)
from .tools.python_exec import TEMP_DIR_NAME

__all__ = ["ToolRegistry", "ToolSpec", "build_registry"]

ERROR_PREFIX = "错误："

#: 支持的参数类型 → 校验函数
_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
}


def _as_text(result: Any) -> str:
    """工具结果一律转成字符串：dict/list 用 JSON，其余用 str()。

    消息规范要求 ``role="tool"`` 的 ``content`` 必须是字符串，
    因此这里统一收口，避免 dict / None 直接进消息体。
    """
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    if isinstance(result, (Mapping, list, tuple)):
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # pragma: no cover - 兜底
            return str(result)
    return str(result)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]

    def schema(self) -> dict[str, Any]:
        """OpenAI function calling 用的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _object_schema(
    properties: Mapping[str, dict[str, Any]], required: Sequence[str]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _make_python_exec_handler(
    workspace: Workspace,
    *,
    default_timeout: float,
    max_output_chars: int,
) -> Callable[..., str]:
    """把配置里的默认超时/截断上限绑进 handler；模型显式给了 ``timeout`` 就按它的来。"""

    def handler(code: str, timeout: float | None = None) -> str:
        return run_python_code(
            workspace,
            code,
            timeout=default_timeout if timeout is None else timeout,
            max_output_chars=max_output_chars,
        )

    return handler


class ToolRegistry:
    """已启用工具的集合。"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._logger = logger or get_logger("agent.registry")

    # ---- 注册 -----------------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 重复注册")
        if spec.parameters.get("type") != "object":
            raise ValueError(f"工具 {spec.name!r} 的 parameters 必须是 object schema")
        if "required" not in spec.parameters:
            raise ValueError(f"工具 {spec.name!r} 的 parameters 必须显式声明 required")
        self._tools[spec.name] = spec

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """给 provider.chat(tools=...) 用的工具定义列表。"""
        return [spec.schema() for spec in self._tools.values()]

    def describe_lines(self) -> list[str]:
        if not self._tools:
            return ["（没有启用任何工具）"]
        lines = []
        for spec in self._tools.values():
            required = spec.parameters.get("required") or []
            lines.append(
                f"{spec.name}({', '.join(required)})  {spec.description}"
                if required
                else f"{spec.name}()  {spec.description}"
            )
        return lines

    # ---- 调用 -----------------------------------------------------------
    def call(self, name: str, arguments: str | Mapping[str, Any] | None = None) -> str:
        """执行工具，永远返回字符串：成功结果是内容，失败结果是给模型看的错误文本。"""
        spec = self._tools.get(name)
        if spec is None:
            return (
                f"{ERROR_PREFIX}未知工具 {name!r}。可用工具：{self.names() or '（无）'}。"
            )

        try:
            parsed = self._parse_arguments(spec, arguments)
            self._validate(spec, parsed)
        except ToolError as exc:
            return f"{ERROR_PREFIX}{exc}"

        try:
            result = spec.handler(**parsed)
        except ToolError as exc:
            self._logger.info("工具 %s 返回可预期失败：%s", name, exc)
            return f"{ERROR_PREFIX}{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具内部 bug 也不该炸掉对话
            self._logger.exception("工具 %s 内部异常", name)
            return (
                f"{ERROR_PREFIX}工具 {name} 内部异常（{type(exc).__name__}: {exc}）。"
                f"这是程序问题，请换一种方式或告知用户，不要重复同样的调用。"
            )
        return _as_text(result)

    # ---- 校验 -----------------------------------------------------------
    @staticmethod
    def _parse_arguments(
        spec: ToolSpec, arguments: str | Mapping[str, Any] | None
    ) -> dict[str, Any]:
        if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
            return {}
        if isinstance(arguments, Mapping):
            return dict(arguments)
        if not isinstance(arguments, str):
            raise ToolError(
                f"参数必须是 JSON 对象或 JSON 字符串，实际是 {type(arguments).__name__}。"
                f"请按下面的 schema 重新给出参数：\n"
                f"{json.dumps(spec.parameters, ensure_ascii=False)}"
            )

        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"参数不是合法 JSON，请重试。解析失败：{exc.msg}"
                f"（第 {exc.lineno} 行第 {exc.colno} 列）。"
                f"收到的原文：{arguments[:300]!r}。"
                f"请只输出一个 JSON 对象，字符串要用双引号，不要带注释或多余文字。"
                f"本工具的 parameters schema："
                f"{json.dumps(spec.parameters, ensure_ascii=False)}"
            ) from exc

        if not isinstance(parsed, dict):
            raise ToolError(
                f"参数必须是 JSON 对象（{{...}}），实际是 {type(parsed).__name__}。"
                f"schema：{json.dumps(spec.parameters, ensure_ascii=False)}"
            )
        return parsed

    @staticmethod
    def _validate(spec: ToolSpec, arguments: dict[str, Any]) -> None:
        schema = spec.parameters
        properties: dict[str, Any] = schema.get("properties", {})
        required: list[str] = list(schema.get("required", []))

        missing = [name for name in required if name not in arguments or arguments[name] is None]
        if missing:
            raise ToolError(
                f"缺少必填参数 {missing}。本工具需要的参数："
                f"{json.dumps(schema, ensure_ascii=False)}"
            )

        if schema.get("additionalProperties") is False:
            unknown = [name for name in arguments if name not in properties]
            if unknown:
                raise ToolError(
                    f"出现了未定义的参数 {unknown}。允许的参数只有 {list(properties)}，"
                    f"请去掉多余字段后重试。"
                )

        for name, value in arguments.items():
            declared = properties.get(name, {}).get("type")
            if value is None or declared is None:
                continue
            check = _TYPE_CHECKS.get(declared)
            if check is None or check(value):
                continue
            raise ToolError(
                f"参数 {name} 的类型应为 {declared}，实际是 {type(value).__name__}"
                f"（值：{value!r}）。请按 schema 重新给出参数："
                f"{json.dumps({name: properties[name]}, ensure_ascii=False)}"
            )


# ---------------------------------------------------------------- 构建
def build_registry(config: AppConfig, logger: logging.Logger | None = None) -> ToolRegistry:
    """按 ``tools.enabled`` 构建注册表，并把配置（workspace、挂载、白名单）绑进处理函数。"""
    registry = ToolRegistry(logger=logger)
    # workspace 用 AppConfig.resolve_path() 解析成绝对路径（基准是配置文件所在目录）
    mounts = tuple(
        Mount(
            name=extra.name,
            path=config.resolve_path(extra.path),
            writable=extra.writable,
        )
        for extra in config.agent.extra_workspaces
    )
    workspace = Workspace(root=config.workspace_path, extras=mounts)
    mounts_text = ""
    if mounts:
        mounts_text = "；附加挂载（用 @名字/路径 访问）：" + "、".join(
            f"@{mount.name} -> {mount.path}（{'读写' if mount.writable else '只读'}）"
            for mount in mounts
        )

    builders: dict[str, Callable[[], ToolSpec]] = {
        "read_file": lambda: ToolSpec(
            name="read_file",
            description=(
                "读取 workspace 内的文本文件（UTF-8）。路径相对于 workspace 根目录，不能越界。"
                f"{mounts_text}"
            ),
            parameters=_object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "相对 workspace 根目录的文件路径（例如 notes/a.txt）；"
                            "附加挂载用 @名字/路径（例如 @libs/http/client.py）",
                    }
                },
                ["path"],
            ),
            handler=partial(read_file, workspace),
        ),
        "write_file": lambda: ToolSpec(
            name="write_file",
            description=(
                "把文本写入 workspace 内的文件（UTF-8，必要时创建父目录），已存在的文件会被覆盖。"
                "生成包含大量引号、特殊字符的代码（如 HTML/JS）时，请确保 JSON 转义正确。"
                "附加挂载默认只读，写入只读挂载会被拒绝。"
                f"{mounts_text}"
            ),
            parameters=_object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "相对 workspace 根目录的文件路径（例如 notes/b.txt）；"
                            "只读挂载不可写",
                    },
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                ["path", "content"],
            ),
            handler=partial(write_file, workspace),
        ),
        "create_dir": lambda: ToolSpec(
            name="create_dir",
            description=(
                "在 workspace 内创建目录（必要时自动创建多级父目录）。"
                "路径相对于 workspace 根目录；已存在时直接返回提示，不报错。"
                f"{mounts_text}"
            ),
            parameters=_object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "相对 workspace 根目录的目录路径，例如 src/components；"
                        "附加挂载用 @名字/路径（只读挂载不可创建）",
                    }
                },
                ["path"],
            ),
            handler=partial(create_dir, workspace),
        ),
        "delete_path": lambda: ToolSpec(
            name="delete_path",
            description=(
                "删除 workspace 内的文件或**空目录**（非空目录会拒绝，需先删里面的内容）。"
                "不能删除工作区/挂载的根目录；只读挂载不可删除。"
                f"{mounts_text}"
            ),
            parameters=_object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "相对 workspace 根目录的路径，例如 tmp/old.txt 或 empty_dir",
                    }
                },
                ["path"],
            ),
            handler=partial(delete_path, workspace),
        ),
        "move_path": lambda: ToolSpec(
            name="move_path",
            description=(
                "在 workspace 内移动或重命名文件/目录（相当于 mv）。目标已存在时拒绝覆盖；"
                "只读挂载不可作为源或目标。"
                f"{mounts_text}"
            ),
            parameters=_object_schema(
                {
                    "src": {
                        "type": "string",
                        "description": "源路径，相对 workspace 根目录，例如 notes/a.txt",
                    },
                    "dest": {
                        "type": "string",
                        "description": "目标路径，相对 workspace 根目录，例如 docs/b.txt",
                    },
                },
                ["src", "dest"],
            ),
            handler=partial(move_path, workspace),
        ),
        "list_dir": lambda: ToolSpec(
            name="list_dir",
            description=(
                "列出 workspace 内某个目录的直接子项。查看 workspace 根目录请传 path=\".\"。"
                "列主根时会显示可用的附加挂载（用 @名字/路径 访问）。"
                f"{mounts_text}"
            ),
            parameters=_object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "相对 workspace 根目录的目录路径，根目录填 \".\"；"
                            "附加挂载用 @名字（例如 @libs）",
                    }
                },
                ["path"],
            ),
            handler=partial(list_dir, workspace),
        ),
        "search_code": lambda: ToolSpec(
            name="search_code",
            description=(
                "在 workspace 内按行搜索关键字，返回「相对路径:行号: 命中内容」。"
                "只读；支持用 file_extension 过滤扩展名（例如 py 或 .py），省略则搜索全部文本文件。"
                "默认大小写不敏感；自动跳过 .git/.venv/node_modules 和二进制文件。"
            ),
            parameters=_object_schema(
                {
                    "keyword": {
                        "type": "string",
                        "description": "要搜索的关键字（1–200 字符，不能包含换行）",
                    },
                    "file_extension": {
                        "type": "string",
                        "description": "可选扩展名过滤，例如 py、.ts；省略表示所有文本文件",
                    },
                },
                ["keyword"],
            ),
            handler=partial(search_code, workspace),
        ),
        "git_status": lambda: ToolSpec(
            name="git_status",
            description=(
                "在 workspace 内执行只读的 `git status --short`，返回有改动的文件列表。"
                "不接受参数；非 Git 仓库或未安装 git 时返回可读错误。"
            ),
            parameters=_object_schema({}, []),
            handler=partial(git_status, workspace),
        ),
        "run_shell": lambda: ToolSpec(
            name="run_shell",
            description=(
                "在 workspace 目录下执行一条白名单内的命令（shell=False，不经过 shell，"
                "不支持管道与重定向）。白名单为空时任何命令都会被拒绝。"
                if not config.tools.allow_unsafe_shell
                else "在 workspace 目录下执行**任意 Shell 命令**：allow_unsafe_shell=true，"
                "使用 shell=True 并绕过白名单；每次调用都强制进入人工审批。"
                "这不是 OS 隔离，审批只代表人工确认，请勿把它当作安全边界。"
            ),
            parameters=_object_schema(
                {
                    "command": {
                        "type": "string",
                        "description": "完整命令行，例如 \"git status --short\"",
                    }
                },
                ["command"],
            ),
            handler=partial(
                run_shell,
                workspace.root,
                allowed_commands=config.tools.run_shell.allowed_commands,
                allow_unsafe_shell=config.tools.allow_unsafe_shell,
            ),
        ),
        "run_python_code": lambda: ToolSpec(
            name="run_python_code",
            description=(
                "在 workspace 内执行一段任意 Python 代码并返回其输出。"
                "**这是进程级轻量沙箱，不是 OS 隔离**：代码以当前后端进程的权限运行，"
                "能读写该权限触达的文件、"
                "访问网络，请只在用户明确要求时使用，并如实说明潜在副作用。"
                f"代码会写到主工作区 {TEMP_DIR_NAME}/ 下的临时文件后执行、用完即删；"
                "子进程工作目录固定为工作区根；"
                f"默认超时 {config.tools.python_exec.timeout:g}s，"
                f"stdout / stderr 各截断到 {config.tools.python_exec.max_output_chars} 字符"
                "（截断处会注明省略量）；只读挂载不能放临时文件。"
                "生成包含大量引号、特殊字符的代码（如 HTML/JS）时，请确保 JSON 转义正确。"
            ),
            parameters=_object_schema(
                {
                    "code": {
                        "type": "string",
                        "description": "要执行的完整 Python 代码（≤20KB）；用 print 输出结果",
                    },
                    "timeout": {
                        "type": "number",
                        "description": (
                            "可选，本次执行的超时秒数；省略则用默认值 "
                            f"{config.tools.python_exec.timeout:g}s"
                        ),
                    },
                },
                ["code"],
            ),
            handler=_make_python_exec_handler(
                workspace,
                default_timeout=config.tools.python_exec.timeout,
                max_output_chars=config.tools.python_exec.max_output_chars,
            ),
        ),
        "http_get": lambda: ToolSpec(
            name="http_get",
            description="GET 一个白名单域名下的 URL，返回状态码与正文（可能被截断）。",
            parameters=_object_schema(
                {
                    "url": {
                        "type": "string",
                        "description": "完整的 http/https URL，例如 https://api.github.com/zen",
                    }
                },
                ["url"],
            ),
            handler=partial(http_get, allowed_domains=config.tools.http_get.allowed_domains),
        ),
        # 示例工具（README「如何新增一个工具」里的模板）：无副作用，不需要配置绑定
        "get_time": lambda: ToolSpec(
            name="get_time",
            description="返回当前时间。示例工具，用于演示如何新增工具，无副作用。",
            parameters=_object_schema(
                {
                    "zone": {
                        "type": "string",
                        "description": "UTC 或 LOCAL；省略则按 UTC 处理",
                    }
                },
                [],  # 可选参数：required 显式声明为空，而不是省略该字段
            ),
            handler=get_time,
        ),
    }

    for name in config.tools.enabled:
        builder = builders.get(name)
        if builder is None:  # pragma: no cover - 配置层已校验过工具名
            raise ValueError(f"没有名为 {name!r} 的工具实现")
        registry.register(builder())

    return registry