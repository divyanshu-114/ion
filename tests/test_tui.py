from pathlib import Path
import asyncio

import pytest
from textual.widgets import Button, Input, OptionList, RichLog, Static

from ion.config import load_config
from ion.contracts import ModelEvent
from ion.providers.scripted import ScriptedProvider
from ion.tui.app import IonApp
from ion.tui.widgets import Brand, Composer, EntryDialog, Picker


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('ION_DATA_DIR', str(tmp_path / 'ion-data'))
    for name in ('GROQ_API_KEY', 'OPENROUTER_API_KEY', 'AI_API_KEY', 'DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    return IonApp(load_config(Path(__file__).resolve().parents[1] / 'ion.toml'), str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [(120, 40), (80, 24)])
async def test_home_composer_and_searchable_model_dialog(app, size):
    async with app.run_test(size=size) as pilot:
        assert app.query_one(Brand).render().plain.strip()
        composer = app.query_one(Composer)
        assert composer.has_focus
        assert composer.region.bottom <= size[1]
        assert not app.query_one('#cancel', Button).display
        await app.show_models()
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert app.screen.query_one(OptionList).option_count == len(app.config.profiles)
        app.screen.query_one(Input).value = 'groq'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert app.profile_name == 'groq-qwen-dev'
        assert not isinstance(app.screen, Picker)


@pytest.mark.asyncio
async def test_commands_repo_and_session_only_masked_connection(app, monkeypatch, tmp_path):
    async with app.run_test() as pilot:
        app.query_one(Composer).text = '/help'
        await pilot.press('enter')
        assert isinstance(app.screen, Picker)
        await pilot.press('escape')
        original_repo = app.repo_path
        await app._command('/repo')
        await pilot.pause()
        assert app.repo_path == original_repo
        assert 'locked' in str(app.query_one('#status', Static).render()).lower()
        await app._command('/connect')
        await pilot.pause()
        app.screen.query_one(Input).value = 'openrouter'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert app.screen.query_one(Input).password
        monkeypatch.setenv('OPENROUTER_API_KEY', '')
        app.screen.query_one(Input).value = 'fixture-session-key'
        await pilot.press('enter')
        await pilot.pause()
        import os
        assert os.environ['OPENROUTER_API_KEY'] == 'fixture-session-key'
        assert not (tmp_path / '.env').exists()


@pytest.mark.asyncio
async def test_enter_runs_engine_and_new_returns_to_home(app, monkeypatch, tmp_path):
    app.profile_name = "openrouter-coding-free"
    (tmp_path / 'readme.txt').write_text('Hello\n')
    provider = ScriptedProvider([
        [ModelEvent(kind='tool_call', tool='file_read', arguments={'relative_path': 'readme.txt'}, call_id='r'), ModelEvent(kind='completed')],
        [ModelEvent(kind='tool_call', tool='finish_request', arguments={'summary': 'Read the file'}, call_id='f'), ModelEvent(kind='completed')],
    ])
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-key')
    monkeypatch.setattr('ion.tui.app.OpenAICompatibleProvider', lambda *args: provider)
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one(Composer).text = 'Read readme.txt'
        await pilot.press('enter')
        await pilot.pause()
        assert app.running_task is not None
        await app.running_task
        assert app.screen.has_class('session')
        assert app.query_one('#activity', RichLog).lines
        assert 'Read the file' in str(app.query_one('#status', Static).render())
        assert app.query_one('#sidebar').display
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert not app.query_one('#sidebar').display
        log = app.query_one(RichLog)
        assert log.virtual_size.width <= log.scrollable_content_region.width
        app.query_one(Composer).text = '/new'
        await pilot.press('enter')
        assert not app.screen.has_class('session')
        assert app.query_one(Composer).has_focus


@pytest.mark.asyncio
async def test_slash_help_lists_commands_and_ctrl_q_no_longer_exits(app):
    async with app.run_test() as pilot:
        await pilot.press('ctrl+q')
        app.query_one(Composer).text = '/help'
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        labels = ' '.join(label for _, label in app.screen.items)
        assert '/stop' in labels
        assert '/providers' in labels
        assert '/quit' in labels


@pytest.mark.asyncio
async def test_singular_model_command_opens_picker(app):
    async with app.run_test() as pilot:
        app.query_one(Composer).text = '/model'
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, Picker)


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [(120, 40), (80, 24)])
async def test_slash_suggestions_filter_complete_and_open_models(app, size):
    async with app.run_test(size=size) as pilot:
        composer = app.query_one(Composer)
        await pilot.press('/', 'm', 'o')
        choices = app.query_one('#command-suggestions', OptionList)
        assert choices.display
        assert choices.get_option_at_index(0).id == '/models'
        assert composer.has_focus
        assert choices.region.bottom <= size[1]
        assert composer.region.bottom <= size[1]
        await pilot.press('tab')
        assert composer.text == '/models'
        assert composer.cursor_location == (0, 7)
        assert not choices.display
        assert composer.has_focus
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert app.screen.heading == 'Models'


