import json

import pytest

from ion.contracts import ModelEvent, TaskSpec
from ion.providers.scripted import ScriptedProvider
from test_budget_engine import _engine


def turn(tool, arguments, index, protocol):
    if protocol == 'native':
        event = ModelEvent(kind='tool_call', tool=tool, arguments=arguments, call_id=str(index))
    else:
        event = ModelEvent(kind='text_delta', text=json.dumps({'action': 'tool', 'tool': tool, 'arguments': arguments}))
    return [event, ModelEvent(kind='completed')]


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['native', 'structured_json'])
async def test_navigation_recovers_after_edit_phase_transition(tmp_path, protocol):
    actions = [('file_read', {'relative_path': 'note.txt'}),
               ('repo_list', {'relative_path': '.'}),
               ('repo_list', {'relative_path': '.'}),
               ('write_file', {'plan': 'Create requested file', 'relative_path': 'test/auth.py', 'content': 'def authenticate():\n    return False\n', 'done': True})]
    provider = ScriptedProvider([turn(tool, args, i, protocol) for i, (tool, args) in enumerate(actions)])
    engine, repo = _engine(tmp_path, provider, files={'note.txt': 'Repository context'})
    profile = engine.config.profiles[engine.config.default_profile].copy()
    profile['tool_protocol'] = protocol
    engine.config = engine.config.model_copy(update={'profiles': {engine.config.default_profile: profile}})
    result = await engine.run(TaskSpec(text='Make a folder and add an auth file with auth logic', repo_path=str(repo), profile_name=engine.config.default_profile))
    assert (repo / 'test/auth.py').exists(), result
    if protocol == 'native':
        assert 'repo_list' in {t['function']['name'] for t in provider.requests[1].tools}
    else:
        assert 'repo_list' in str(provider.requests[1].messages)
        assert 'phase_hidden' not in str(provider.requests[2].messages)
        assert 'repo_list' in str(provider.requests[2].messages)


@pytest.mark.asyncio
async def test_unknown_tool_retry_budget_resets_after_success(tmp_path):
    actions = [('file_read', {'relative_path': 'one.txt'}), ('imaginary', {}),
               ('file_read', {'relative_path': 'two.txt'}), ('other_imaginary', {}),
               ('finish_request', {'summary': 'Read both files'})]
    provider = ScriptedProvider([turn(tool, args, i, 'native') for i, (tool, args) in enumerate(actions)])
    engine, repo = _engine(tmp_path, provider, files={'one.txt': 'one', 'two.txt': 'two'})
    result = await engine.run(TaskSpec(text='Explain the repository', repo_path=str(repo), profile_name=engine.config.default_profile))
    assert result.summary == 'Read both files', result
    correction = str(provider.requests[2].messages)
    assert 'available_tools' in correction


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked_tool', ['write_file', 'command_start', 'run_linter'])
async def test_recovery_never_enables_writes_for_questions(tmp_path, blocked_tool):
    actions = [('file_read', {'relative_path': 'note.txt'}), (blocked_tool, {'plan': 'wrong', 'relative_path': 'bad.py', 'content': 'bad', 'done': True}), ('finish_request', {'summary': 'Read only answer'})]
    provider = ScriptedProvider([turn(tool, args, i, 'native') for i, (tool, args) in enumerate(actions)])
    engine, repo = _engine(tmp_path, provider, files={'note.txt': 'Context'}, allow_commands=True)
    result = await engine.run(TaskSpec(text='Explain this repository', repo_path=str(repo), profile_name=engine.config.default_profile))
    assert result.summary == 'Read only answer'
    assert not (repo / 'bad.py').exists()
    assert all(blocked_tool not in {t['function']['name'] for t in request.tools} for request in provider.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['imaginary', 'command_start', 'edit_file', 'artifact_read'])
async def test_forbidden_unknown_or_unready_tools_stop_after_bounded_corrections(tmp_path, name):
    provider = ScriptedProvider([turn(name, {}, i, 'native') for i in range(4)])
    engine, repo = _engine(tmp_path, provider, files={'note.txt': 'Context'})
    result = await engine.run(TaskSpec(text='Improve repository code', repo_path=str(repo), profile_name=engine.config.default_profile))
    assert result.error_category == 'unavailable_tool'
    assert len(provider.requests) == 3
    assert not engine.dispatcher.results  # None reached execution.
    assert all(name not in {t['function']['name'] for t in request.tools} for request in provider.requests)


@pytest.mark.asyncio
async def test_rejected_native_batch_has_a_result_for_every_call(tmp_path):
    batch = [ModelEvent(kind='tool_call', tool='missing_a', arguments={}, call_id='a'),
             ModelEvent(kind='tool_call', tool='missing_b', arguments={}, call_id='b'),
             ModelEvent(kind='tool_call', tool='file_read', arguments={'relative_path': 'note.txt'}, call_id='c'),
             ModelEvent(kind='completed')]
    provider = ScriptedProvider([batch, turn('finish_request', {'summary': 'Read context'}, 2, 'native')])
    engine, repo = _engine(tmp_path, provider, files={'note.txt': 'Context'})
    result = await engine.run(TaskSpec(text='Explain repository', repo_path=str(repo), profile_name=engine.config.default_profile))
    assert result.summary == 'Read context'
    messages = provider.requests[1].messages
    assert {message['tool_call_id'] for message in messages if message['role'] == 'tool'} == {'a', 'b', 'c'}


def test_recovery_eligibility_keeps_permission_and_evidence_gates():
    from ion.tools.bundles import eligible_tools
    base = dict(edit_intent=True, observed_page_count=0, has_artifacts=False, target_hashes_available=False, allow_commands=False)
    names = eligible_tools(**base)
    assert {'repo_list', 'repo_search', 'infra_scan', 'web_search', 'diff_inspect', 'write_file'} <= names
    assert not {'command_start', 'run_linter', 'edit_file', 'delete_file', 'patch_apply', 'artifact_read'} & names
    ready = eligible_tools(**{**base, 'observed_page_count': 1, 'has_artifacts': True, 'target_hashes_available': True, 'allow_commands': True})
    assert {'command_start', 'run_linter', 'edit_file', 'patch_apply', 'artifact_read'} <= ready
    delete_ready = eligible_tools(**{**base, 'observed_page_count': 1, 'has_artifacts': True, 'target_hashes_available': True, 'allow_commands': True, 'delete_intent': True})
    assert {'command_start', 'run_linter', 'edit_file', 'delete_file', 'patch_apply', 'artifact_read'} <= delete_ready
    assert not {'edit_file', 'write_file', 'patch_apply'} & eligible_tools(**{**base, 'edit_intent': False})
