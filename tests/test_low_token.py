import json
from pathlib import Path

import pytest

from ion.artifacts import ArtifactStore
from ion.config import load_config, resolve_profile
from ion.context import ContextManager
from ion.contracts import ModelEvent, Phase, TaskSpec, ToolCall
from ion.diagnostics import DiagnosticLogger, recent_diagnostics
from ion.engine import Engine
from ion.processes import CommandSupervisor
from ion.providers.scripted import ScriptedProvider
from ion.tools.registry import ToolDispatcher, tool_schemas
from ion.workspace import Workspace, digest


def test_economy_tool_catalog_pushes_model_to_edit_after_two_reads():
    assert [item["function"]["name"] for item in tool_schemas(("edit_file", "finish_request"))] == ["finish_request", "edit_file"]
    assert "edit_file" in {item["function"]["name"] for item in tool_schemas(("file_read", "edit_file", "finish_request"))}


@pytest.mark.asyncio
async def test_economy_read_read_edit_finishes_without_repository_checks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    readme = repo / "README.md"
    readme.write_text("# Project\n\nShort introduction.\n" + "x" * 7000)
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "README.md"}, call_id="r1"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "README.md", "offset": 4000}, call_id="r2"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="edit_file", arguments={
            "plan": "Expand the introduction with a clear product description.",
            "read_id": "r1", "old_text": "Short introduction.",
            "new_text": "A detailed introduction to the project and its main workflow.", "done": True,
        }, call_id="e1"), ModelEvent(kind="completed")],
    ])
    diagnostics = DiagnosticLogger(tmp_path / "ion.jsonl", "task")
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    result = await Engine(config, provider, dispatcher, diagnostics=diagnostics).run(
        TaskSpec(text="Rewrite the README with more detail", repo_path=str(repo), profile_name="openrouter-coding-free")
    )
    third_tools = [item["function"]["name"] for item in provider.requests[2].tools]
    assert {"file_read", "edit_file", "write_file", "finish_request"} <= set(third_tools)
    assert "repo_search" not in third_tools
    third_messages = provider.requests[2].messages
    assert any(message.get("tool_call_id") == "r2" for message in third_messages)
    assert "Short introduction." in json.dumps(third_messages)
    assert result.changed_files == ("README.md",)
    assert result.outcome == "unverified"
    assert len(provider.requests) == 3
    assert "detailed introduction" in readme.read_text()
    events = recent_diagnostics(tmp_path / "ion.jsonl")
    assert any(item["event"] == "tool.result" and item["tool"] == "edit_file" for item in events)
    assert all("old_text" not in json.dumps(item) and "new_text" not in json.dumps(item) for item in events)


def test_large_model_context_still_uses_small_prompt_budget():
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    profile = resolve_profile(config, "openrouter-qwen-free", "product")
    task = TaskSpec(text="Fix parser", repo_path="/tmp/repo", profile_name="fixture")
    history = [{"role": "user", "content": task.text}]
    for i in range(20):
        history.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": str(i), "type": "function", "function": {"name": "file_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": str(i), "content": "x" * 3000},
        ])
    packet = ContextManager().build(task, profile, Phase.act, history, "")
    serialized = json.dumps({"messages": packet.messages, "tools": tool_schemas()}, separators=(",", ":"), ensure_ascii=False)
    assert len(serialized) / 3 <= 6000
    assert packet.messages[-1]["tool_call_id"] == "19"
    assert packet.dropped_turns > 0


@pytest.mark.asyncio
async def test_file_read_pages_keep_full_hash_and_valid_json(tmp_path):
    text = "line\n" * 2500
    (tmp_path / "large.txt").write_text(text)
    workspace = Workspace.capture(tmp_path)
    artifacts = ArtifactStore(tmp_path / ".artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    first = await dispatcher.execute(ToolCall(task_id="t", tool="file_read", arguments={"relative_path": "large.txt"}))
    assert len(first.data["text"]) <= 4000
    assert first.data["sha256"] == digest(text.encode())
    second = await dispatcher.execute(ToolCall(task_id="t", tool="file_read", arguments={"relative_path": "large.txt", "offset": first.data["next_offset"]}))
    assert second.status == "succeeded"
    assert second.data["sha256"] == first.data["sha256"]
    assert first.data["text"] + second.data["text"] == text[:second.data["next_offset"]]


@pytest.mark.asyncio
async def test_root_listing_accepts_dot_and_does_not_expose_private_files(tmp_path):
    (tmp_path / "app.py").write_text("pass\n")
    (tmp_path / ".env").write_text("PRIVATE=fixture\n")
    workspace = Workspace.capture(tmp_path)
    artifacts = ArtifactStore(tmp_path / ".artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    result = await dispatcher.execute(ToolCall(task_id="t", tool="repo_list", arguments={"relative_path": "."}))
    assert result.status == "succeeded"
    assert "app.py" in result.data["paths"]
    assert ".env" not in result.data["paths"]


@pytest.mark.asyncio
async def test_command_tool_is_disabled_by_default(tmp_path):
    workspace = Workspace.capture(tmp_path)
    artifacts = ArtifactStore(tmp_path / ".artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    result = await dispatcher.execute(ToolCall(task_id="t", tool="command_start", arguments={"command": "pwd"}))
    assert result.status == "failed"
    assert result.error == "command execution is disabled for this workspace-locked session"


def test_tool_feedback_keeps_json_intact_when_shortening_output():
    from ion.contracts import ToolResult
    result = ToolResult(operation_id="op", status="succeeded", summary="command", data={"output": '"\\\n' * 8000, "artifact_id": "retained"})
    response = json.loads(Engine._tool_response(result))
    assert response["data"]["artifact_id"] == "retained"
    assert response["data"]["truncated"]
    assert len(response["data"]["output"]) < 4100


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [
    ModelEvent(kind="text_delta", text="I will inspect the file."),
    ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Done"}, call_id="early"),
])
async def test_small_model_is_redirected_to_tools_before_finishing(tmp_path, first):
    (tmp_path / "bug.py").write_text("value = 1\n")
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    workspace = Workspace.capture(tmp_path)
    artifacts = ArtifactStore(tmp_path / ".artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    provider = ScriptedProvider([
        [first, ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "bug.py"}, call_id="read"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Inspected"}, call_id="finish"), ModelEvent(kind="completed")],
    ])
    result = await Engine(config, provider, dispatcher).run(TaskSpec(text="Inspect the project", repo_path=str(tmp_path), profile_name="openrouter-qwen-free"))
    assert len(provider.requests) == 3
    assert result.summary == "Inspected"
    if first.kind == "tool_call":
        assert any(m.get("tool_call_id") == "early" for m in provider.requests[1].messages)
