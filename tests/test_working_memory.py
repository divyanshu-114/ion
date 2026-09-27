import json

import pytest

from ion.artifacts import ArtifactStore
from ion.contracts import ModelProfile, Phase, TaskSpec, ToolCall, ToolResult
from ion.context import ContextManager
from ion.working_memory import WorkingMemory, LoopGuard
from ion.workspace import Workspace, digest


def test_memory_keeps_pointers_not_file_content_and_invalidates_stale_reads(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n")
    memory = WorkingMemory(Workspace.capture(repo), ArtifactStore(tmp_path / "artifacts"))
    call = ToolCall(task_id="task", tool="file_read", arguments={"relative_path": "app.py"})
    memory.observe(call, ToolResult(operation_id=call.operation_id, status="succeeded", summary="read", data={"path": "app.py", "sha256": digest(b"value = 1\n"), "text": "PRIVATE_CONTENT", "next_offset": None}))
    assert "app.py" in memory.render()
    assert "PRIVATE_CONTENT" not in memory.render()
    assert (tmp_path / "artifacts/MEMORY.md").exists()
    assert not (repo / "MEMORY.md").exists()
    (repo / "app.py").write_text("value = 2\n")
    assert '"stale":true' in memory.render()


def test_compaction_retains_bounded_memory_and_rules(tmp_path):
    task = TaskSpec(text="Fix the bug", repo_path=str(tmp_path), profile_name="test")
    profile = ModelProfile(provider="test", endpoint="https://example.com", model_id="test", context_window=32000, max_output_tokens=1000)
    history = [{"role": "user", "content": task.text}]
    for i in range(12):
        history += [
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"read-{i}", "type": "function",
             "function": {"name": "file_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"read-{i}", "content": "x" * 4000},
        ]
    packet = ContextManager().build(task, profile, Phase.act, history, "Never modify generated files", memory='{"path":"parser.py","status":"read"}')
    assert packet.dropped_turns > 0
    assert "parser.py" in json.dumps(packet.messages)
    assert "Never modify generated files" in packet.messages[0]["content"]
    assert packet.estimated_input_tokens <= 6000


@pytest.mark.parametrize("cycle", [["a"], ["a", "b"]])
def test_loop_guard_warns_on_third_cycle_and_stops_fourth(cycle):
    guard = LoopGuard()
    outcomes = []
    for _ in range(4):
        for key in cycle:
            outcomes.append(guard.observe(key, "same-workspace"))
    assert outcomes[len(cycle) * 3 - 1] == "warn"
    assert outcomes[-1] == "stop"
    assert guard.observe("a", "changed-workspace") is None


@pytest.mark.asyncio
async def test_engine_stops_repeated_reads_without_spending_full_request_budget(tmp_path):
    from pathlib import Path
    from ion.config import load_config
    from ion.contracts import ModelEvent
    from ion.engine import Engine
    from ion.providers.scripted import ScriptedProvider
    from ion.processes import CommandSupervisor
    from ion.tools.registry import ToolDispatcher

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n")
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    provider = ScriptedProvider([
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "app.py"}, call_id=str(i)), ModelEvent(kind="completed")]
        for i in range(10)
    ])
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    result = await Engine(config, provider, dispatcher).run(TaskSpec(text="Fix app.py", repo_path=str(repo), profile_name="openrouter-qwen-free"))
    assert result.outcome == "blocked"
    assert "repeated tool cycle" in result.summary
    assert len(provider.requests) == 4
    assert len(dispatcher.results) == 3  # One local prefetch and two model-requested reads.
    for request in provider.requests:
        ids = {call["id"] for message in request.messages for call in message.get("tool_calls", [])}
        assert all(message["tool_call_id"] in ids for message in request.messages if message["role"] == "tool")
