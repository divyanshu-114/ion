from __future__ import annotations

import pytest

from ion.artifacts import ArtifactStore
from ion.contracts import Phase, ToolCall
from ion.processes import CommandSupervisor
from ion.tools.registry import ToolDispatcher, tool_schemas
from ion.workspace import Workspace, digest


def dispatcher_for(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    workspace = Workspace.capture(repo)
    return repo, artifacts, ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))


async def call(dispatcher, name, **arguments):
    return await dispatcher.execute(ToolCall(task_id="test", tool=name, arguments=arguments))


@pytest.mark.asyncio
async def test_file_outline_returns_bounded_ranges_without_source_bodies(tmp_path):
    repo, _, dispatcher = dispatcher_for(tmp_path)
    (repo / "module.py").write_text("import os\n\nclass Widget:\n    secret = 'do not echo'\n\ndef build():\n    return 'private body'\n")
    result = await call(dispatcher, "file_outline", relative_path="module.py", max_items=2)
    assert result.status == "succeeded"
    assert len(result.data["items"]) == 2
    assert result.data["items"][0]["start_line"] == 1
    assert result.data["items"][1]["start_line"] == 3
    assert result.data["items"][1]["end_line"] == 5
    assert result.truncated is True
    assert "do not echo" not in str(result.data)
    assert "private body" not in str(result.data)
    blocked = await call(dispatcher, "file_outline", relative_path="../outside")
    assert blocked.status == "failed"


@pytest.mark.asyncio
async def test_artifact_search_paginates_bounded_snippets(tmp_path):
    _, artifacts, dispatcher = dispatcher_for(tmp_path)
    marker = "needle"
    payload = ("A" * 300 + marker + "B" * 300 + "\n") * 5
    artifact = artifacts.put(payload.encode(), "command_output")
    first = await call(dispatcher, "artifact_search", artifact_id=artifact.artifact_id, query=marker, max_matches=2)
    assert first.status == "succeeded"
    assert len(first.data["matches"]) == 2
    assert all(len(item["snippet"]) <= 160 for item in first.data["matches"])
    assert first.data["next_cursor"] is not None
    assert first.truncated is True
    second = await call(dispatcher, "artifact_search", artifact_id=artifact.artifact_id, query=marker,
                        cursor=first.data["next_cursor"], max_matches=2)
    assert len(second.data["matches"]) == 2
    assert second.data["matches"][0]["offset"] > first.data["matches"][-1]["offset"]
    invalid = await call(dispatcher, "artifact_search", artifact_id=artifact.artifact_id, query=marker, max_matches=21)
    assert invalid.status == "failed"


@pytest.mark.asyncio
async def test_artifact_read_pages_report_truncation_and_decode_loss(tmp_path):
    _, artifacts, dispatcher = dispatcher_for(tmp_path)
    artifact = artifacts.put(b"abc\xffdefghi", "command_output")
    first = await call(dispatcher, "artifact_read", artifact_id=artifact.artifact_id, offset=0, limit=4)
    assert first.status == "succeeded"
    assert len(first.data["text"]) <= 4
    assert first.data["next_offset"] == 4
    assert first.truncated is True
    assert first.lossy is True
    last = await call(dispatcher, "artifact_read", artifact_id=artifact.artifact_id, offset=4, limit=4)
    assert last.status == "succeeded"
    assert last.data["text"] == "defg"
    assert last.data["next_offset"] == 8
    assert last.lossy is True
    for limit in (0, 16001):
        assert (await call(dispatcher, "artifact_read", artifact_id=artifact.artifact_id, limit=limit)).status == "failed"


