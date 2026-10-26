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
async def test_product_coding_loop_exposes_bounded_command_and_verifies(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bug.py").write_text("def value():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_bug.py").write_text(
        "from bug import value\n\n\ndef test_value():\n    assert value() == 2\n"
    )
    config = load_config(Path(__file__).resolve().parents[1] / "ion.toml")
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / ".ion-artifacts")
    dispatcher = ToolDispatcher(
        workspace, artifacts, CommandSupervisor(workspace, artifacts), allow_commands=True
    )
    original = (repo / "bug.py").read_bytes()
    turns = [
        [ModelEvent(kind="tool_call", tool="file_read", arguments={"relative_path": "bug.py"}, call_id="p1"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="edit_file", arguments={"plan": "Fix the return value", "read_id": "r1", "old_text": "return 1", "new_text": "return 2", "done": False}, call_id="p2"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="command_start", arguments={"command": f"{sys.executable} -m pytest tests/test_bug.py -q"}, call_id="p3"), ModelEvent(kind="completed")],
        [ModelEvent(kind="tool_call", tool="finish_request", arguments={"summary": "Fixed value"}, call_id="p4"), ModelEvent(kind="completed")],
    ]
    engine = Engine(config, ScriptedProvider(turns), dispatcher)
    task = TaskSpec(text="Fix value and verify it", repo_path=str(repo), profile_name="groq-qwen-dev", mode="product")
    result = await engine.run(task)
    assert result.outcome.value == "verified"
    assert result.verification_ids
