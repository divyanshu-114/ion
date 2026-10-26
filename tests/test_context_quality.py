import json

import pytest

from ion.context import ContextManager, ContextOverflowError
from ion.contracts import ContextCheckpoint, ModelProfile, Phase, TaskSpec
from ion.tools.registry import tool_schemas


def _task():
    return TaskSpec(text="Fix parser.py", repo_path="/tmp/repo", profile_name="fixture")


def _profile():
    return ModelProfile(provider="fixture", endpoint="https://example.invalid", model_id="fixture",
                        context_window=8000, max_output_tokens=512)


def _read(call_id, body):
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
         "function": {"name": "file_read", "arguments": '{"relative_path":"parser.py"}'}}]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"path": "parser.py", "sha256": "same",
         "read_id": call_id, "text": body})},
    ]


def test_optional_memory_and_duplicate_reads_drop_before_required_context():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("old", "A" * 1200),
               *_read("new", "B" * 1200)]
    names = ("file_read", "finish_request")
    packet = ContextManager().build(task, _profile(), Phase.act, history, "Keep public API stable",
                                    steering=("Do not change dependencies",), memory="hint " * 250,
                                    tools=tool_schemas(names), tool_names=names, input_budget_tokens=1400)

    assert packet.estimated_input_tokens <= 1400
    assert "Fix parser.py" in packet.messages[0]["content"]
    assert "Keep public API stable" in packet.messages[0]["content"]
    assert "Do not change dependencies" in packet.messages[0]["content"]
    assert packet.messages[-2]["tool_calls"][0]["id"] == "new"
    assert packet.messages[-1]["tool_call_id"] == "new"
    assert "B" * 1200 in packet.messages[-1]["content"]
    assert "A" * 1200 not in json.dumps(packet.messages)
    assert "hint hint" not in json.dumps(packet.messages)
    assert packet.manifest.omission_reasons["memory"] == "optional_repository_memory"
    assert packet.manifest.omission_reasons["old"] == "duplicate_read_body"
    assert "old" in packet.manifest.included_turn_ids
    assert not packet.manifest.omitted_turn_ids


def test_required_latest_turn_raises_context_overflow():
    task = _task()
    names = ("file_read", "finish_request")
    history = [{"role": "user", "content": task.text}, *_read("latest", "X" * 9000)]
    payload = json.loads(history[-1]["content"])
    payload.pop("path")
    payload.pop("read_id")
    history[-1]["content"] = json.dumps(payload)
    with pytest.raises(ContextOverflowError) as exc:
        ContextManager().build(task, _profile(), Phase.act,
                               history, "",
                               tools=tool_schemas(names), tool_names=names, input_budget_tokens=1100)

    assert exc.value.code == "latest_turn_overflow"
    assert exc.value.dispatched is False
    assert "latest" in exc.value.manifest.included_turn_ids
    assert exc.value.manifest.estimated_input_tokens > 1100


def test_latest_file_read_body_is_compacted_before_blocking():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("latest", "B" * 4000)]
    packet = ContextManager().build(task, _profile(), Phase.act, history, "",
                                    tool_names=("file_read", "finish_request"), input_budget_tokens=1600)

    encoded = json.dumps(packet.messages)
    assert packet.manifest.omission_reasons["latest"] == "latest_read_body_preview"
    assert "B" * 4000 not in encoded
    assert "read_id" in encoded
    assert "reread by reference" in encoded


def test_latest_non_file_tool_output_is_compacted_before_blocking():
    task = _task()
    history = [{"role": "user", "content": task.text},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "scan", "type": "function",
                "function": {"name": "infra_scan", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "scan",
                "content": json.dumps({"status": "succeeded", "data": {"output": "X" * 9000, "framework": "python"}})}]

    packet = ContextManager().build(task, _profile(), Phase.inspect, history, "",
                                    tool_names=("infra_scan", "finish_request"), input_budget_tokens=1600)

    encoded = json.dumps(packet.messages)
    assert "X" * 9000 not in encoded
    assert "body omitted; reread by reference" in encoded
    assert packet.manifest.omission_reasons["scan"] == "latest_tool_body_preview"
    output = json.loads(packet.messages[-1]["content"])
    assert output["data"]["framework"] == "python"