@pytest.mark.asyncio
async def test_incomplete_artifact_stays_lossy_after_store_reopens(tmp_path):
    _, artifacts, dispatcher = dispatcher_for(tmp_path)
    artifact = artifacts.put(b"first line\nsecond line\n", "command_output", complete=False)
    reopened = ArtifactStore(artifacts.root)
    reopened_dispatcher = ToolDispatcher(dispatcher.workspace, reopened, CommandSupervisor(dispatcher.workspace, reopened))
    page = await call(reopened_dispatcher, "artifact_read", artifact_id=artifact.artifact_id, limit=10)
    search = await call(reopened_dispatcher, "artifact_search", artifact_id=artifact.artifact_id, query="second")
    assert page.status == "succeeded"
    assert page.data["complete"] is False
    assert page.lossy is True
    assert search.status == "succeeded"
    assert search.data["complete"] is False
    assert search.lossy is True
    assert search.data["matches"][0]["offset"] == 11
    assert reopened.has_artifacts() is True


@pytest.mark.asyncio
async def test_legacy_artifact_is_discoverable_and_readable_without_metadata(tmp_path):
    from ion.tools.bundles import select_tool_bundle

    _, artifacts, dispatcher = dispatcher_for(tmp_path)
    (artifacts.root / "MEMORY.md").write_text("task pointers")
    assert artifacts.has_artifacts() is False
    (artifacts.root / "legacy-output").write_bytes(b"before needle after")

    assert artifacts.has_artifacts() is True
    offered = select_tool_bundle(Phase.act, has_artifacts=artifacts.has_artifacts())
    assert {"artifact_read", "artifact_search"} <= set(offered)
    page = await call(dispatcher, "artifact_read", artifact_id="legacy-output")
    search = await call(dispatcher, "artifact_search", artifact_id="legacy-output", query="needle")
    assert page.status == "succeeded"
    assert page.data["text"] == "before needle after"
    assert page.data["complete"] is False
    assert search.status == "succeeded"
    assert search.data["matches"][0]["offset"] == 7


