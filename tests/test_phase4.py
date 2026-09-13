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
