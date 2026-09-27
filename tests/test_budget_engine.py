import json
from pathlib import Path

import pytest

from ion.artifacts import ArtifactStore
from ion.config import EconomyConfig, load_config
from ion.context import ContextManager, ContextOverflowError, ContextPacket
from ion.contracts import ContextManifest, ModelEvent, Phase, TaskSpec, ToolResult
from ion.engine import Engine
from ion.processes import CommandSupervisor
from ion.providers.scripted import ScriptedProvider
from ion.tools.registry import ToolDispatcher
from ion.tools.bundles import select_tool_bundle
from ion.workspace import Workspace
from ion.workspace import digest


def _engine(tmp_path, provider, *, tokens=24000, input_budget=6000, files=None, economy_fields=None, allow_commands=False):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name, content in (files or {}).items():
        (repo / name).write_text(content)
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts), allow_commands=allow_commands)
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    config = config.model_copy(update={"economy": config.economy.model_copy(update={"max_total_tokens": tokens, **(economy_fields or {})})})
    profile = config.profiles[config.default_profile].copy()
    profile["input_budget_tokens"] = input_budget
    config = config.model_copy(update={"profiles": {**config.profiles, config.default_profile: profile}})
    return Engine(config, provider, dispatcher), repo


@pytest.mark.asyncio
async def test_engine_reports_context_overflow_without_dispatch(tmp_path):
    provider = ScriptedProvider([])
    engine, repo = _engine(tmp_path, provider, input_budget=2400)
    context = ContextManager()

    def build_with_required_result(*args, **kwargs):
        history = [*args[3],
                   {"role": "assistant", "content": None, "tool_calls": [{"id": "latest", "type": "function", "function": {"name": "file_read", "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "latest", "content": json.dumps({"status": "succeeded", "data": {"text": "x" * 20000}})}]
        return context.build(*args[:3], history, *args[4:], **kwargs)

    engine.context.build = build_with_required_result
    result = await engine.run(TaskSpec(text="Inspect project", repo_path=str(repo), profile_name=engine.config.default_profile))

    assert len(provider.requests) == 0
    assert result.outcome == "blocked"
    assert result.error_category == "context_overflow"
    assert result.request_dispatched is False
    assert "no model request was sent" in result.summary.lower()
    assert "latest tool result" in result.summary.lower()
    assert "narrow" in result.summary.lower()
    assert "larger-context profile" in result.summary.lower()
    assert result.budget is not None


@pytest.mark.asyncio
async def test_empty_repository_can_create_named_file(tmp_path):
    provider = ScriptedProvider([[
        ModelEvent(kind="tool_call", tool="write_file", arguments={
            "plan": "Add the requested greeting file", "relative_path": "hello.txt",
            "content": "Hello, world!\n", "done": True,
        }, call_id="create"), ModelEvent(kind="completed"),
    ]])
    engine, repo = _engine(tmp_path, provider)
    result = await engine.run(TaskSpec(text="Create hello.txt with a greeting", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert "write_file" in {schema["function"]["name"] for schema in provider.requests[0].tools}
    assert (repo / "hello.txt").read_text() == "Hello, world!\n"
    assert result.changed_files == ("hello.txt",)
    assert result.outcome == "unverified"


@pytest.mark.asyncio
async def test_small_final_action_uses_dynamic_output_cap(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "note.txt"}, call_id="read"),
         ModelEvent(kind="usage", usage={"prompt_tokens": 4300, "completion_tokens": 10}), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Inspected note"}, call_id="finish"),
         ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, tokens=6000, files={"note.txt": "A short note.\n"})
    result = await engine.run(TaskSpec(text="Inspect project", repo_path=str(repo), profile_name=engine.config.default_profile))

    assert len(provider.requests) == 2, result
    assert provider.requests[1].max_output_tokens < 1024
    assert result.summary == "Inspected note"
    assert result.request_dispatched is True
    assert result.budget is not None


@pytest.mark.asyncio
async def test_overflow_recovery_dispatches_with_reduced_cap(tmp_path):
    provider = ScriptedProvider([[ModelEvent(kind="error", error="provider quota exhausted")]])
    engine, repo = _engine(tmp_path, provider)
    profile = engine.config.profiles[engine.config.default_profile].copy()
    profile.update({"context_window": 1487, "max_output_tokens": 512, "input_budget_tokens": 1487})
    engine.config = engine.config.model_copy(update={"profiles": {**engine.config.profiles, engine.config.default_profile: profile}})

    def build_with_tight_window(*args, **kwargs):
        cap = kwargs["output_cap"]
        manifest = ContextManifest(estimated_input_tokens=955, output_cap=cap)
        if cap > 256:
            raise ContextOverflowError("context_overflow", "preferred cap does not fit", manifest)
        return ContextPacket(messages=({"role": "user", "content": "Inspect project"},),
                             max_output_tokens=cap, manifest=manifest, estimated_input_tokens=955)

    engine.context.build = build_with_tight_window
    result = await engine.run(TaskSpec(text="Inspect project", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert len(provider.requests) == 1
    assert provider.requests[0].max_output_tokens == 256
    assert result.error_category == "provider_quota_exhausted"


@pytest.mark.asyncio
async def test_task_token_allowance_drops_optional_memory_before_admission(tmp_path):
    provider = ScriptedProvider([[ModelEvent(kind="error", error="provider quota exhausted")]])
    engine, repo = _engine(tmp_path, provider, tokens=2200)
    context = ContextManager()
    packets = []

    def build_with_memory(*args, **kwargs):
        kwargs["memory"] = "optional repository memory " * 140
        packet = context.build(*args, **kwargs)
        packets.append(packet)
        return packet

    engine.context.build = build_with_memory
    await engine.run(TaskSpec(text="Inspect project", repo_path=str(repo),
                              profile_name=engine.config.default_profile))

    assert len(provider.requests) == 1
    assert packets[0].manifest.omission_reasons["memory"] == "optional_repository_memory"
    assert "optional repository memory" not in json.dumps(provider.requests[0].messages)


@pytest.mark.asyncio
async def test_large_first_read_keeps_second_file_evidence(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "first.txt", "limit": 12000}, call_id="first"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "second.txt"}, call_id="second"), ModelEvent(kind="completed")],
        [ModelEvent(kind="error", error="provider quota exhausted")],
    ])
    engine, repo = _engine(tmp_path, provider, input_budget=4650,
                           files={"first.txt": "A" * 9000, "second.txt": "second-file evidence"},
                           economy_fields={"max_tool_preview_chars": 9000})
    result = await engine.run(TaskSpec(text="Change first.txt and second.txt", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert len(provider.requests) == 3, result
    latest = next(message for message in provider.requests[2].messages if message.get("tool_call_id") == "second")
    assert "second-file evidence" in latest["content"]
    assert not any(message.get("tool_call_id") == "first" for message in provider.requests[2].messages)


@pytest.mark.asyncio
async def test_non_economy_edits_two_files_after_first_patch(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "a.txt"}, call_id="read-a"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="patch_apply", arguments={"edits": [{"path": "a.txt", "expected_hash": digest(b"old a"), "old_text": "old a", "new_text": "new a"}]}, call_id="patch-a"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "b.txt"}, call_id="read-b"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="patch_apply", arguments={"edits": [{"path": "b.txt", "expected_hash": digest(b"old b"), "old_text": "old b", "new_text": "new b"}]}, call_id="patch-b"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Updated both files"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"a.txt": "old a", "b.txt": "old b"},
                           economy_fields={"enabled": False}, allow_commands=True)
    result = await engine.run(TaskSpec(text="Change a.txt and b.txt", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert (repo / "a.txt").read_text() == "new a"
    assert (repo / "b.txt").read_text() == "new b"
    assert len(provider.requests) == 5
    assert "file_read" in {item["function"]["name"] for item in provider.requests[2].tools}
    assert result.changed_files == ("a.txt", "b.txt")


@pytest.mark.asyncio
async def test_failed_verification_returns_to_edit_tools(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="edit_file", arguments={
            "plan": "Apply the first correction", "read_id": "r1", "old_text": "old", "new_text": "mid", "done": True,
        }, call_id="first-edit"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="command_start", arguments={"command": "false"}, call_id="check"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "a.txt"}, call_id="reread"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="edit_file", arguments={
            "plan": "Correct the failed check", "read_id": "r2", "old_text": "mid", "new_text": "new", "done": True,
        }, call_id="second-edit"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Applied both corrections"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"a.txt": "old"}, allow_commands=True,
                           economy_fields={"max_requests": 8})
    result = await engine.run(TaskSpec(text="Fix a.txt", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert (repo / "a.txt").read_text() == "new"
    assert len(provider.requests) == 5, result
    assert "command_start" in {item["function"]["name"] for item in provider.requests[1].tools}
    assert "file_read" in {item["function"]["name"] for item in provider.requests[2].tools}


@pytest.mark.asyncio
async def test_configured_inspection_tier_controls_request_cap(tmp_path):
    provider = ScriptedProvider([[ModelEvent(kind="tool_call", tool="file_read",
                                             arguments={"relative_path": "note.txt"}, call_id="read"),
                                  ModelEvent(kind="completed")],
                                 [ModelEvent(kind="tool_call", tool="finish_request",
                                             arguments={"summary": "Inspected"}, call_id="finish"),
                                  ModelEvent(kind="completed")]])
    engine, repo = _engine(tmp_path, provider, files={"note.txt": "small"},
                           economy_fields={"inspect_output_tokens": 768})
    await engine.run(TaskSpec(text="Inspect project", repo_path=str(repo), profile_name=engine.config.default_profile))
    assert provider.requests[0].max_output_tokens == 768


@pytest.mark.asyncio
async def test_incomplete_edit_keeps_edit_tools_available(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "a.txt"}, call_id="read"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="edit_file", arguments={"plan": "Update first note", "read_id": "r1", "old_text": "old", "new_text": "new", "done": False}, call_id="edit"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "b.txt"}, call_id="next"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "One edit applied"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"a.txt": "old", "b.txt": "other"}, allow_commands=True)
    await engine.run(TaskSpec(text="Change project notes", repo_path=str(repo), profile_name=engine.config.default_profile))
    assert len(provider.requests) >= 3
    assert (repo / "a.txt").read_text() == "new"
    assert "file_read" in {schema["function"]["name"] for schema in provider.requests[2].tools}


@pytest.mark.asyncio
async def test_truncated_tool_call_is_not_executed(tmp_path):
    from ion.workspace import digest

    patch = {"edits": [{"path": "a.txt", "expected_hash": digest(b"old"), "old_text": "old", "new_text": "new"}]}
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="patch_apply", arguments=patch, call_id="partial"),
         ModelEvent(kind="completed", finish_reason="length")],
        [ModelEvent(kind="tool_call", tool="patch_apply", arguments=patch, call_id="complete"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Updated"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"a.txt": "old"})
    result = await engine.run(TaskSpec(text="Fix a.txt", repo_path=str(repo), profile_name=engine.config.default_profile))
    assert len(provider.requests) >= 2
    assert (repo / "a.txt").read_text() == "new"
    assert result.changed_files == ("a.txt",)


@pytest.mark.asyncio
async def test_provider_rate_limit_keeps_its_error_category(tmp_path):
    provider = ScriptedProvider([[ModelEvent(kind="error", error="provider rate limit", retry_after_seconds=31)]])
    engine, repo = _engine(tmp_path, provider)
    result = await engine.run(TaskSpec(text="Inspect project", repo_path=str(repo), profile_name=engine.config.default_profile))
    assert result.outcome == "failed"
    assert result.error_category == "provider_rate_limit"
    assert result.request_dispatched is True
    assert len(provider.requests) == 1


def test_tool_preview_respects_configured_maximum():
    response = Engine._tool_response(ToolResult(operation_id="op", status="succeeded", summary="read",
                                               data={"output": "x" * 1000}), max_chars=256)
    preview = json.loads(response)["data"]["output"]
    assert len(preview) <= 256
    assert "output shortened" in preview


def test_reviewed_preview_config_name_accepts_validated_value():
    config = EconomyConfig(max_tool_preview_chars=256)
    assert config.max_tool_preview_chars == 256
    assert config.tool_preview_max_chars == 256
    with pytest.raises(ValueError):
        EconomyConfig(max_tool_preview_chars=255)


@pytest.mark.asyncio
async def test_file_read_preview_repoints_next_offset(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "long.txt"}, call_id="read"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Read the preview"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"long.txt": "A" * 1000},
                           economy_fields={"max_tool_preview_chars": 256})
    await engine.run(TaskSpec(text="Inspect repository", repo_path=str(repo),
                              profile_name=engine.config.default_profile))

    result_message = next(message for message in provider.requests[1].messages if message.get("tool_call_id") == "read")
    data = json.loads(result_message["content"])["data"]
    assert len(data["text"]) == 256
    assert data["offset"] == 0
    assert data["next_offset"] == 256
    assert data["truncated"] is True
    assert data["fully_read"] is False
    assert "file_read" in data["read_more"]
    assert "Unseen page: offset=256" in provider.requests[1].messages[0]["content"]


@pytest.mark.asyncio
async def test_prefetched_read_preview_does_not_claim_full_rewrite_evidence(tmp_path):
    provider = ScriptedProvider([[ModelEvent(kind="error", error="provider quota exhausted")]])
    engine, repo = _engine(tmp_path, provider, files={"long.txt": "A" * 1000},
                           economy_fields={"max_tool_preview_chars": 256})
    await engine.run(TaskSpec(text="Rewrite long.txt", repo_path=str(repo),
                              profile_name=engine.config.default_profile))

    assert len(provider.requests) == 1
    assert provider.requests[0].tool_choice != "write_file"
    assert "fully_read=False" in provider.requests[0].messages[0]["content"]
    assert "next_offset=256" in provider.requests[0].messages[0]["content"]


@pytest.mark.asyncio
async def test_clipped_read_cannot_authorize_whole_file_rewrite(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="write_file", arguments={
            "plan": "Replace the file", "relative_path": "long.txt", "read_id": "r1",
            "content": "replacement", "done": True,
        }, call_id="write"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Could not rewrite"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"long.txt": "A" * 1021},
                           economy_fields={"max_tool_preview_chars": 256})
    result = await engine.run(TaskSpec(text="Rewrite long.txt", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert (repo / "long.txt").read_text() == "A" * 1021
    assert len(engine.dispatcher.reads["r1"][3]) == 256
    assert any(item.error and "next unread offset=256" in item.error for item in engine.dispatcher.results.values())
    assert not result.changed_files


@pytest.mark.asyncio
async def test_context_omitted_read_cannot_authorize_whole_file_rewrite(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "first.txt", "limit": 12000}, call_id="first"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "second.txt"}, call_id="second"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="write_file", arguments={
            "plan": "Replace the earlier file", "relative_path": "first.txt", "read_id": "r1",
            "content": "replacement", "done": True,
        }, call_id="stale-write"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Could not rewrite"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, input_budget=4650,
                           files={"first.txt": "A" * 9000, "second.txt": "second-file evidence"},
                           economy_fields={"max_tool_preview_chars": 9000})
    result = await engine.run(TaskSpec(text="Change repository notes", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert len(provider.requests) >= 3, result
    assert not any(message.get("tool_call_id") == "first" for message in provider.requests[2].messages)
    assert "second-file evidence" in json.dumps(provider.requests[2].messages)
    assert (repo / "first.txt").read_text() == "A" * 9000
    assert any(item.error and "existing file requires read_id" in item.error
               for item in engine.dispatcher.results.values())
    assert not result.changed_files


@pytest.mark.asyncio
async def test_engine_rewrite_accepts_all_visible_file_pages(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "long.txt", "limit": 4000}, call_id="first-page"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "long.txt", "offset": 4000, "limit": 4000}, call_id="second-page"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="write_file", arguments={
            "plan": "Rewrite both observed pages", "relative_path": "long.txt", "read_id": "r1",
            "content": "replacement", "done": True,
        }, call_id="write"), ModelEvent(kind="completed")],
    ])
    engine, repo = _engine(tmp_path, provider, files={"long.txt": "A" * 4000 + "B" * 2000})
    result = await engine.run(TaskSpec(text="Rewrite the repository note", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert len(provider.requests) == 3, result
    assert {message.get("tool_call_id") for message in provider.requests[2].messages} >= {"first-page", "second-page"}
    assert (repo / "long.txt").read_text() == "replacement"
    assert result.changed_files == ("long.txt",)


@pytest.mark.asyncio
async def test_finalization_uses_reserved_fifth_request_after_four_reads(tmp_path):
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": f"f{i}.txt"}, call_id=f"r{i}"), ModelEvent(kind="completed")]
        for i in range(4)
    ] + [[ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Inspected four files"}, call_id="finish"), ModelEvent(kind="completed")]])
    engine, repo = _engine(tmp_path, provider, files={f"f{i}.txt": f"content {i}" for i in range(4)},
                           economy_fields={"max_requests": 5})
    result = await engine.run(TaskSpec(text="Inspect repository", repo_path=str(repo),
                                       profile_name=engine.config.default_profile))

    assert len(provider.requests) == 5
    assert result.summary == "Inspected four files"
    assert result.error_category is None
    assert {item["function"]["name"] for item in provider.requests[4].tools} == {"diff_summary", "finish_request"}
    assert result.budget.requests_remaining == 0


def test_memory_file_does_not_offer_artifact_tools(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    (store.root / "MEMORY.md").write_text("task pointers")
    assert not store.has_artifacts()
    assert "artifact_read" not in select_tool_bundle(Phase.act, has_artifacts=store.has_artifacts())
    artifact = store.put(b"retained output", "command_output")
    assert store.has_artifacts()
    assert "artifact_read" in select_tool_bundle(Phase.act, has_artifacts=store.has_artifacts())
    assert store.read(artifact.artifact_id) == b"retained output"
