from pathlib import Path
import sys

import pytest

from ion.artifacts import ArtifactStore
from ion.config import load_config
from ion.contracts import ModelEvent, TaskSpec
from ion.engine import Engine
from ion.processes import CommandSupervisor
from ion.providers.scripted import ScriptedProvider
from ion.tools.registry import ToolDispatcher
from ion.workspace import Workspace, digest


@pytest.mark.asyncio
async def test_scripted_coding_loop_changes_file_and_verifies_relevant_check(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bug.py").write_text("def value():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_bug.py").write_text("from bug import value\n\ndef test_value():\n    assert value() == 2\n")
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    config = config.model_copy(update={"economy": config.economy.model_copy(update={"enabled": False})})
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / ".ion-artifacts")
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts), allow_commands=True)
    original = b"def value():\n    return 1\n"
    turns = [
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "bug.py"}, call_id="c1"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="patch_apply", arguments={"edits": [{"path": "bug.py", "expected_hash": digest(original), "old_text": "return 1", "new_text": "return 2"}]}, call_id="c2"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="command_start", arguments={"command": f"{sys.executable} -m pytest tests/test_bug.py -q"}, call_id="c3"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Fixed value"}, call_id="c4"), ModelEvent(kind="completed")],
    ]
    provider = ScriptedProvider(turns)
    engine = Engine(config, provider, dispatcher)
    task = TaskSpec(text="Make value return 2", repo_path=str(repo), profile_name="groq-qwen-dev")
    await engine.steer("Do not change the test file")
    result = await engine.run(task)
    assert "Do not change the test file" in provider.requests[0].messages[0]["content"]
    assert provider.requests[1].max_output_tokens == 4096
    assert (repo / "bug.py").read_text().endswith("return 2\n")
    assert result.outcome.value == "verified"
    assert result.patch_artifact_id
    assert b"+    return 2" in artifacts.read(result.patch_artifact_id)
