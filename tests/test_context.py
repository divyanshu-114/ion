from ion.context import ContextManager
from ion.contracts import ModelProfile, Phase, TaskSpec
from ion.tools.registry import tool_schemas


def test_context_trim_keeps_task_and_complete_latest_tool_turn():
    profile = ModelProfile(provider="fixture", endpoint="https://example.com/v1", model_id="fixture", context_window=2200, max_output_tokens=200)
    task = TaskSpec(text="Change the parser and keep the API stable", repo_path="/tmp/repo", profile_name="fixture")
    history = [{"role": "user", "content": task.text}]
    for index in range(4):
        history += [
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"c{index}", "type": "function", "function": {"name": "file_read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{index}", "content": "x" * 1300},
        ]
    names = ("file_read", "finish_request")
    packet = ContextManager().build(task, profile, Phase.act, history, "", ("Do not change dependencies",),
                                    tools=tool_schemas(names), tool_names=names)
    assert packet.dropped_turns > 0
    assert "keep the API stable" in packet.messages[0]["content"]
    assert "Do not change dependencies" in packet.messages[0]["content"]
    assert packet.messages[-2]["tool_calls"][0]["id"] == "c3"
    assert packet.messages[-1]["tool_call_id"] == "c3"
    call_ids = {call["id"] for message in packet.messages if message["role"] == "assistant" for call in message.get("tool_calls", [])}
    assert all(message["tool_call_id"] in call_ids for message in packet.messages if message["role"] == "tool")
