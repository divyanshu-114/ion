import json
from pathlib import Path

import pytest

from ion.artifacts import ArtifactStore
from ion.config import load_config
from ion.contracts import ModelEvent, TaskSpec
from ion.engine import Engine
from ion.processes import CommandSupervisor
from ion.providers.scripted import ScriptedProvider
from ion.tools.registry import ToolDispatcher
from ion.workspace import Workspace


async def run_reads(tmp_path, arguments):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'README.md').write_text('a' * 4000 + 'b' * 4000)
    workspace = Workspace.capture(repo)
    artifacts = ArtifactStore(tmp_path / 'artifacts')
    dispatcher = ToolDispatcher(workspace, artifacts, CommandSupervisor(workspace, artifacts))
    turns = [[ModelEvent(kind='tool_call', tool='file_read', arguments=args, call_id=str(i)), ModelEvent(kind='completed')] for i, args in enumerate(arguments)]
    turns.append([ModelEvent(kind='tool_call', tool='finish_request', arguments={'summary': 'Inspected'}, call_id='finish'), ModelEvent(kind='completed')])
    provider = ScriptedProvider(turns)
    config = load_config(Path(__file__).resolve().parents[1] / 'ion.toml')
    result = await Engine(config, provider, dispatcher).run(TaskSpec(text='Inspect the project', repo_path=str(repo), profile_name='openrouter-coding-free'))
    return provider, result


@pytest.mark.asyncio
async def test_equivalent_reads_stop_without_identical_tool_sequences(tmp_path):
    variants = [{'relative_path': 'README.md'}, {'relative_path': './README.md'}, {'relative_path': 'README.md', 'offset': 0}]
    provider, result = await run_reads(tmp_path, variants * 3)
    assert len(provider.requests) == 5
    assert result.outcome == 'blocked'
    assert 'reread README.md at offset 0' in result.summary
    assert 'offset=4000' in provider.requests[1].messages[0]['content']
    # Each old copy is reduced to metadata; the latest page remains available.
    bodies = [json.loads(m['content']).get('data', {}).get('text') for m in provider.requests[-1].messages if m['role'] == 'tool']
    assert sum(text is not None for text in bodies) == 1


@pytest.mark.asyncio
async def test_distinct_pages_remain_available_for_patch_context(tmp_path):
    provider, result = await run_reads(tmp_path, [
        {'relative_path': 'README.md', 'limit': 4000},
        {'relative_path': 'README.md', 'offset': 4000, 'limit': 4000},
    ])
    bodies = [json.loads(m['content']).get('data', {}).get('text') for m in provider.requests[-1].messages if m['role'] == 'tool']
    assert 'a' * 4000 in bodies
    assert 'b' * 4000 in bodies
    assert result.summary == 'Inspected'