@pytest.mark.asyncio
async def test_slash_suggestions_navigate_select_and_dismiss(app):
    async with app.run_test() as pilot:
        composer = app.query_one(Composer)
        await pilot.press('/')
        choices = app.query_one('#command-suggestions', OptionList)
        assert choices.display
        commands = {choices.get_option_at_index(i).id for i in range(choices.option_count)}
        assert {'/models', '/doctor', '/history', '/help', '/github URL'} <= commands
        await pilot.press('down')
        assert choices.highlighted == 1
        await pilot.press('up')
        assert choices.highlighted == 0
        await pilot.press('escape')
        assert composer.text == '/'
        assert not choices.display
        await pilot.press('g', 'i', 'enter')
        assert composer.text == '/github '
        assert composer.cursor_location == (0, 8)
        assert not choices.display
        assert composer.has_focus
        assert not app._busy()


@pytest.mark.asyncio
async def test_slash_suggestions_enter_executes_and_arguments_hide_menu(app):
    async with app.run_test() as pilot:
        composer = app.query_one(Composer)
        await pilot.press('/', 'r', 'e', 'p', 'enter')
        assert composer.text == ''
        assert 'Workspace locked' in str(app.query_one('#status', Static).render())
        choices = app.query_one('#command-suggestions', OptionList)
        for text in ('Write /models docs', '/unknown', '/steer keep going', '/models\nmore'):
            composer.text = text
            await pilot.pause()
            assert not choices.display
        composer.text = 'first\nsecond'
        composer.cursor_location = (1, 3)
        await pilot.press('up')
        assert composer.cursor_location == (0, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize('session', [False, True])
async def test_slash_suggestions_fit_small_terminal_and_support_mouse(app, session):
    async with app.run_test(size=(80, 24)) as pilot:
        if session:
            app._session('Existing task')
        composer = app.query_one(Composer)
        await pilot.press('/')
        choices = app.query_one('#command-suggestions', OptionList)
        assert choices.region.y >= 0
        assert choices.region.bottom <= composer.region.y
        assert composer.region.bottom <= 24
        await pilot.press('g', 'i')
        await pilot.click('#command-suggestions', offset=(2, 1))
        await pilot.pause()
        assert composer.text == '/github '
        assert composer.has_focus
        await pilot.press('h', 't', 't', 'p')
        assert composer.text == '/github http'
        assert not choices.display


@pytest.mark.asyncio
async def test_logs_command_shows_diagnostic_location(app):
    async with app.run_test() as pilot:
        await app._command('/logs')
        await pilot.pause()
        assert app.screen.has_class('session')
        assert 'Activity logs' in str(app.query_one('#session-header', Static).render())
        assert app.query_one('#activity', RichLog).lines


@pytest.mark.asyncio
@pytest.mark.parametrize('dialog', [False, True])
async def test_ctrl_c_exits_even_in_dialog(app, dialog):
    async with app.run_test() as pilot:
        if dialog:
            await app._command('/help')
        await pilot.press('ctrl+c')
        assert app._exit


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/stop', '/quit', 'ctrl+c'])
async def test_stop_and_exit_cancel_active_work(app, command):
    started, cleaned_up = asyncio.Event(), asyncio.Event()

    async def work():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    async with app.run_test() as pilot:
        app.running_task = asyncio.create_task(work())
        await started.wait()
        if command == 'ctrl+c':
            await pilot.press(command)
        else:
            await app._command(command)
        await pilot.pause()
        assert cleaned_up.is_set()
        assert app.running_task.cancelled()
        assert app._exit == (command != '/stop')


@pytest.mark.asyncio
async def test_provider_keys_are_masked_and_secret_entry_uses_asterisks(app, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-secret-key')
    async with app.run_test() as pilot:
        await app._command('/providers')
        await pilot.pause()
        transcript = '\n'.join(item.plain for item in app._transcript)
        assert 'OPENROUTER_API_KEY=***' in transcript
        assert 'fixture-secret-key' not in transcript
        app.push_screen(EntryDialog('Key', secret=True))
        await pilot.pause()
        entry = app.screen.query_one(Input)
        entry.value = 'fixture-secret-key'
        await pilot.pause()
        rendered = entry.render_line(0).text
        assert '***' in rendered
        assert 'fixture-secret-key' not in rendered


@pytest.mark.asyncio
async def test_logs_show_readable_events_and_mask_credentials(app, monkeypatch):
    from ion.diagnostics import DiagnosticLogger

    monkeypatch.setenv('AI_API_KEY', 'fixture-secret-key')
    logger = DiagnosticLogger(app._data_root() / 'logs' / 'ion.jsonl', 'test-run')
    logger.emit('tool.request', tool='file_read', path='src/main.py', offset=0)
    logger.emit('model.response', request=2, error='Rejected fixture-secret-key')
    async with app.run_test() as pilot:
        await app._command('/logs')
        await pilot.pause()
        transcript = '\n'.join(item.plain for item in app._transcript)
        assert 'Reading file' in transcript and 'src/main.py' in transcript
        assert 'Rejected ***' in transcript
        assert 'fixture-secret-key' not in transcript
        assert 'tool.request' not in transcript
        assert '{"' not in transcript


@pytest.mark.asyncio
async def test_github_command_imports_context_and_rejects_duplicate(app, monkeypatch, issue_checkout):
    app.profile_name = "openrouter-coding-free"
    started = asyncio.Event()
    release = asyncio.Event()
    submitted = []
    async def fetch_issue(url, root):
        started.set()
        await release.wait()
        return '# Issue evidence\nFix startup crash'
    async def run_task(text, *, context_markdown=None, workspace_root=None):
        submitted.append((text, context_markdown))
    monkeypatch.setattr('ion.tools.github.import_issue', fetch_issue)
    monkeypatch.setattr(app, '_run_task', run_task)
    async with app.run_test() as pilot:
        await app._command('/github https://github.com/acme/repo/issues/1')
        await started.wait()
        original = app.running_task
        await app._command('/github https://github.com/acme/repo/issues/2')
        assert app.running_task is original
        release.set()
        await original
        assert len(submitted) == 1
        assert '/issues/1' in submitted[0][0]
        assert 'Fix startup crash' in submitted[0][1]
        assert not app.query_one('#cancel', Button).display


@pytest.mark.asyncio
async def test_github_stop_cancels_fetch_without_starting_agent(app, monkeypatch, issue_checkout):
    app.profile_name = "openrouter-coding-free"
    started = asyncio.Event()
    async def fetch_issue(*args):
        started.set()
        await asyncio.Event().wait()
    async def run_task(*args, **kwargs):
        pytest.fail('cancelled import must not start agent')
    monkeypatch.setattr('ion.tools.github.import_issue', fetch_issue)
    monkeypatch.setattr(app, '_run_task', run_task)
    async with app.run_test():
        await app._command('/github https://github.com/acme/repo/issues/1')
        await started.wait()
        await app.action_cancel()
        await app.running_task
        assert not app._busy()
        assert not app.query_one('#cancel', Button).display


@pytest.mark.asyncio
async def test_github_context_retained_and_available_in_initial_request(app, monkeypatch, tmp_path, issue_checkout):
    app.profile_name = "openrouter-coding-free"
    provider = ScriptedProvider([[ModelEvent(kind='error', error='provider quota exhausted')]])
    markdown = '# Issue\n' + 'Evidence ' * 900
    async def fetch_issue(*args):
        app.query_one(Composer).text = 'Draft typed while preparing'
        return markdown
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-key')
    monkeypatch.setattr('ion.tui.app.OpenAICompatibleProvider', lambda *args: provider)
    monkeypatch.setattr('ion.tools.github.import_issue', fetch_issue)
    async with app.run_test():
        app.query_one(Composer).text = 'unrelated draft'
        await app._command('/github https://github.com/acme/repo/issues/1')
        await app.running_task
        assert provider.requests
        request = provider.requests[0]
        assert 'artifact_read' in {schema['function']['name'] for schema in request.tools}
        prompt = str(request.messages)
        assert 'Investigate and fix the issue' in prompt
        assert 'unrelated draft' not in prompt
        assert app.query_one(Composer).text == 'Draft typed while preparing'
        assert markdown not in prompt
        artifact_files = [path for path in (tmp_path / 'ion-data/artifacts').glob('*/*') if path.is_file()]
        assert any(path.read_text() == markdown for path in artifact_files)


@pytest.mark.asyncio
async def test_fresh_launch_requires_selection_before_network_or_task(app, monkeypatch):
    def unexpected_catalog(*args):
        pytest.fail('No provider requests before model selection')

    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-key')
    monkeypatch.setattr(app, '_new_catalog', unexpected_catalog)
    async with app.run_test() as pilot:
        assert app.profile_name is None
        assert 'No model selected' in str(app.query_one('#profile', Static).render())
        app.query_one(Composer).text = 'Explain this repository'
        await pilot.press('enter')
        await pilot.pause()
        assert app.engine is None
        assert app.store.recent() == []
        assert app.query_one(Composer).text == 'Explain this repository'
        assert 'Select a model' in str(app.query_one('#status', Static).render())
        await app._command('/doctor')
        assert 'Select a model' in str(app.query_one('#status', Static).render())
        await app._command('/providers')
        assert not any(' · active' in str(item) for item in app._transcript)
        await app.show_models()
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert not any('●' in label for _, label in app.screen.items)
        await pilot.press('escape')
        assert app.profile_name is None


@pytest.mark.asyncio
async def test_github_requires_model_before_fetching_issue(app, monkeypatch):
    async def unexpected_fetch(*args, **kwargs):
        pytest.fail('No issue fetch before model selection')

    monkeypatch.setattr('ion.tools.github.import_issue', unexpected_fetch)
    async with app.run_test():
        await app._command('/github https://github.com/acme/repo/issues/1')
        assert app.running_task is None
        assert 'Select a model' in str(app.query_one('#status', Static).render())


@pytest.mark.asyncio
async def test_evaluation_starts_with_configured_model_and_blocks_selection(app):
    from ion.config import apply_environment

    config = apply_environment(app.config.model_copy(update={"evaluation_profiles": ()}), force_evaluation=True)
    evaluation_app = IonApp(config, app.repo_path)
    async with evaluation_app.run_test():
        assert evaluation_app.profile_name == config.evaluation_profile
        assert evaluation_app._profile().locked
        assert 'No model selected' not in str(evaluation_app.query_one('#profile', Static).render())
        await evaluation_app.show_models()
        assert not isinstance(evaluation_app.screen, Picker)
        assert 'locked' in str(evaluation_app.query_one('#status', Static).render())


@pytest.mark.asyncio
async def test_deepseek_connection_uses_entered_key_without_env_file(app, monkeypatch, tmp_path):
    import httpx
    import os
    from ion.models.catalog import ModelCatalog

    env_file = tmp_path / '.env'
    env_file.write_text('')
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == 'https://api.deepseek.com/models'
        assert request.headers['Authorization'] == 'Bearer fixture-entered-key'
        return httpx.Response(200, json={'data': [{'id': 'deepseek-flash', 'object': 'model', 'owned_by': 'deepseek'}]})

    monkeypatch.setattr(app, '_new_catalog', lambda credential: ModelCatalog(credential, httpx.MockTransport(handle)))
    async with app.run_test() as pilot:
        assert app.profile_name is None
        await app._command('/connect')
        await pilot.pause()
        app.screen.query_one(Input).value = 'deepseek'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        entry = app.screen.query_one(Input)
        assert entry.password
        entry.value = 'fixture-entered-key'
        await pilot.press('enter')
        await app.workers.wait_for_complete()
        assert len(requests) == 1
        assert app._profile().provider == 'deepseek'
        assert app.profile_name == 'deepseek-direct'
        assert os.environ['DEEPSEEK_API_KEY'] == 'fixture-entered-key'
        assert 'AI_API_KEY' not in os.environ
        assert 'available' in str(app.query_one('#status', Static).render())
        assert 'fixture-entered-key' not in str(app.query_one('#model-info', Static).render())
        assert env_file.read_text() == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/model', '/models', '/connect', '/providers'])
async def test_evaluation_picker_switches_provider_using_exported_key_only(app, monkeypatch, command):
    import httpx
    from ion.config import apply_environment, resolve_credential
    from ion.models.catalog import ModelCatalog

    monkeypatch.setenv('AI_API_KEY', 'fixture-qwen-evaluation-key')
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'stale-local-key')
    config = apply_environment(app.config, force_evaluation=True)
    evaluation_app = IonApp(config, app.repo_path)
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == 'https://dashscope.aliyuncs.com/compatible-mode/v1/models'
        assert request.headers['Authorization'] == 'Bearer fixture-qwen-evaluation-key'
        return httpx.Response(200, json={'data': [{'id': 'qwen-plus'}]})

    monkeypatch.setattr(evaluation_app, '_new_catalog', lambda credential: ModelCatalog(credential, httpx.MockTransport(handle)))
    monkeypatch.setattr('ion.doctor.ModelCatalog', lambda credential, **kwargs: ModelCatalog(credential, httpx.MockTransport(handle)))
    async with evaluation_app.run_test() as pilot:
        assert evaluation_app._profile().provider == 'deepseek'
        assert '/model' in str(evaluation_app.query_one('#status', Static).render())
        assert 'Qwen' in str(evaluation_app.query_one('#status', Static).render())
        await evaluation_app._command(command)
        await evaluation_app.workers.wait_for_complete()
        await pilot.pause()
        assert isinstance(evaluation_app.screen, Picker)
        assert len(evaluation_app.screen.items) == 2
        assert not requests
        evaluation_app.screen.query_one(Input).value = 'qwen'
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert not isinstance(evaluation_app.screen, Picker)
        profile = evaluation_app._profile()
        assert profile.provider == 'qwen' and profile.model_id == 'qwen-plus'
        assert profile.locked
        assert resolve_credential(profile, 'evaluation') == ('fixture-qwen-evaluation-key', 'AI_API_KEY')
        assert not requests
        await evaluation_app._command('/doctor')
        assert len(requests) == 1
        assert 'fixture-qwen-evaluation-key' not in str(evaluation_app.query_one('#model-info', Static).render())


@pytest.mark.asyncio
async def test_evaluation_picker_cannot_switch_during_task(app):
    from ion.config import apply_environment

    evaluation_app = IonApp(apply_environment(app.config, force_evaluation=True), app.repo_path)
    async with evaluation_app.run_test() as pilot:
        await evaluation_app.show_models()
        await pilot.pause()
        assert isinstance(evaluation_app.screen, Picker)
        evaluation_app.screen.query_one(Input).value = 'qwen'
        await pilot.pause()
        evaluation_app.running_task = asyncio.create_task(asyncio.Event().wait())
        await pilot.press('enter')
        await pilot.pause()
        assert evaluation_app._profile().provider == 'deepseek'
        await evaluation_app.show_models()
        assert not isinstance(evaluation_app.screen, Picker)
        await evaluation_app.action_cancel()
        await asyncio.gather(evaluation_app.running_task, return_exceptions=True)


@pytest.fixture
def issue_checkout(app, monkeypatch, tmp_path):
    checkout = tmp_path / 'cloned-issue-repo'
    checkout.mkdir()
    async def prepare(url, root, **kwargs):
        return checkout
    monkeypatch.setattr('ion.tools.github.prepare_issue_workspace', prepare)
    return checkout


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['https://github.com/acme/repo/issues/1', 'Please fix https://github.com/acme/repo/issues/1 and add a regression test.'])
async def test_pasted_issue_clones_before_import_and_runs_in_checkout(app, monkeypatch, tmp_path, text):
    app.profile_name = 'deepseek-direct'
    checkout = tmp_path / 'issue-checkout'
    checkout.mkdir()
    order = []
    async def prepare(url, root, **kwargs):
        order.append('clone')
        return checkout
    async def fetch(url, root):
        assert root == checkout
        order.append('import')
        return '# Issue evidence'
    async def run(text, *, context_markdown=None, workspace_root=None):
        assert workspace_root == checkout
        assert context_markdown == '# Issue evidence'
        assert '/issues/1' in text
        order.append('run')
    monkeypatch.setattr('ion.tools.github.prepare_issue_workspace', prepare, raising=False)
    monkeypatch.setattr('ion.tools.github.import_issue', fetch)
    monkeypatch.setattr(app, '_run_task', run)
    launch_root = app.repo_path
    async with app.run_test() as pilot:
        app.query_one(Composer).text = text
        await pilot.press('enter')
        await pilot.pause()
        await app.running_task
        assert order == ['clone', 'import', 'run']
        assert app.repo_path == launch_root


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_name', ['deepseek-direct', 'qwen-direct'])
async def test_evaluation_issue_edits_and_verifies_checkout_with_universal_key(app, monkeypatch, issue_checkout, profile_name):
    import httpx
    import json
    import shlex
    import sys
    from ion.config import apply_environment, resolve_profile
    from ion.models.catalog import ModelCatalog
    from ion.providers.openai_compatible import OpenAICompatibleProvider

    (issue_checkout / 'app.py').write_text('value = 1\n')
    (issue_checkout / 'test_app.py').write_text('from app import value\ndef test_value():\n    assert value == 2\n')
    monkeypatch.setenv('AI_API_KEY', 'fixture-evaluation-key')
    config = apply_environment(app.config, force_evaluation=True)
    evaluation_app = IonApp(config, app.repo_path)
    profile = resolve_profile(config, profile_name, 'evaluation')
    calls = []
    turns = iter([
        ('file_read', {'relative_path': 'app.py'}),
        ('edit_file', {'plan': 'Fix value', 'read_id': 'r1', 'old_text': 'value = 1', 'new_text': 'value = 2', 'done': False}),
        ('command_start', {'command': f'{shlex.quote(sys.executable)} -m pytest -q test_app.py'}),
        ('finish_request', {'summary': 'Fixed the issue and passed its regression test.'}),
    ])
    def handle(request):
        assert str(request.url).startswith(profile.endpoint + '/')
        assert request.headers['Authorization'] == 'Bearer fixture-evaluation-key'
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [{'id': profile.model_id}]})
        body = json.loads(request.content)
        assert body['model'] == profile.model_id
        calls.append(body)
        tool, args = next(turns)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'tool_calls', 'message': {
            'content': None, 'tool_calls': [{'id': f'call-{len(calls)}', 'type': 'function',
                'function': {'name': tool, 'arguments': json.dumps(args)}}]}}]})
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(evaluation_app, '_new_catalog', lambda key: ModelCatalog(key, transport))
    monkeypatch.setattr('ion.tui.app.OpenAICompatibleProvider', lambda profile, key: OpenAICompatibleProvider(profile, key, transport))
    async def fetch(url, root):
        assert root == issue_checkout
        return '# Wrong value\napp.py should set value to 2. Run test_app.py.'
    monkeypatch.setattr('ion.tools.github.import_issue', fetch)
    async with evaluation_app.run_test() as pilot:
        if profile_name == 'qwen-direct':
            await evaluation_app.show_models()
            await pilot.pause()
            evaluation_app.screen.query_one(Input).value = 'qwen'
            await pilot.pause()
            await pilot.press('enter')
            await pilot.pause()
        evaluation_app.query_one(Composer).text = 'https://github.com/acme/repo/issues/1'
        await pilot.press('enter')
        await pilot.pause()
        await evaluation_app.running_task
        assert len(calls) == 4
        assert (issue_checkout / 'app.py').read_text() == 'value = 2\n'
        assert not (Path(app.repo_path) / 'app.py').exists()
        row = evaluation_app.store.recent()[0]
        saved = evaluation_app.store.inspect(row['task_id'])
        assert saved['task']['repo_path'] == str(issue_checkout)
        assert saved['result']['outcome'] == 'verified'
        assert saved['result']['verification_ids']
        await evaluation_app._command('/repo')
        assert str(issue_checkout) in str(evaluation_app.query_one('#status', Static).render())
        assert evaluation_app.repo_path == app.repo_path


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['solve the issue', 'Please fix it', 'solve them', 'solve', 'fix', 'resolve', 'repair'])
async def test_issue_reference_without_context_requests_details_before_network(app, monkeypatch, text):
    app.profile_name = 'deepseek-direct'
    async def unexpected_run(*args, **kwargs):
        pytest.fail('An unresolved issue reference must not spend model requests')
    monkeypatch.setattr(app, '_run_task', unexpected_run)
    async with app.run_test() as pilot:
        app.query_one(Composer).text = text
        await pilot.press('enter')
        await pilot.pause()
        assert app.running_task is None
        assert 'issue URL' in str(app.query_one('#status', Static).render())
        assert app.query_one(Composer).text == text


