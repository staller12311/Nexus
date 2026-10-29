"""阶段 4：工具层安全测试（越界 / 符号链接 / 大小写 / 白名单 / Schema 严格性）。

对应任务验收要求的三类安全用例：
1. ``../`` 穿越；(2) 符号链接指向 workspace 外；(3) workspace 内正常读写。
另加 Windows 特有的大小写不敏感用例，以及 run_shell / http_get 白名单用例。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent.config import build_config, load_config
from agent.registry import ToolRegistry, ToolSpec, build_registry
from agent.tools import ToolError, Workspace, http_get, list_dir, read_file, run_shell, write_file
from agent.tools.files import WorkspaceViolation
from agent.tools.http import _host_allowed

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.yaml"


# ------------------------------------------------------------------ 夹具
@pytest.fixture()
def ws_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("hello 工作区", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("bbb", encoding="utf-8")
    return root


@pytest.fixture()
def workspace(ws_root: Path) -> Workspace:
    return Workspace(root=ws_root)


def make_config(tmp_path: Path, **overrides: Any):
    data: dict[str, Any] = {
        "default_model": "m",
        "models": {
            "m": {
                "base_url": "http://127.0.0.1:9/v1",
                "model": "test-model",
                "api_key_env": "TOOLS_TEST_KEY",
            }
        },
        "agent": {"workspace": str(tmp_path / "workspace")},
    }
    for key, value in overrides.items():
        data[key] = value
    return build_config(data)


# ============================================================ 1. 正常读写
def test_read_file_inside_workspace(workspace: Workspace) -> None:
    content = read_file(workspace, "a.txt")
    assert "hello 工作区" in content
    assert "a.txt" in content


def test_read_file_accepts_nested_and_dot_prefixed_paths(workspace: Workspace) -> None:
    assert "bbb" in read_file(workspace, "sub/b.txt")
    assert "bbb" in read_file(workspace, "./sub/../sub/b.txt")


def test_write_file_inside_workspace(workspace: Workspace, ws_root: Path) -> None:
    message = write_file(workspace, "notes/new.txt", "新内容")
    assert "已写入 notes/new.txt" in message
    assert (ws_root / "notes" / "new.txt").read_text(encoding="utf-8") == "新内容"
    assert "已创建缺失的父目录" in message


def test_write_file_overwrites_existing(workspace: Workspace, ws_root: Path) -> None:
    write_file(workspace, "a.txt", "覆盖后")
    assert (ws_root / "a.txt").read_text(encoding="utf-8") == "覆盖后"


def test_list_dir_inside_workspace(workspace: Workspace) -> None:
    listing = list_dir(workspace, ".")
    assert "sub/" in listing
    assert "a.txt" in listing


def test_read_missing_file_reports_clearly(workspace: Workspace) -> None:
    with pytest.raises(ToolError) as excinfo:
        read_file(workspace, "nope.txt")
    assert "文件不存在" in str(excinfo.value)


def test_read_directory_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(ToolError) as excinfo:
        read_file(workspace, "sub")
    assert "是目录" in str(excinfo.value)


def test_read_file_truncates_large_file(workspace: Workspace, ws_root: Path) -> None:
    (ws_root / "big.txt").write_text("x" * 5000, encoding="utf-8")
    content = read_file(workspace, "big.txt", max_bytes=100)
    assert "已截断" in content
    body = content.split("\n")[-1]
    assert body == "x" * 100


# ============================================================ 2. ../ 穿越
@pytest.mark.parametrize(
    "path",
    ["../outside.txt", "../../outside.txt", "sub/../../outside.txt", "./../outside.txt"],
)
def test_read_file_dotdot_traversal_is_rejected(
    workspace: Workspace, ws_root: Path, path: str
) -> None:
    (ws_root.parent / "outside.txt").write_text("机密", encoding="utf-8")
    with pytest.raises(WorkspaceViolation) as excinfo:
        read_file(workspace, path)
    assert "越出 workspace" in str(excinfo.value)


def test_write_file_dotdot_traversal_is_rejected(workspace: Workspace, ws_root: Path) -> None:
    with pytest.raises(WorkspaceViolation):
        write_file(workspace, "../escaped.txt", "x")
    assert not (ws_root.parent / "escaped.txt").exists(), "越界写入必须没有产生任何文件"


def test_list_dir_dotdot_traversal_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolation):
        list_dir(workspace, "..")


def test_absolute_path_outside_workspace_is_rejected(workspace: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "secret.txt"
    outside.parent.mkdir()
    outside.write_text("机密", encoding="utf-8")
    with pytest.raises(WorkspaceViolation):
        read_file(workspace, str(outside))
    with pytest.raises(WorkspaceViolation):
        write_file(workspace, str(tmp_path / "elsewhere" / "new.txt"), "x")
    with pytest.raises(WorkspaceViolation):
        list_dir(workspace, str(tmp_path))


def test_drive_root_is_rejected(workspace: Workspace) -> None:
    with pytest.raises(WorkspaceViolation):
        read_file(workspace, "C:\\Windows\\win.ini")


def test_workspace_root_itself_is_allowed(workspace: Workspace) -> None:
    assert "a.txt" in list_dir(workspace, ".")


# ---- 与验收标准第 5 条逐字对应的字面路径 ----
@pytest.mark.parametrize("path", ["../../etc/passwd", "..\\..\\etc\\passwd"])
def test_acceptance_read_file_etc_passwd_is_rejected(workspace: Workspace, path: str) -> None:
    """验收原文：read_file("../../etc/passwd") 必须被拒绝。"""
    with pytest.raises(WorkspaceViolation) as excinfo:
        read_file(workspace, path)
    assert "越出 workspace" in str(excinfo.value)


def test_acceptance_write_file_evil_txt_is_rejected(workspace: Workspace, ws_root: Path) -> None:
    """验收原文：write_file("../evil.txt", ...) 必须被拒绝，且不得真的落盘。"""
    with pytest.raises(WorkspaceViolation) as excinfo:
        write_file(workspace, "../evil.txt", "恶意内容")
    assert "越出 workspace" in str(excinfo.value)
    assert not (ws_root.parent / "evil.txt").exists(), "越界写入绝不能产生文件"


# ============================================================ 3. 符号链接
#: 测试中创建的链接，由下面的 autouse fixture 显式清理（全部位于 tmp_path 内）
_CREATED_LINKS: list[Path] = []


def unlink_link(link: Path) -> None:
    """只删除链接本身，绝不递归进目标。

    目录联接必须用 ``os.rmdir``：它移除重解析点自身，不会进目标目录。
    注意 **``os.path.islink()`` 在 Python 3.11 对 junction 返回 False**，
    因此不能靠它判断「这是不是链接」，也不该把清理交给带递归语义的工具。
    """
    try:
        if link.is_symlink():
            link.unlink()  # 真符号链接（POSIX / 开了开发者模式的 Windows）
        elif link.is_dir():
            os.rmdir(link)  # Windows 目录联接
        elif link.exists():
            link.unlink()
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _cleanup_created_links():
    _CREATED_LINKS.clear()
    yield
    while _CREATED_LINKS:
        unlink_link(_CREATED_LINKS.pop())


def _make_symlink(target: Path, link: Path, *, directory: bool) -> str:
    """创建指向 workspace 外的链接。

    优先 ``os.symlink``；Windows 上它需要开发者模式或管理员权限（否则 WinError 1314），
    此时对目录回退到 **junction（目录联接）** —— junction 不校验特权，同样是被
    ``Path.resolve()`` 跟随的重解析点，足以验证越界检查。两者都失败才 skip，并写明原因。
    """
    attempts: list[str] = []
    try:
        os.symlink(target, link, target_is_directory=directory)
        _CREATED_LINKS.append(link)
        return "symlink"
    except (OSError, NotImplementedError) as exc:
        attempts.append(f"os.symlink 失败：{type(exc).__name__}: {exc}")

    if os.name == "nt" and directory:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            shell=False,
        )
        if completed.returncode == 0 and Path(link).exists():
            _CREATED_LINKS.append(link)
            return "junction"
        attempts.append(
            f"mklink /J 失败（退出码 {completed.returncode}）："
            f"{(completed.stdout or '') + (completed.stderr or '')}".strip()
        )

    pytest.skip(
        "当前环境无法创建符号链接/目录联接，故无法验证符号链接越界"
        "（Windows 创建符号链接需要开发者模式或管理员权限）：" + " | ".join(attempts)
    )


def test_symlink_to_directory_outside_is_rejected(workspace: Workspace, ws_root: Path) -> None:
    outside = ws_root.parent / "outside_dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("机密", encoding="utf-8")
    _make_symlink(outside, ws_root / "link", directory=True)

    with pytest.raises(WorkspaceViolation) as excinfo:
        read_file(workspace, "link/secret.txt")
    assert "越出 workspace" in str(excinfo.value)

    with pytest.raises(WorkspaceViolation):
        write_file(workspace, "link/new.txt", "x")
    assert not (outside / "new.txt").exists()

    with pytest.raises(WorkspaceViolation):
        list_dir(workspace, "link")


def test_symlink_to_file_outside_is_rejected(workspace: Workspace, ws_root: Path) -> None:
    outside = ws_root.parent / "outside_file.txt"
    outside.write_text("机密", encoding="utf-8")
    _make_symlink(outside, ws_root / "filelink.txt", directory=False)

    with pytest.raises(WorkspaceViolation):
        read_file(workspace, "filelink.txt")


def test_symlink_inside_workspace_is_allowed(workspace: Workspace, ws_root: Path) -> None:
    _make_symlink(ws_root / "a.txt", ws_root / "goodlink.txt", directory=False)
    assert "hello 工作区" in read_file(workspace, "goodlink.txt")


def test_list_dir_marks_escaping_symlink(workspace: Workspace, ws_root: Path) -> None:
    outside = ws_root.parent / "outside_dir2"
    outside.mkdir()
    _make_symlink(outside, ws_root / "badlink", directory=True)
    listing = list_dir(workspace, ".")
    assert "badlink" in listing
    assert "指向 workspace 外" in listing


def test_link_cleanup_removes_only_the_link(workspace: Workspace, ws_root: Path) -> None:
    """清理必须只摘链接：用 rmdir 而不是 rmtree，目标目录内容必须完好。"""
    outside = ws_root.parent / "keep_me"
    outside.mkdir()
    (outside / "precious.txt").write_text("目标数据", encoding="utf-8")

    link = ws_root / "dirlink"
    _make_symlink(outside, link, directory=True)
    assert link.exists()

    unlink_link(link)
    assert not link.exists(), "链接本身应被移除"
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "目标数据"
    assert outside.is_dir(), "链接目标目录必须完好无损"


# ============================================================ 4. 大小写不敏感
@pytest.mark.skipif(os.name != "nt", reason="大小写不敏感路径是 Windows 特有行为")
def test_case_insensitive_paths_stay_inside_workspace(workspace: Workspace, ws_root: Path) -> None:
    upper_root = str(ws_root).upper()
    assert upper_root != str(ws_root) or True  # 记录：大写形式与原形式应视为同一目录

    assert "hello 工作区" in read_file(workspace, str(Path(upper_root) / "A.TXT"))
    assert "hello 工作区" in read_file(workspace, str(Path(upper_root) / "a.txt"))
    assert "hello 工作区" in read_file(workspace, "A.TXT")
    assert "bbb" in read_file(workspace, str(Path(upper_root) / "SUB" / "B.TXT"))

    # 大小写变体不得被判成越界
    assert workspace.resolve(str(Path(upper_root) / "a.txt")) == ws_root / "a.txt"


@pytest.mark.skipif(os.name != "nt", reason="大小写归一比较是 Windows 特有行为")
def test_is_relative_to_normalizes_case_on_windows(ws_root: Path) -> None:
    upper = Path(str(ws_root).upper())
    assert (upper / "a.txt").resolve().is_relative_to(ws_root)
    assert (ws_root / "a.txt").resolve().is_relative_to(upper)


# ============================================================ 5. run_shell
def test_split_command_keeps_quotes_and_backslashes() -> None:
    from agent.tools.shell import split_command

    assert split_command('git commit -m "a b"') == ["git", "commit", "-m", "a b"]
    assert split_command(r"python C:\tmp\x.py") == ["python", r"C:\tmp\x.py"]
    assert split_command('python -c "print(2+2)"') == ["python", "-c", "print(2+2)"]
    assert split_command("   ") == []
    with pytest.raises(ToolError):
        split_command('git commit -m "unterminated')


def test_run_shell_empty_allowlist_refuses_everything(ws_root: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        run_shell(ws_root, "git status", allowed_commands=[])
    assert "白名单为空" in str(excinfo.value)


def test_run_shell_rejects_command_with_path(ws_root: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        run_shell(ws_root, r"C:\evil\git.exe status", allowed_commands=["git"])
    assert "裸命令名" in str(excinfo.value)


def test_run_shell_rejects_command_outside_allowlist(ws_root: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        run_shell(ws_root, "git status", allowed_commands=["python"])
    assert "不在白名单" in str(excinfo.value)


def test_run_shell_cmd_builtin_is_not_supported(ws_root: Path) -> None:
    """dir/type 是 cmd 内建命令，shell=False 下必然找不到可执行文件。"""
    if shutil.which("dir") is not None:  # pragma: no cover - 环境异常
        pytest.skip("本机 PATH 里居然有 dir 可执行文件，无法验证内建命令不受支持")
    with pytest.raises(ToolError) as excinfo:
        run_shell(ws_root, "dir", allowed_commands=["dir"])
    message = str(excinfo.value)
    assert "找不到可执行文件" in message
    assert "cmd 内建命令" in message


def test_run_shell_runs_allowed_command_in_workspace(ws_root: Path) -> None:
    if shutil.which("python") is None:  # pragma: no cover
        pytest.skip("PATH 中没有 python，无法验证白名单内命令可执行")
    output = run_shell(
        ws_root,
        'python -c "import os;print(os.getcwd())"',
        allowed_commands=["python"],
    )
    assert "退出码：0" in output
    assert str(ws_root) in output


def test_run_shell_reports_nonzero_exit(ws_root: Path) -> None:
    if shutil.which("python") is None:  # pragma: no cover
        pytest.skip("PATH 中没有 python")
    output = run_shell(ws_root, 'python -c "raise SystemExit(3)"', allowed_commands=["python"])
    assert "退出码：3" in output


# ============================================================ 6. http_get
class _HttpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/redirect-away":
            self.send_response(302)
            self.send_header("Location", "http://example.com/evil")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = f"内容来自 {self.path}".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


@pytest.fixture()
def http_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HttpHandler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_http_get_empty_allowlist_refuses(http_server: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        http_get(f"{http_server}/x", allowed_domains=[])
    assert "白名单为空" in str(excinfo.value)


def test_http_get_rejects_non_whitelisted_host(http_server: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        http_get("https://example.com/", allowed_domains=["127.0.0.1"])
    assert "不在白名单" in str(excinfo.value)


def test_http_get_rejects_non_http_scheme() -> None:
    with pytest.raises(ToolError) as excinfo:
        http_get("file:///C:/Windows/win.ini", allowed_domains=["127.0.0.1"])
    assert "只支持 http/https" in str(excinfo.value)


def test_http_get_works_for_whitelisted_host(http_server: str) -> None:
    result = http_get(f"{http_server}/hello", allowed_domains=["127.0.0.1"])
    assert "HTTP 200" in result
    assert "内容来自 /hello" in result


def test_http_get_blocks_redirect_outside_whitelist(http_server: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        http_get(f"{http_server}/redirect-away", allowed_domains=["127.0.0.1"])
    assert "不在白名单" in str(excinfo.value)


def test_host_matching_allows_subdomains_only() -> None:
    assert _host_allowed("api.example.com", ["example.com"]) is True
    assert _host_allowed("example.com", ["example.com"]) is True
    assert _host_allowed("example.com.evil.com", ["example.com"]) is False
    assert _host_allowed("notexample.com", ["example.com"]) is False
    assert _host_allowed(None, ["example.com"]) is False


# ============================================================ 7. registry
def test_default_config_enables_expected_tools() -> None:
    config = load_config(EXAMPLE_CONFIG, load_env_file=False)
    registry = build_registry(config)
    assert registry.names() == ["read_file", "write_file", "list_dir", "get_time"]
    assert "run_shell" not in registry
    assert "http_get" not in registry


def test_schemas_are_strict(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        tools={"enabled": ["read_file", "write_file", "list_dir", "run_shell", "http_get"]},
    )
    registry = build_registry(config)
    schemas = registry.schemas()
    assert len(schemas) == 5

    for schema in schemas:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["description"]
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters["required"], list)
        assert parameters["additionalProperties"] is False
        for name, prop in parameters["properties"].items():
            assert prop["type"] in {"string", "integer", "number", "boolean"}, (
                f"{function['name']}.{name} 缺少明确类型"
            )
            assert prop.get("description"), f"{function['name']}.{name} 缺少描述"

    by_name = {schema["function"]["name"]: schema["function"]["parameters"] for schema in schemas}
    assert by_name["read_file"]["required"] == ["path"]
    assert by_name["write_file"]["required"] == ["path", "content"]
    assert by_name["list_dir"]["required"] == ["path"]
    assert by_name["run_shell"]["required"] == ["command"]
    assert by_name["http_get"]["required"] == ["url"]


def test_registry_call_reads_from_config_workspace(tmp_path: Path) -> None:
    """workspace 必须是 AppConfig.resolve_path() 解析出的绝对路径，而不是当前工作目录。"""
    config = make_config(tmp_path)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "note.txt").write_text("来自配置目录", encoding="utf-8")

    registry = build_registry(config)
    assert registry.call("read_file", {"path": "note.txt"}).endswith("来自配置目录")


def test_registry_call_writes_relative_to_config_workspace(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    registry = build_registry(config)
    registry.call("write_file", {"path": "out/x.txt", "content": "写入成功"})
    assert (tmp_path / "workspace" / "out" / "x.txt").read_text(encoding="utf-8") == "写入成功"


def test_registry_call_unknown_tool(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    result = registry.call("rm_rf", {"path": "/"})
    assert result.startswith("错误：")
    assert "未知工具" in result
    assert "read_file" in result  # 提示可用工具


def test_registry_call_invalid_json_arguments(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    result = registry.call("read_file", '{"path": "a.txt"')
    assert result.startswith("错误：")
    assert "不是合法 JSON" in result
    assert "请重试" in result
    assert "schema" in result


def test_registry_call_missing_required_argument(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    result = registry.call("read_file", {})
    assert result.startswith("错误：")
    assert "缺少必填参数" in result
    assert "path" in result


def test_registry_call_wrong_argument_type(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    result = registry.call("read_file", {"path": 123})
    assert result.startswith("错误：")
    assert "类型应为 string" in result


def test_registry_call_rejects_unknown_argument(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    result = registry.call("read_file", {"path": "a.txt", "encoding": "gbk"})
    assert result.startswith("错误：")
    assert "未定义的参数" in result


def test_registry_call_tool_error_is_returned_as_text(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    result = registry.call("read_file", {"path": "../escape.txt"})
    assert result.startswith("错误：")
    assert "越出 workspace" in result
    assert "Traceback" not in result


def test_registry_call_unknown_tool_from_arguments_string(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    assert "错误：" in registry.call("nope", "{}")


def test_registry_call_wraps_handler_exception(tmp_path: Path) -> None:
    registry = ToolRegistry()

    def boom(**kwargs: Any) -> str:
        raise RuntimeError("内部炸了")

    def expected_failure(**kwargs: Any) -> str:
        raise ToolError("预期的失败")

    schema = {
        "type": "object",
        "properties": {"x": {"type": "string", "description": "占位"}},
        "required": ["x"],
        "additionalProperties": False,
    }
    registry.register(ToolSpec("boom", "会抛异常", schema, boom))
    registry.register(ToolSpec("expected", "会抛 ToolError", schema, expected_failure))

    crashed = registry.call("boom", {"x": "1"})
    assert crashed.startswith("错误：")
    assert "内部异常" in crashed

    failed = registry.call("expected", {"x": "1"})
    assert failed == "错误：预期的失败"


def test_registry_rejects_duplicate_registration() -> None:
    registry = ToolRegistry()
    schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    registry.register(ToolSpec("t", "d", schema, lambda **kwargs: "ok"))
    with pytest.raises(ValueError):
        registry.register(ToolSpec("t", "d", schema, lambda **kwargs: "ok"))


def test_registry_describe_lines(tmp_path: Path) -> None:
    registry = build_registry(make_config(tmp_path))
    lines = registry.describe_lines()
    assert any(line.startswith("read_file(path)") for line in lines)
    assert any(line.startswith("get_time()") for line in lines)
    assert len(lines) == 4


def test_registry_accepts_json_string_arguments(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    (tmp_path / "workspace" / "s.txt").write_text("字符串参数", encoding="utf-8")
    registry = build_registry(config)
    assert registry.call("read_file", json.dumps({"path": "s.txt"})).endswith("字符串参数")


# ============================================================ 8. 示例工具 get_time
def test_get_time_is_registered_and_returns_parseable_time() -> None:
    """① 注册存在；③ 返回的时间戳能被 datetime.fromisoformat 解析。"""
    registry = build_registry(load_config(EXAMPLE_CONFIG, load_env_file=False))
    assert "get_time" in registry
    assert registry.get("get_time") is not None

    for zone, label in (("UTC", "当前时间（UTC）："), ("LOCAL", "当前时间（本地）：")):
        result = registry.call("get_time", {"zone": zone})
        assert result.startswith(label), result
        stamp = result.split("：", 1)[1].strip()
        parsed = datetime.fromisoformat(stamp)  # 解析失败会直接抛异常
        assert parsed.tzinfo is not None, f"{zone} 的时间应带时区信息：{result}"

    # 省略 zone 时按 UTC 处理
    assert registry.call("get_time", {}).startswith("当前时间（UTC）：")
    # 非法 zone 走 ToolError → 错误文本，不崩
    bad = registry.call("get_time", {"zone": "Mars"})
    assert bad.startswith("错误：") and "只支持" in bad


def test_get_time_schema_is_strict() -> None:
    """② JSON Schema 正确：显式 type / description / required / additionalProperties。"""
    registry = build_registry(load_config(EXAMPLE_CONFIG, load_env_file=False))
    spec = registry.get("get_time")
    assert spec is not None
    assert spec.parameters == {
        "type": "object",
        "properties": {
            "zone": {"type": "string", "description": "UTC 或 LOCAL；省略则按 UTC 处理"}
        },
        "required": [],  # 可选参数也必须显式声明 required
        "additionalProperties": False,
    }
    schema = spec.schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_time"
    assert schema["function"]["description"]
    # 多传字段会被本地校验拦下
    assert registry.call("get_time", {"zone": "UTC", "extra": 1}).startswith("错误：")
    # 类型不符也会被拦下
    assert registry.call("get_time", {"zone": 123}).startswith("错误：")


def test_get_time_is_enabled_in_example_config() -> None:
    """④ config.example.yaml 的 tools.enabled 默认值含 get_time。"""
    config = load_config(EXAMPLE_CONFIG, load_env_file=False)
    assert "get_time" in config.tools.enabled
    assert config.tools.enabled == ["read_file", "write_file", "list_dir", "get_time"]


def test_get_time_is_in_code_defaults(tmp_path: Path) -> None:
    """代码默认值与示例配置一致（不写 tools 段时也启用）。"""
    config = make_config(tmp_path)
    assert "get_time" in config.tools.enabled
    assert "get_time" in build_registry(config)