def test_applied_steering_is_not_repeated_in_history():
    task = _task()
    packet = ContextManager().build(
        task, _profile(), Phase.inspect,
        [{"role": "user", "content": task.text}, {"role": "user", "content": "User steering: Keep the CLI stable"}],
        "", steering=("Keep the CLI stable",), tool_names=("finish_request",), input_budget_tokens=1600,
    )

    encoded = json.dumps(packet.messages)
    assert encoded.count("Keep the CLI stable") == 1
    assert packet.manifest.omission_reasons["turn:1"] == "steering_pinned"


def test_bundle_schema_cost_is_included_in_estimate():
    task = _task()
    history = [{"role": "user", "content": task.text}]
    navigate = ("repo_list", "finish_request")
    verify = ("diff_summary", "diff_inspect", "command_start", "finish_request")
    manager = ContextManager()
    small = manager.build(task, _profile(), Phase.inspect, history, "", tool_names=navigate,
                          input_budget_tokens=1600)
    large = manager.build(task, _profile(), Phase.verify, history, "", tool_names=verify,
                          input_budget_tokens=1600)

    assert small.estimated_input_tokens < large.estimated_input_tokens <= 1600
    assert small.manifest.selected_tool_names == navigate
    assert set(large.manifest.selected_tool_names) == set(verify)
    assert small.manifest.output_cap == large.manifest.output_cap == 512


def test_checkpoint_retains_references_and_covers_old_turns():
    task = _task()
    names = ("file_read", "finish_request")
    history = [{"role": "user", "content": task.text}, *_read("old", "A" * 1800),
               *_read("latest", "B" * 700)]
    history[1]["tool_calls"][0]["function"]["arguments"] = '{"relative_path":"legacy.py"}'
    history[2]["content"] = history[2]["content"].replace("parser.py", "legacy.py")
    history[1]["sequence"] = 1
    history[2]["sequence"] = 2
    history[3]["sequence"] = 3
    history[4]["sequence"] = 4
    checkpoint = ContextCheckpoint(checkpoint_id="cp-1", session_id="session", through_seq=2,
                                   constraints_digest="digest", summary="Parser inspected",
                                   pinned_evidence_refs=("artifact-1",), model_profile_digest="model")
    packet = ContextManager().build(task, _profile(), Phase.act, history, "", checkpoint=checkpoint,
                                    tools=tool_schemas(names), tool_names=names, input_budget_tokens=1250)

    assert packet.manifest.checkpoint_id == "cp-1"
    assert packet.manifest.pinned_evidence_refs == ("artifact-1",)
    assert "artifact-1" in packet.messages[0]["content"]
    assert "1" in packet.manifest.omitted_turn_ids
    assert packet.manifest.omission_reasons["1"] == "checkpoint_covered"
    assert packet.messages[-1]["tool_call_id"] == "latest"


def test_pending_operation_does_not_displace_latest_complete_exchange():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("latest", "B" * 700),
               {"role": "assistant", "content": None, "tool_calls": [{"id": "pending", "type": "function",
                "function": {"name": "edit_file", "arguments": '{"read_id":"latest"}'}}]}]

    with pytest.raises(ContextOverflowError) as exc:
        ContextManager().build(task, _profile(), Phase.act, history, "",
                               tool_names=("file_read", "edit_file", "finish_request"),
                               input_budget_tokens=1100)

    assert "latest" in exc.value.manifest.included_turn_ids
    assert "pending" in exc.value.manifest.included_turn_ids
    assert not exc.value.manifest.omitted_turn_ids


def test_history_steering_survives_old_exchange_removal():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("old", "A" * 2000),
               {"role": "user", "content": "User steering: Keep the CLI stable"},
               *_read("latest", "B" * 700)]
    history[1]["tool_calls"][0]["function"]["arguments"] = '{"relative_path":"legacy.py"}'

    packet = ContextManager().build(task, _profile(), Phase.act, history, "",
                                    tool_names=("file_read", "finish_request"), input_budget_tokens=1200)

    assert "User steering: Keep the CLI stable" in json.dumps(packet.messages)
    assert "old" in packet.manifest.omitted_turn_ids
    assert packet.messages[-1]["tool_call_id"] == "latest"


def test_artifact_preview_keeps_reference_before_omitting_exchange():
    task = _task()
    history = [{"role": "user", "content": task.text},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "command", "type": "function",
                "function": {"name": "command_start", "arguments": '{"command":"pytest -q"}'}}]},
               {"role": "tool", "tool_call_id": "command",
                "content": json.dumps({"artifact_id": "artifact-1", "output": "X" * 2200})},
               *_read("latest", "B" * 500)]

    packet = ContextManager().build(task, _profile(), Phase.verify, history, "",
                                    tool_names=("command_start", "file_read", "finish_request"),
                                    input_budget_tokens=1600)

    assert packet.manifest.omission_reasons["command"] == "artifact_preview"
    assert "command" in packet.manifest.included_turn_ids
    assert not packet.manifest.omitted_turn_ids
    assert "artifact-1" in json.dumps(packet.messages)
    assert "X" * 2200 not in json.dumps(packet.messages)


