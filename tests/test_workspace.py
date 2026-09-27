from pathlib import Path

import pytest

from ion.artifacts import ArtifactStore
from ion.contracts import ToolCall
from ion.tools.registry import ToolDispatcher
from ion.workspace import Workspace, digest


class NoCommands:
    async def run(self, command):
        raise AssertionError("command must not run")


@pytest.mark.asyncio
async def test_patch_requires_current_hash_and_preserves_external_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("before\n")
    (repo / "user.txt").write_text("original\n")
    workspace = Workspace.capture(repo)
    tools = ToolDispatcher(workspace, ArtifactStore(tmp_path / "artifacts"), NoCommands())
    (repo / "user.txt").write_text("external\n")
    good = {"path": "app.py", "expected_hash": digest(b"before\n"), "old_text": "before", "new_text": "after"}
    observed = await tools.execute(ToolCall(task_id="t", tool="file_read", arguments={"relative_path": "app.py"}))
    assert observed.status.value == "succeeded"
    result = await tools.execute(ToolCall(task_id="t", tool="patch_apply", arguments={"edits": [good]}))
    assert result.status.value == "succeeded"
    assert (repo / "app.py").read_text() == "after\n"
    assert workspace.changes().external_files == ("user.txt",)
    stale = await tools.execute(ToolCall(task_id="t", tool="patch_apply", arguments={"edits": [good]}))
    assert stale.status.value == "failed"
    assert (repo / "app.py").read_text() == "after\n"
    assert "-before" in workspace.patch_text()


@pytest.mark.asyncio
async def test_file_read_rejects_private_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("SECRET=fixture\n")
    workspace = Workspace.capture(repo)
    tools = ToolDispatcher(workspace, ArtifactStore(tmp_path / "artifacts"), NoCommands())
    result = await tools.execute(ToolCall(task_id="t", tool="file_read", arguments={"relative_path": ".env"}))
    assert result.status.value == "failed"
    assert "fixture" not in str(result)
