from pathlib import Path

import httpx
import pytest

from ion.artifacts import ArtifactStore
from ion.config import load_config, resolve_profile
from ion.contracts import ModelEvent, ModelRequest, TaskSpec, ToolCall
from ion.engine import Engine
from ion.processes import CommandSupervisor
from ion.providers.openai_compatible import OpenAICompatibleProvider
from ion.providers.scripted import ScriptedProvider
from ion.tools.registry import ToolDispatcher
from ion.workspace import Workspace


def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    artifacts = ArtifactStore(tmp_path / "artifacts")
    workspace = Workspace.capture(repo)
    return repo, ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))


async def execute(dispatcher, name, **arguments):
    return await dispatcher.execute(ToolCall(task_id="test", tool=name, arguments=arguments))


@pytest.mark.asyncio
async def test_rewrite_requires_full_current_read_and_preserves_external_changes(tmp_path):
    repo, dispatcher = setup(tmp_path)
    original = "a" * 4000 + "b" * 4000 + "end\n"
    (repo / "README.md").write_text(original)
    await execute(dispatcher, "file_read", relative_path="README.md")
    args = dict(plan="Rewrite README", relative_path="README.md", read_id="r1", content="# Rewritten\n", done=True)
    partial = await execute(dispatcher, "write_file", **args)
    assert partial.status == "failed"
    assert (repo / "README.md").read_text() == original
    await execute(dispatcher, "file_read", relative_path="README.md", offset=4000, limit=8000)
    rewritten = await execute(dispatcher, "write_file", **args)
    assert rewritten.status == "succeeded"
    assert (repo / "README.md").read_text() == "# Rewritten\n"
    (repo / "README.md").write_text("External change\n")
    stale = await execute(dispatcher, "write_file", **args)
    assert stale.status == "failed"
    assert (repo / "README.md").read_text() == "External change\n"


@pytest.mark.asyncio
async def test_creation_and_empty_file_rewrite_are_guarded(tmp_path):
    repo, dispatcher = setup(tmp_path)
    args = dict(plan="Add usage docs", relative_path="docs/usage.md", content="# Usage\n", done=False)
    created = await execute(dispatcher, "write_file", **args)
    assert created.status == "succeeded"
    assert "docs/usage.md" in dispatcher.workspace.changes().attributable_files
    overwrite = await execute(dispatcher, "write_file", **{**args, "content": "Overwrite"})
    assert overwrite.status == "failed"
    assert (repo / "docs/usage.md").read_text() == "# Usage\n"
    (repo / "empty.txt").touch()
    read = await execute(dispatcher, "file_read", relative_path="empty.txt")
    filled = await execute(dispatcher, "write_file", plan="Fill empty file", relative_path="empty.txt",
                           content="Hello", read_id=read.data["read_id"], done=True)
    assert filled.status == "succeeded"
    (repo / "outside").symlink_to(tmp_path, target_is_directory=True)
    for path in ("../escaped", str(tmp_path / "escaped"), "outside/escaped", ".env"):
        result = await execute(dispatcher, "write_file", **{**args, "relative_path": path})
        assert result.status == "failed"
    assert not (tmp_path / "escaped").exists()


@pytest.mark.asyncio
async def test_delete_requires_observed_current_file_and_records_attribution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "obsolete.txt").write_text("remove me\n")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    workspace = Workspace.capture(repo)
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    read = await execute(dispatcher, "file_read", relative_path="obsolete.txt")
    deleted = await execute(dispatcher, "delete_file", plan="Remove obsolete file", relative_path="obsolete.txt",
                            read_id=read.data["read_id"], done=True)
    assert deleted.status == "succeeded"
    assert deleted.data["deleted_files"] == ["obsolete.txt"]
    assert not (repo / "obsolete.txt").exists()
    assert dispatcher.workspace.changes().attributable_files == ("obsolete.txt",)
    assert "-remove me" in dispatcher.workspace.patch_text()


@pytest.mark.asyncio
async def test_delete_rejects_wrong_or_stale_reads_and_directories(tmp_path):
    repo, dispatcher = setup(tmp_path)
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    (repo / "folder").mkdir()
    read_a = await execute(dispatcher, "file_read", relative_path="a.txt")
    read_b = await execute(dispatcher, "file_read", relative_path="b.txt")
    wrong = await execute(dispatcher, "delete_file", plan="Remove b", relative_path="b.txt",
                          read_id=read_a.data["read_id"], done=True)
    assert wrong.status == "failed"
    (repo / "b.txt").write_text("changed\n")
    stale = await execute(dispatcher, "delete_file", plan="Remove b", relative_path="b.txt",
                          read_id=read_b.data["read_id"], done=True)
    assert stale.status == "failed"
    directory = await execute(dispatcher, "delete_file", plan="Remove folder", relative_path="folder",
                              read_id=read_a.data["read_id"], done=True)
    assert directory.status == "failed"
    assert (repo / "b.txt").exists()
    assert (repo / "folder").is_dir()