def test_pinned_task_with_durable_id_is_not_sent_twice():
    task = _task()
    packet = ContextManager().build(task, _profile(), Phase.inspect,
                                    [{"role": "user", "content": task.text, "turn_id": "task-turn"}], "",
                                    tool_names=("finish_request",))

    assert len(packet.messages) == 1
    assert "task-turn" in packet.manifest.included_turn_ids
    assert not packet.manifest.omitted_turn_ids


def test_unmarked_user_constraint_survives_exchange_compaction():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("old", "A" * 2000),
               {"role": "user", "content": "Keep the CLI stable"}, *_read("latest", "B" * 700)]
    history[1]["tool_calls"][0]["function"]["arguments"] = '{"relative_path":"legacy.py"}'

    packet = ContextManager().build(task, _profile(), Phase.act, history, "",
                                    tool_names=("file_read", "finish_request"), input_budget_tokens=1200)

    assert "Keep the CLI stable" in json.dumps(packet.messages)
    assert "old" in packet.manifest.omitted_turn_ids
    assert packet.messages[-1]["tool_call_id"] == "latest"


def test_structured_pending_action_preserves_previous_complete_result():
    task = _task()
    profile = _profile().model_copy(update={"tool_protocol": "structured_json"})
    history = [{"role": "user", "content": task.text},
               {"role": "assistant", "content": json.dumps({"action": "tool", "tool": "file_read",
                "arguments": {"relative_path": "parser.py"}}), "turn_id": "read-action"},
               {"role": "user", "content": "Tool result: " + json.dumps({"read_id": "r1", "text": "B" * 1800}),
                "turn_id": "read-result"},
               {"role": "assistant", "content": json.dumps({"action": "tool", "tool": "edit_file",
                "arguments": {"read_id": "r1", "old_text": "before", "new_text": "after"}}),
                "turn_id": "pending-edit"}]

    with pytest.raises(ContextOverflowError) as exc:
        ContextManager().build(task, profile, Phase.act, history, "",
                               tool_names=("file_read", "edit_file", "finish_request"),
                               input_budget_tokens=1300)

    assert exc.value.dispatched is False
    assert {"read-action", "read-result", "pending-edit"} <= set(exc.value.manifest.included_turn_ids)
    assert not exc.value.manifest.omitted_turn_ids


def test_checkpoint_without_durable_sequences_does_not_claim_coverage():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("old", "A" * 1800),
               *_read("latest", "B" * 700)]
    history[1]["tool_calls"][0]["function"]["arguments"] = '{"relative_path":"legacy.py"}'
    checkpoint = ContextCheckpoint(checkpoint_id="cp-1", session_id="session", through_seq=100,
                                   constraints_digest="digest", summary="Earlier work inspected",
                                   model_profile_digest="model")

    packet = ContextManager().build(task, _profile(), Phase.act, history, "", checkpoint=checkpoint,
                                    tool_names=("file_read", "finish_request"), input_budget_tokens=1250)

    assert "old" in packet.manifest.omitted_turn_ids
    assert packet.manifest.omission_reasons["old"] == "old_completed_turn"


def test_small_repository_memory_drops_before_completed_turns_and_keeps_task_pointer():
    task = _task()
    history = [{"role": "user", "content": task.text}, *_read("old", "A" * 800),
               *_read("latest", "B" * 700)]
    history[1]["tool_calls"][0]["function"]["arguments"] = '{"relative_path":"legacy.py"}'
    pointer = '{"path":"parser.py","status":"read"}'
    packet = ContextManager().build(task, _profile(), Phase.act, history, "",
                                    memory="repository fact " * 12, task_pointers=pointer,
                                    tool_names=("file_read", "finish_request"), input_budget_tokens=1430)

    assert packet.manifest.omission_reasons["memory"] == "optional_repository_memory"
    assert "repository fact" not in json.dumps(packet.messages)
    assert "parser.py" in json.dumps(packet.messages)
    assert "old" not in packet.manifest.omitted_turn_ids
