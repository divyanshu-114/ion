"""Opt-in live smoke run against a disposable README copy. Uses provider quota."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from ion.artifacts import ArtifactStore
from ion.config import load_config, resolve_credential, resolve_profile
from ion.contracts import TaskSpec
from ion.diagnostics import DiagnosticLogger, recent_diagnostics
from ion.engine import Engine
from ion.launcher import load_local_env
from ion.processes import CommandSupervisor
from ion.providers.openai_compatible import OpenAICompatibleProvider
from ion.tools.registry import ToolDispatcher
from ion.workspace import Workspace


async def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "ion.toml")
    load_local_env(root / "ion.toml", evaluation=False)
    profile = resolve_profile(config, config.default_profile, "product")
    key, key_name = resolve_credential(profile)
    if not key:
        print(f"Missing {key_name}")
        return 2
    with tempfile.TemporaryDirectory(prefix="ion-smoke-") as name:
        temp = Path(name)
        repo = temp / "repo"
        repo.mkdir()
        shutil.copy2(root / "README.md", repo / "README.md")
        workspace = Workspace.capture(repo)
        artifacts = ArtifactStore(temp / "artifacts")
        dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts, key))
        task = TaskSpec(text="rewrite the readme", repo_path=str(repo), profile_name=config.default_profile)
        log = temp / "diagnostics.jsonl"
        result = await Engine(config, OpenAICompatibleProvider(profile, key), dispatcher,
                              diagnostics=DiagnosticLogger(log, task.task_id)).run(task)
        for entry in recent_diagnostics(log):
            if entry["event"] in {"model.response", "tool.request", "tool.result", "request.retry"}:
                print({key: value for key, value in entry.items() if key not in {"time", "task_id"}})
        print(result.model_dump_json())
        print("Attributable files:", workspace.changes().attributable_files)
        return 0 if "README.md" in workspace.changes().attributable_files and result.outcome == "unverified" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