@pytest.mark.asyncio
@pytest.mark.parametrize('followup', ['solve the issue', 'solve', 'fix', 'resolve', 'repair'])
async def test_issue_followup_keeps_evidence_and_checkout_until_new_task(app, monkeypatch, tmp_path, followup):
    app.profile_name = 'deepseek-direct'
    checkout = tmp_path / 'issue-repo'
    checkout.mkdir()
    clones, fetches, runs = [], [], []
    async def prepare(url, root, **kwargs):
        clones.append(url)
        return checkout
    async def fetch(url, root):
        fetches.append(url)
        return '# Issue details\nFix the parser crash.'
    async def run(text, **kwargs):
        runs.append((text, kwargs))
        app.query_one(Composer).text = ''
    monkeypatch.setattr('ion.tools.github.prepare_issue_workspace', prepare)
    monkeypatch.setattr('ion.tools.github.import_issue', fetch)
    monkeypatch.setattr(app, '_run_task', run)
    async with app.run_test() as pilot:
        for message in ['https://github.com/acme/repo/issues/1', followup, 'retry']:
            app.query_one(Composer).text = message
            await pilot.press('enter')
            await pilot.pause()
            await app.running_task
        assert len(clones) == len(fetches) == 1
        assert len(runs) == 3
        for text, kwargs in runs:
            assert '/issues/1' in text
            assert kwargs['workspace_root'] == checkout
            assert 'parser crash' in kwargs['context_markdown']
        assert followup in runs[1][0]
        app.action_new()
        app.query_one(Composer).text = 'solve the issue'
        await pilot.press('enter')
        await pilot.pause()
        assert len(runs) == 3
        assert 'issue URL' in str(app.query_one('#status', Static).render())


