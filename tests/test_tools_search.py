"""search_code / git_status 的只读工具测试。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.tools import Workspace
from agent.tools.git_status import git_status
from agent.tools.search import MAX_RESULTS, search_code


def make_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def hello():\n    print('Needle')\n", encoding="utf-8"
    )
    (root / "src" / "view.ts").write_text(
        "const needle = 'Needle'\n", encoding="utf-8"
    )
    (root / "README.md").write_text("needle in docs\n", encoding="utf-8")
    return Workspace(root=root)


def test_search_code_is_case_insensitive_and_reports_line_numbers(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    result = search_code(workspace, "needle")
    assert "src/app.py:2:" in result
    assert "src/view.ts:1:" in result
    assert "README.md:1:" in result


def test_search_code_filters_by_extension(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    result = search_code(workspace, "needle", "py")
    assert "src/app.py:2:" in result
    assert "src/view.ts" not in result
    assert "README.md" not in result


def test_search_code_skips_vcs_and_binary_files(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    git_dir = workspace.root / ".git"
    git_dir.mkdir()
    (git_dir / "secret.py").write_text("needle", encoding="utf-8")
    (workspace.root / "binary.py").write_bytes(b"needle\x00binary")
    result = search_code(workspace, "needle")
    assert ".git/secret.py" not in result
    assert "binary.py" not in result


def test_search_code_rejects_empty_keyword(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    with pytest.raises(Exception, match="keyword"):
        search_code(workspace, "   ")


def test_search_code_reports_no_match(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    assert "未找到匹配内容" in search_code(workspace, "definitely-not-here")


@pytest.mark.skipif(shutil.which("git") is None, reason="git 未安装")
def test_git_status_reads_short_status(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    (root / "changed.txt").write_text("x", encoding="utf-8")
    result = git_status(Workspace(root=root))
    assert "?? changed.txt" in result


def test_git_status_reports_non_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 不真正创建外部临时目录（Windows 上可能被安全软件拖慢），直接模拟 git 的失败输出。
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 128, "", "fatal: not a git repository (or any of the parent directories): .git"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(Exception, match="git status"):
        git_status(Workspace(root=tmp_path))


def test_search_code_result_limit_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "many"
    root.mkdir()
    (root / "a.txt").write_text("\n".join(["needle"] * (MAX_RESULTS + 5)), encoding="utf-8")
    result = search_code(Workspace(root=root), "needle")
    assert result.count("a.txt:") >= MAX_RESULTS
    assert "达到上限" in result