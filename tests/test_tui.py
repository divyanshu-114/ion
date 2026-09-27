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
