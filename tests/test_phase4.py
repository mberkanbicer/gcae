from pathlib import Path

import pytest

from gcae.models import ToolCall
from gcae.tools import DangerousCommand, ToolRegistry, WorkspaceViolation


def test_workspace_escape_rejected(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    with pytest.raises(WorkspaceViolation):
        tools.read_file({"path": "../outside"})


def test_dangerous_command_rejected(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    with pytest.raises(DangerousCommand):
        tools.run_command({"command": "sudo rm -rf /"})
    with pytest.raises(DangerousCommand):
        tools.run_command({"command": "git reset --hard HEAD"})


def test_file_tools(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    result = tools.create_file({"path": "a.txt", "content": "hello"})
    assert result.success
    assert tools.read_file({"path": "a.txt"}).output == "hello"
    assert "a.txt" in tools.list_files({}).output
    assert "a.txt:1:hello" in tools.search_text({"query": "hello"}).output


def test_patch_workspace_escape_rejected(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    with pytest.raises(WorkspaceViolation):
        tools.apply_patch(
            {
                "patch": (
                    "diff --git a/../outside.txt b/../outside.txt\n"
                    "--- a/../outside.txt\n"
                    "+++ b/../outside.txt\n"
                    "@@ -0,0 +1 @@\n"
                    "+outside\n"
                )
            }
        )


def test_create_file_refusal_points_at_the_alternative(tmp_path: Path) -> None:
    """The model must be told what to do instead, not just that it failed."""
    registry = ToolRegistry(tmp_path)
    (tmp_path / "notes.md").write_text("existing\n")
    result = registry.execute(
        ToolCall(name="create_file", arguments={"path": "notes.md", "content": "new\n"})
    )
    assert result.success is False
    assert "write_file" in (result.error or "")
    assert (tmp_path / "notes.md").read_text() == "existing\n"


def test_search_regex_include_context_and_cap(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "a.py").write_text("alpha\nfoo123\nomega\n")
    (tmp_path / "b.txt").write_text("foo123\n")
    matched = tools.search_text({"query": r"foo\d+", "regex": True, "include": "**/*.py"})
    assert matched.success
    assert "a.py:2:foo123" in matched.output
    assert "b.txt" not in matched.output
    with_context = tools.search_text({"query": "foo123", "context_lines": 1})
    assert "a.py:1-alpha" in with_context.output
    assert "a.py:3-omega" in with_context.output
    capped = tools.search_text({"query": "foo123", "max_matches": 1})
    assert "max_matches=1 reached" in capped.output
    failed = tools.execute(
        ToolCall(name="search_text", arguments={"query": "([", "regex": True})
    )
    assert failed.success is False
    assert "invalid regex" in (failed.error or "")
    failed = tools.execute(
        ToolCall(name="search_text", arguments={"query": "x", "context_lines": -1})
    )
    assert failed.success is False


def test_writes_are_atomic_and_create_is_exclusive(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    assert tools.write_file({"path": "sub/a.txt", "content": "v1"}).success
    assert (tmp_path / "sub" / "a.txt").read_text() == "v1"
    assert list(tmp_path.glob(".gcae-tmp-*")) == []
    assert list((tmp_path / "sub").glob(".gcae-tmp-*")) == []
    assert tools.create_file({"path": "new.txt", "content": "n"}).success
    raced = tools.execute(
        ToolCall(name="create_file", arguments={"path": "new.txt", "content": "other"})
    )
    assert raced.success is False
    assert (tmp_path / "new.txt").read_text() == "n"


def test_run_command_cwd_and_env(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "proj").mkdir()
    result = tools.run_command({"command": "pwd", "cwd": "proj"})
    assert result.success
    assert result.output.strip().endswith("proj")
    assert "proj" in tools.run_command({"command": "pwd", "cwd": "proj"}).cwd
    probed = tools.run_command(
        {"command": "echo $GCAE_PROBE", "env": {"GCAE_PROBE": "beacon"}}
    )
    assert probed.success
    assert probed.output.strip() == "beacon"
    defaulted = tools.run_command({"command": "pwd"})
    assert defaulted.success
    assert Path(defaulted.cwd) == tmp_path
    with pytest.raises(WorkspaceViolation):
        tools.run_command({"command": "pwd", "cwd": "../outside"})
    failed = tools.execute(
        ToolCall(name="run_command", arguments={"command": "pwd", "cwd": "missing"})
    )
    assert failed.success is False
    assert "not a directory" in (failed.error or "")
    failed = tools.execute(
        ToolCall(name="run_command", arguments={"command": "pwd", "env": {"K": 1}})
    )
    assert failed.success is False


def test_read_files_partial_results(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "b.txt").write_text("beta\n")
    result = tools.read_files({"paths": ["a.txt", "b.txt"]})
    assert result.success
    assert "=== a.txt ===\nalpha" in result.output
    assert "=== b.txt ===\nbeta" in result.output
    partial = tools.read_files({"paths": ["a.txt", "missing.txt"]})
    assert partial.success is False
    assert "alpha" in partial.output
    assert "missing.txt" in (partial.error or "")
    failed = tools.execute(ToolCall(name="read_files", arguments={"paths": []}))
    assert failed.success is False
    with pytest.raises(WorkspaceViolation):
        tools.read_files({"paths": ["../outside"]})


def test_write_files_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "keep.txt").write_text("orig\n")
    (tmp_path / "blocker").mkdir()
    result = tools.write_files(
        {"files": [{"path": "a.txt", "content": "A\n"}, {"path": "b.txt", "content": "B\n"}]}
    )
    assert result.success
    assert sorted(result.changed_files) == ["a.txt", "b.txt"]
    refused = tools.execute(
        ToolCall(
            name="write_files",
            arguments={
                "files": [
                    {"path": "keep.txt", "content": "NEW\n"},
                    {"path": "blocker", "content": "x"},
                ]
            },
        )
    )
    assert refused.success is False
    assert "directory" in (refused.error or "")
    assert (tmp_path / "keep.txt").read_text() == "orig\n"
    calls = 0
    real_write = ToolRegistry._write_atomically

    def flaky(path: Path, content: str, *, exclusive: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        real_write(path, content, exclusive=exclusive)

    monkeypatch.setattr(ToolRegistry, "_write_atomically", staticmethod(flaky))
    failed = tools.execute(
        ToolCall(
            name="write_files",
            arguments={
                "files": [
                    {"path": "keep.txt", "content": "NEW\n"},
                    {"path": "fresh.txt", "content": "x"},
                ]
            },
        )
    )
    assert failed.success is False
    assert "rolled back" in (failed.error or "")
    assert (tmp_path / "keep.txt").read_text() == "orig\n"
    assert not (tmp_path / "fresh.txt").exists()


def test_make_dirs(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    result = tools.make_dirs({"paths": ["a/b", "c"]})
    assert result.success
    assert (tmp_path / "a" / "b").is_dir()
    assert (tmp_path / "c").is_dir()
    again = tools.make_dirs({"paths": ["a/b"]})
    assert again.success
    (tmp_path / "file.txt").write_text("x")
    failed = tools.execute(ToolCall(name="make_dirs", arguments={"paths": ["file.txt"]}))
    assert failed.success is False
    with pytest.raises(WorkspaceViolation):
        tools.make_dirs({"paths": ["../outside"]})


def test_edit_file_exact_replacement(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "a.txt").write_text("hello world\n")
    result = tools.edit_file({"path": "a.txt", "old_text": "world", "new_text": "there"})
    assert result.success
    assert result.changed_files == ["a.txt"]
    assert (tmp_path / "a.txt").read_text() == "hello there\n"
    failed = tools.execute(
        ToolCall(name="edit_file", arguments={"path": "a.txt", "old_text": "absent"})
    )
    assert failed.success is False
    assert "not found" in (failed.error or "")
    (tmp_path / "b.txt").write_text("x x x\n")
    ambiguous = tools.execute(
        ToolCall(name="edit_file", arguments={"path": "b.txt", "old_text": "x", "new_text": "y"})
    )
    assert ambiguous.success is False
    assert "3 times" in (ambiguous.error or "")
    assert (tmp_path / "b.txt").read_text() == "x x x\n"
    assert tools.edit_file(
        {"path": "b.txt", "old_text": "x", "new_text": "y", "replace_all": True}
    ).success
    assert (tmp_path / "b.txt").read_text() == "y y y\n"


def test_edit_files_validates_before_writing(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path)
    (tmp_path / "a.txt").write_text("aaa\n")
    (tmp_path / "b.txt").write_text("bbb\n")
    result = tools.edit_files(
        {
            "edits": [
                {"path": "a.txt", "old_text": "aaa", "new_text": "A"},
                {"path": "b.txt", "old_text": "bbb", "new_text": "B"},
            ]
        }
    )
    assert result.success
    assert sorted(result.changed_files) == ["a.txt", "b.txt"]
    failed = tools.execute(
        ToolCall(
            name="edit_files",
            arguments={
                "edits": [
                    {"path": "a.txt", "old_text": "A", "new_text": "Z"},
                    {"path": "b.txt", "old_text": "absent", "new_text": "Z"},
                ]
            },
        )
    )
    assert failed.success is False
    assert (tmp_path / "a.txt").read_text() == "A\n"
    assert (tmp_path / "b.txt").read_text() == "B\n"


class _FakeResponse:
    content = b"hello web"

    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def get(self, url: str) -> _FakeResponse:
        assert url == "http://example.com/x"
        return _FakeResponse()


def test_fetch_url_guards_and_truncation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    tools = ToolRegistry(tmp_path)
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    result = tools.fetch_url({"url": "http://example.com/x"})
    assert result.success
    assert result.output == "hello web"
    short = tools.fetch_url({"url": "http://example.com/x", "max_bytes": 5})
    assert short.success
    assert short.output == "hello\n[truncated to 5 bytes]"
    for bad in ("ftp://example.com/x", "http://127.0.0.1/", "http://10.0.0.1/", "not-a-url"):
        failed = tools.execute(ToolCall(name="fetch_url", arguments={"url": bad}))
        assert failed.success is False, bad
    unresolvable = tools.execute(
        ToolCall(name="fetch_url", arguments={"url": "http://nonexistent.invalid/"})
    )
    assert unresolvable.success is False