@pytest.mark.asyncio
async def test_diff_summary_shows_hashes_and_sizes_without_patch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / "app.py"
    path.write_text("before\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    workspace = Workspace.capture(repo)
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    old_hash = digest(b"before\n")
    read = await call(dispatcher, "file_read", relative_path="app.py")
    assert read.data["sha256"] == old_hash
    edited = await call(dispatcher, "patch_apply", edits=[{
        "path": "app.py", "expected_hash": old_hash, "old_text": "before", "new_text": "after"
    }])
    assert edited.status == "succeeded"
    result = await call(dispatcher, "diff_summary", relative_paths=["app.py"])
    assert result.status == "succeeded"
    assert result.data["files"][0]["path"] == "app.py"
    assert result.data["files"][0]["before_hash"] == old_hash
    assert result.data["files"][0]["after_hash"] == digest(b"after\n")
    assert result.data["files"][0]["after_bytes"] == len(b"after\n")
    assert "patch" not in result.data
    assert "+after" not in str(result.data)


@pytest.mark.asyncio
async def test_diff_inspect_retains_patch_without_echoing_body(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("before\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    workspace = Workspace.capture(repo)
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    assert (await call(dispatcher, "file_read", relative_path="app.py")).status == "succeeded"
    changed = await call(dispatcher, "patch_apply", edits=[{
        "path": "app.py", "expected_hash": digest(b"before\n"),
        "old_text": "before", "new_text": "after",
    }])
    assert changed.status == "succeeded"
    inspected = await call(dispatcher, "diff_inspect")
    assert inspected.status == "succeeded"
    assert "app.py" in inspected.data["changed_files"]
    assert "-before" not in str(inspected.data)
    assert "+after" not in str(inspected.data)
    assert b"+after" in artifacts.read(inspected.data["patch_artifact_id"])


@pytest.mark.asyncio
async def test_patch_apply_rejects_ninth_edit_and_oversized_batch_without_writes(tmp_path):
    repo, _, dispatcher = dispatcher_for(tmp_path)
    edits = []
    for index in range(9):
        path = repo / f"{index}.txt"
        path.write_text("old")
        edits.append({"path": path.name, "expected_hash": digest(b"old"), "old_text": "old", "new_text": "new"})
    too_many = await call(dispatcher, "patch_apply", edits=edits)
    assert too_many.status == "failed"
    assert all((repo / f"{i}.txt").read_text() == "old" for i in range(9))
    from ion.tools.registry import MAX_PATCH_REPLACEMENT_BYTES
    too_large = await call(dispatcher, "patch_apply", edits=[{**edits[0], "new_text": "x" * (MAX_PATCH_REPLACEMENT_BYTES + 1)}])
    assert too_large.status == "failed"
    assert (repo / "0.txt").read_text() == "old"


@pytest.mark.asyncio
async def test_patch_apply_requires_observed_read_for_every_normalized_target(tmp_path):
    repo, _, dispatcher = dispatcher_for(tmp_path)
    for name in ("a.py", "b.py"):
        (repo / name).write_text("before\n")
    expected_hash = digest(b"before\n")
    first = {"path": "./a.py", "expected_hash": expected_hash,
             "old_text": "before", "new_text": "after"}
    second = {**first, "path": "b.py"}
    unread = await call(dispatcher, "patch_apply", edits=[first])
    assert unread.status == "failed"
    assert "read" in unread.error.lower()
    assert (repo / "a.py").read_text() == "before\n"
    await call(dispatcher, "file_read", relative_path="a.py")
    partial = await call(dispatcher, "patch_apply", edits=[first, second])
    assert partial.status == "failed"
    assert all((repo / name).read_text() == "before\n" for name in ("a.py", "b.py"))
    await call(dispatcher, "file_read", relative_path="b.py")
    applied = await call(dispatcher, "patch_apply", edits=[first, second])
    assert applied.status == "succeeded"
    assert all((repo / name).read_text() == "after\n" for name in ("a.py", "b.py"))


@pytest.mark.asyncio
async def test_invalid_patch_schema_does_not_echo_submitted_text(tmp_path):
    repo, _, dispatcher = dispatcher_for(tmp_path)
    (repo / "app.py").write_text("before")
    secret = "private-patch-body" * 500
    result = await call(dispatcher, "patch_apply", edits=[{
        "path": "app.py", "expected_hash": digest(b"before"),
        "old_text": "before", "new_text": [secret],
    }])
    assert result.status == "failed"
    assert len(result.error) < 256
    assert "private-patch-body" not in result.error
    assert (repo / "app.py").read_text() == "before"


def test_progressive_bundles_expose_evidence_gated_tools(tmp_path):
    from ion.tools.bundles import select_tool_bundle

    artifacts = ArtifactStore(tmp_path / "artifacts")
    navigate = select_tool_bundle(Phase.inspect, edit_intent=False, observed_page_count=0,
                                  has_artifacts=artifacts.has_artifacts(), target_hashes_available=False, allow_commands=False)
    verify = select_tool_bundle(Phase.verify, edit_intent=False, observed_page_count=0,
                                has_artifacts=artifacts.has_artifacts(), target_hashes_available=False, allow_commands=False)
    assert len(tool_schemas(navigate)) < len(tool_schemas())
    assert len(tool_schemas(verify)) < len(tool_schemas())
    assert "file_outline" in navigate
    assert "artifact_search" not in navigate + verify
    assert "command_start" not in verify
    assert "edit_file" not in navigate
    edit = select_tool_bundle(Phase.act, edit_intent=True, observed_page_count=0,
                              has_artifacts=False, target_hashes_available=False, allow_commands=False)
    assert "edit_file" not in edit
    assert "patch_apply" not in edit
    edit_ready = select_tool_bundle(Phase.act, edit_intent=True, observed_page_count=1,
                                    has_artifacts=False, target_hashes_available=True, allow_commands=False)
    assert {"edit_file", "patch_apply"} <= set(edit_ready)
    artifacts.put(b"output", "command_output")
    with_artifact = select_tool_bundle(Phase.verify, edit_intent=False, observed_page_count=1,
                                       has_artifacts=artifacts.has_artifacts(), target_hashes_available=False, allow_commands=True)
    assert {"artifact_search", "artifact_read", "command_start"} <= set(with_artifact)