@pytest.mark.asyncio
async def test_truncated_response_preserves_usage_and_emits_no_partial_tools():
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, config.default_profile, "product")
    payload = {"usage": {"prompt_tokens": 100, "completion_tokens": 768,
                         "completion_tokens_details": {"reasoning_tokens": 700}},
               "choices": [{"finish_reason": "length", "message": {"tool_calls": [
                   {"id": "partial", "function": {"name": "write_file", "arguments": '{"content":"unfinished'}}]}}]}
    provider = OpenAICompatibleProvider(profile, "fixture", httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    request = ModelRequest(messages=({"role": "user", "content": "Rewrite"},), max_output_tokens=768, profile_digest="test")
    events = [event async for event in provider.generate(request)]
    assert [event.kind for event in events] == ["usage", "error"]
    assert events[0].usage["reasoning_tokens"] == 700
    assert events[1].finish_reason == "length"


@pytest.mark.asyncio
async def test_named_rewrite_prefetches_source_and_edits_in_one_model_call(tmp_path):
    repo, dispatcher = setup(tmp_path)
    (repo / "README.md").write_text("# Existing documentation\nKeep these facts.\n")
    provider = ScriptedProvider([[ModelEvent(kind="tool_call", tool="write_file", call_id="write", arguments={
        "plan": "Reorganize existing facts", "relative_path": "README.md", "read_id": "r1",
        "content": "# Documentation\n\nKeep these facts.\n", "done": True,
    })]])
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    config = config.model_copy(update={"economy": config.economy.model_copy(update={"enabled": True})})
    result = await Engine(config, provider, dispatcher).run(TaskSpec(text="rewrite the readme", repo_path=str(repo), profile_name=config.default_profile))
    assert len(provider.requests) == 1
    assert provider.requests[0].tool_choice == "write_file"
    assert provider.requests[0].max_output_tokens == 4096
    assert "Keep these facts." in str(provider.requests[0].messages)
    assert result.outcome == "unverified"
    assert (repo / "README.md").read_text() == "# Documentation\n\nKeep these facts.\n"


@pytest.mark.asyncio
async def test_named_tool_choice_is_sent_without_changing_model():
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, config.default_profile, "product")
    import json
    def response(request):
        body = json.loads(request.content)
        assert body["tool_choice"] == {"type": "function", "function": {"name": "write_file"}}
        assert body["model"] == profile.model_id
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
    provider = OpenAICompatibleProvider(profile, "fixture", httpx.MockTransport(response))
    request = ModelRequest(messages=({"role": "user", "content": "Rewrite"},), tools=({"type": "function", "function": {"name": "write_file"}},),
                           max_output_tokens=4096, profile_digest="test", tool_choice="write_file")
    assert [e.kind async for e in provider.generate(request)] == ["completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["native", "structured_json"])
async def test_multifile_edit_can_reread_after_write_and_recovers_truncation(tmp_path, protocol):
    import json

    repo, dispatcher = setup(tmp_path)
    (repo / "a.txt").write_text("before")
    (repo / "b.txt").write_text("original")
    def turn(tool, **args):
        usage = ModelEvent(kind="usage", usage={"prompt_tokens": 500, "completion_tokens": 150})
        if protocol == "structured_json":
            return [ModelEvent(kind="text_delta", text=json.dumps({"action": "tool", "tool": tool, "arguments": args})), usage]
        return [ModelEvent(kind="tool_call", tool=tool, arguments=args, call_id="call"), usage]
    provider = ScriptedProvider([
        turn("file_read", relative_path="a.txt"),
        [ModelEvent(kind="usage", usage={"prompt_tokens": 100, "completion_tokens": 2048}),
         ModelEvent(kind="error", error="provider output truncated", finish_reason="length")],
        turn("edit_file", plan="Correct a", read_id="r1", old_text="before", new_text="after", done=False),
        turn("file_read", relative_path="b.txt"),
        turn("file_read", relative_path="a.txt"),
        turn("write_file", plan="Update b", relative_path="b.txt", read_id="r2", content="updated", done=True),
    ])
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    config = config.model_copy(update={"economy": config.economy.model_copy(update={"enabled": True})})
    profile = resolve_profile(config, config.default_profile, "product").model_copy(update={"tool_protocol": protocol})
    result = await Engine(config, provider, dispatcher, profile_override=profile).run(
        TaskSpec(text="Fix both files", repo_path=str(repo), profile_name=config.default_profile))
    assert result.outcome == "unverified"
    assert (repo / "a.txt").read_text() == "after"
    assert (repo / "b.txt").read_text() == "updated"
    assert provider.requests[2].max_output_tokens > provider.requests[1].max_output_tokens
    if protocol == "native":
        assert all("file_read" in {t["function"]["name"] for t in request.tools} for request in provider.requests)