@pytest.mark.asyncio
async def test_retry_failed_issue_fetch_reuses_clone_but_regular_task_clears_context(app, monkeypatch, tmp_path):
    app.profile_name = 'deepseek-direct'
    checkout = tmp_path / 'retry-checkout'
    checkout.mkdir()
    clones, fetches, runs = [], [], []
    async def prepare(url, root, **kwargs):
        clones.append(url)
        return checkout
    async def fetch(url, root):
        fetches.append(url)
        if len(fetches) == 1:
            raise ValueError('Temporary GitHub failure')
        return '# Retry evidence'
    async def run(text, **kwargs):
        runs.append((text, kwargs))
    monkeypatch.setattr('ion.tools.github.prepare_issue_workspace', prepare)
    monkeypatch.setattr('ion.tools.github.import_issue', fetch)
    monkeypatch.setattr(app, '_run_task', run)
    async with app.run_test() as pilot:
        for text in ['https://github.com/acme/repo/issues/1', 'retry', 'Explain README.md']:
            app.query_one(Composer).text = text
            await pilot.press('enter')
            await pilot.pause()
            await app.running_task
        assert len(clones) == 1 and len(fetches) == 2
        assert runs[0][1]['workspace_root'] == checkout
        assert runs[1] == ('Explain README.md', {})
        app.query_one(Composer).text = 'solve the issue'
        await pilot.press('enter')
        await pilot.pause()
        assert len(runs) == 2
        assert 'issue URL' in str(app.query_one('#status', Static).render())


@pytest.mark.asyncio
async def test_pasted_issue_shows_preparation_and_enter_feedback_and_escape_cancels(app, monkeypatch):
    app.profile_name = 'deepseek-direct'
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def prepare(url, root, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr('ion.tools.github.prepare_issue_workspace', prepare)
    async with app.run_test() as pilot:
        from textual.events import Paste
        app.post_message(Paste('https://github.com/acme/repo/issues/1'))
        await pilot.pause()
        await pilot.press('enter')
        await asyncio.wait_for(entered.wait(), 2)
        assert app.screen.has_class('session')
        assert app.query_one(Composer).text == ''
        assert app.query_one('#cancel', Button).display
        assert 'Cloning' in str(app.query_one('#status', Static).render())
        app.query_one(Composer).text = 'Is it running?'
        await pilot.press('enter')
        await pilot.pause()
        assert 'preparing' in str(app.query_one('#status', Static).render()).lower()
        assert app.query_one(Composer).text == 'Is it running?'
        await pilot.press('escape')
        await app.running_task
        assert cancelled.is_set()
        assert not app._busy()
        assert not app.query_one('#cancel', Button).display
        assert 'send' in str(app.query_one('#run', Button).label)
