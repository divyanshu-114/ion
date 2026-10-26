from __future__ import annotations

import asyncio
import os
from time import monotonic
from datetime import datetime
from pathlib import Path

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from ion.artifacts import ArtifactStore
from ion.config import AppConfig, resolve_credential, resolve_profile, validate_task
from ion.contracts import ModelProfile
from ion.doctor import Doctor
from ion.diagnostics import DiagnosticLogger, recent_diagnostics
from ion.engine import Engine
from ion.intent import is_issue_followup
from ion.model_selection import select_model
from ion.models.catalog import ModelCatalog
from ion.processes import CommandSupervisor
from ion.providers.openai_compatible import OpenAICompatibleProvider
from ion.recovery import WorkspaceLease, WorkspaceRecoveryRequired
from ion.memory.store import MemoryStore
from ion.storage import RunStore
from ion.session import SessionService, SessionSocketServer
from ion.tools.registry import ToolDispatcher
from ion.workspace import Workspace
from ion.tui.widgets import Brand, Composer, EntryDialog, Picker, Transcript
from ion.tui.logs import PHASE_LABELS, activity_message, format_diagnostic, mask_keys
from ion.tui.commands import COMMANDS


class IonApp(App, inherit_bindings=False):
    CSS_PATH = 'app.tcss'
    TITLE = 'Ion'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding('ctrl+c', 'shutdown', 'Exit', priority=True),
        Binding('escape', 'cancel', 'Stop task'),
    ]

    def __init__(self, config: AppConfig, workspace_root: str | Path | None = None) -> None:
        super().__init__()
        self.config = config
        root = Path(workspace_root or Path.cwd()).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError('workspace root must be a directory')
        self.repo_path = str(root)
        self.displayed_workspace = self.repo_path
        self._issue_url: str | None = None
        self._issue_workspace: Path | None = None
        self._issue_markdown: str | None = None
        self.mode = 'evaluation' if config.evaluation_profile else 'product'
        self.profile_name: str | None = config.evaluation_profile
        self.profile_override: ModelProfile | None = None
        self.engine: Engine | None = None
        self.running_task: asyncio.Task | None = None
        self.active_task_id: str | None = None
        self.store: RunStore | None = None
        self.session_service: SessionService | None = None
        self.session_server: SessionSocketServer | None = None
        self.catalog = None
        self._transcript: list = []
        self._dismissed_command_text: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id='masthead'):
            yield Static('ION', id='masthead-brand')
            yield Static('AUTONOMOUS REPOSITORY WORKBENCH', id='masthead-title')
            yield Static('/help  COMMANDS', id='masthead-command')
        with Horizontal(id='body'):
            with Vertical(id='main'):
                yield Static('New task', id='session-header', markup=False)
                yield Transcript(id='activity', wrap=True, markup=False, highlight=False, max_lines=2500, min_width=1)
                with Vertical(id='center'):
                    with Vertical(id='home'):
                        yield Brand()
                        yield Static('LOCAL TOOLS  /  YOUR MODELS  /  VERIFIED CHANGES', id='home-caption')
                    with Vertical(id='composer-wrap'):
                        yield OptionList(id='command-suggestions', markup=False)
                        yield Static('TASK / STEER', id='composer-label')
                        with Vertical(id='composer'):
                            yield Composer(id='task', placeholder='Describe one outcome… or type /help', highlight_cursor_line=False)
                            with Horizontal(id='composer-meta'):
                                yield Static('', id='profile', markup=False)
                                yield Button('send ↵', id='run')
                                yield Button('stop esc', id='cancel', disabled=True)
                        yield Static('Type / for commands    Shift+Enter new line    Ctrl+C exit', id='hints')
                        yield Static('', id='status', markup=False)
            with Vertical(id='sidebar'):
                yield Static('RUN STATE', id='sidebar-title')
                yield Static('01  CONTEXT', classes='side-heading')
                yield Static('No requests yet', id='context-info', classes='side-text', markup=False)
                yield Static('02  MODEL', classes='side-heading')
                yield Static('', id='model-info', classes='side-text', markup=False)
                yield Static('03  WORKSPACE', classes='side-heading')
                yield Static('', id='repo-info', classes='side-text', markup=False)
                yield Static('04  CHANGES', classes='side-heading')
                yield Static('No files changed', id='changes-info', classes='side-text', markup=False)
                yield Static('05  VERIFICATION', classes='side-heading')
                yield Static('Not run', id='verification-info', classes='side-text', markup=False)
        with Horizontal(id='bottom'):
            yield Static('', id='cwd', markup=False)
            yield Static('LOCAL  /  ION 0.1.0', id='version')

    async def on_mount(self) -> None:
        self.theme = 'textual-dark'
        self.store = RunStore(self._data_root() / 'runs.sqlite3')
        self.session_service = SessionService(self.store)
        self.session_server = SessionSocketServer(self.session_service, SessionSocketServer.compact_path(self._data_root()))
        try:
            await self.session_server.start()
        except OSError:
            # Some restricted runners disallow AF_UNIX sockets. Keep the
            # foreground client usable and expose the limitation in status.
            self.session_server = None
            self._status('Private session socket unavailable; using foreground session journal.')
        self.default_screen.add_class('sidebar-visible')
        self._profile_label()
        self._repo_label()
        if self.mode == 'evaluation' and len(self.config.evaluation_profiles) > 1:
            self._status('DeepSeek selected. Using a Qwen key? Use /model to select Qwen.')
        composer = self.query_one(Composer)
        composer.suggestions = self.query_one('#command-suggestions', OptionList)
        composer.suggestions.can_focus = False
        composer.focus()

    def on_resize(self, event) -> None:
        self.default_screen.set_class(event.size.width < 105, 'narrow')
        self.default_screen.set_class(event.size.height < 32, 'short')

    async def on_unmount(self) -> None:
        if self.running_task and not self.running_task.done():
            self.running_task.cancel()
            await asyncio.gather(self.running_task, return_exceptions=True)
        if self.store:
            if self.active_task_id:
                self.store.interrupt(self.active_task_id)
            self.store.close()
        if self.session_server:
            await self.session_server.close()

    @staticmethod
    def _data_root() -> Path:
        return Path(os.environ.get('ION_DATA_DIR', Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'ion'))

    def _profile(self) -> ModelProfile:
        if self.profile_name is None:
            raise ValueError('No model selected. Use /models to select a model.')
        return self.profile_override or resolve_profile(self.config, self.profile_name, self.mode)

    def _profile_label(self) -> None:
        if self.profile_name is None:
            self.query_one('#profile', Static).update('MODEL  No model selected')
            self.query_one('#model-info', Static).update('No model selected\nUse /models to choose')
            return
        profile = self._profile()
        credential, _ = resolve_credential(profile, self.mode)
        label = Text('MODEL  ', style='#82b7b5')
        label.append(profile.model_id.split('/')[-1], style='#e5e9e8')
        label.append(f'  /  {profile.provider}' + ('  /  key needed' if not credential else ''), style='#7d888b')
        self.query_one('#profile', Static).update(label)
        selection = ('Evaluation · /model to choose provider' if len(self.config.evaluation_profiles) > 1 else 'Evaluation locked') if self.mode == 'evaluation' else 'Selected for next task'
        self.query_one('#model-info', Static).update(f'{profile.model_id.split("/")[-1]}\n{profile.provider}\nKey: {"***" if credential else "not set"}\n' + selection)

    def _repo_label(self, workspace_root: Path | None = None) -> None:
        self.displayed_workspace = str(workspace_root or self.repo_path)
        path = self.displayed_workspace.replace(str(Path.home()), '~', 1)
        self.query_one('#cwd', Static).update(path)
        self.query_one('#repo-info', Static).update(path)

    def _status(self, value: str) -> None:
        self.query_one('#status', Static).update(mask_keys(value))

    def _log(self, value) -> None:
        renderable = Text(value) if isinstance(value, str) else value
        if isinstance(renderable, Text):
            masked = mask_keys(renderable.plain)
            if masked != renderable.plain:
                renderable = Text(masked, style=renderable.style)
        self._transcript.append(renderable)
        self._transcript = self._transcript[-400:]
        log = self.query_one('#activity', RichLog)
        log.write(renderable, width=max(1, log.scrollable_content_region.width - 1) if log.size.width else None)

    def _reflow_transcript(self) -> None:
        log = self.query_one('#activity', RichLog)
        log.clear()
        for renderable in self._transcript:
            log.write(renderable, width=max(1, log.scrollable_content_region.width - 1))

    def _session(self, title: str = 'Ion') -> None:
        self.default_screen.add_class('session')
        self.query_one('#session-header', Static).update(title[:110])
        self.call_after_refresh(self._reflow_transcript)

    def _busy(self) -> bool:
        return bool(self.running_task and not self.running_task.done())

    def _require_model(self) -> bool:
        if self.profile_name is not None:
            return True
        self._status('Select a model with /models before continuing. Use /connect to add a provider key.')
        return False

    def action_commands(self) -> None:
        items = [(command, f'{command:<19} {description}') for command, description in COMMANDS]
        self.push_screen(Picker('Commands', items), self._picked_command)

    def _hide_command_suggestions(self) -> None:
        self.query_one('#command-suggestions', OptionList).display = False
        self.default_screen.remove_class('suggesting')
        self.query_one('#hints', Static).update('Type / for commands    Shift+Enter new line    Ctrl+C exit')

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if not isinstance(event.text_area, Composer):
            return
        text = event.text_area.text
        choices = self.query_one('#command-suggestions', OptionList)
        if text == self._dismissed_command_text:
            self._hide_command_suggestions()
            return
        self._dismissed_command_text = None
        matches = [
            (command, description) for command, description in COMMANDS
            if command.split(' ', 1)[0].startswith(text)
        ] if text.startswith('/') and not any(char.isspace() for char in text) else []
        choices.clear_options()
        if not matches:
            self._hide_command_suggestions()
            return
        choices.add_options(Option(f'{command:<19} {description}', id=command) for command, description in matches)
        choices.highlighted = 0
        choices.display = True
        self.default_screen.add_class('suggesting')
        self.query_one('#hints', Static).update('↑↓ browse   Tab complete   Enter select   Esc dismiss')

    def _complete_command(self, command: str) -> None:
        composer = self.query_one(Composer)
        text = command.split(' ', 1)[0] + (' ' if ' ' in command else '')
        self._dismissed_command_text = text
        composer.text = text
        composer.cursor_location = (0, len(text))
        self._hide_command_suggestions()
        composer.focus()

    def on_composer_complete_command(self, event: Composer.CompleteCommand) -> None:
        choices = self.query_one('#command-suggestions', OptionList)
        if choices.highlighted is not None:
            command = choices.get_option_at_index(choices.highlighted).id
            if command:
                self._complete_command(command)

    def on_composer_dismiss_commands(self, event: Composer.DismissCommands) -> None:
        self._dismissed_command_text = self.query_one(Composer).text
        self._hide_command_suggestions()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != 'command-suggestions':
            return
        event.stop()
        self._hide_command_suggestions()
        self.query_one(Composer).text = ''
        self._picked_command(event.option.id)

    def _picked_command(self, value: str | None) -> None:
        if value:
            if ' ' in value:
                self._complete_command(value)
            else:
                self.run_worker(self._command(value))

    def action_sidebar(self) -> None:
        self.default_screen.toggle_class('sidebar-visible')
        self.call_after_refresh(self._reflow_transcript)

    def action_new(self) -> None:
        if self._busy():
            self._status('Stop the current task before starting a new one.')
            return
        self.default_screen.remove_class('session')
        self._clear_issue_context()
        self.query_one('#activity', RichLog).clear()
        self._transcript.clear()
        self.query_one(Composer).text = ''
        self._repo_label()
        self._status('')
        self.query_one(Composer).focus()

    async def action_cancel(self) -> None:
        if self._busy():
            if self.engine:
                await self.engine.cancel()
            self.running_task.cancel()
            self._status('Cancelled. Partial changes remain in the repository.')
        else:
            self._status('No task is running.')

    async def action_shutdown(self) -> None:
        if self._busy():
            await self.action_cancel()
            await asyncio.gather(self.running_task, return_exceptions=True)
        self.exit()

    async def on_composer_submitted(self, event: Composer.Submitted) -> None:
        text = self.query_one(Composer).text.strip()
        if not text:
            return
        if text.startswith('/'):
            self.query_one(Composer).text = ''
            await self._command(text)
        elif self._busy():
            if self.engine:
                await self.engine.steer(text)
                self.query_one(Composer).text = ''
                self._status('Steering queued for the next turn.')
            else:
                self._status('Still preparing the repository or model connection. Your text is kept; use Esc or /stop to cancel.')
        elif self._require_model():
            from ion.tools.github import find_issue_url
            issue_url = find_issue_url(text)
            if issue_url:
                self.running_task = asyncio.create_task(self._run_github(issue_url, task_text=text))
            elif is_issue_followup(text):
                if self._issue_url:
                    self.running_task = asyncio.create_task(self._run_github(self._issue_url, task_text=text, reuse=True))
                else:
                    self._status('Paste the GitHub issue URL or describe the bug and expected behavior. This task has no issue context yet.')
            else:
                self._clear_issue_context()
                self.running_task = asyncio.create_task(self._run_task(text))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'run':
            await self.on_composer_submitted(Composer.Submitted())
        elif event.button.id == 'cancel':
            await self.action_cancel()

    async def _command(self, command: str) -> None:
        if command == '/github' or command.startswith('/github '):
            if self._busy():
                self._status('A task is already running. Use /stop first.')
                return
            if not self._require_model():
                return
            from ion.tools.github import issue_identity
            url = command.removeprefix('/github').strip()
            try:
                issue_identity(url)
            except ValueError as exc:
                self._status(str(exc))
                return
            self.running_task = asyncio.create_task(self._run_github(url))
        elif command in ('/model', '/models'):
            self.run_worker(self.show_models(), exclusive=True, group='catalog')
        elif command in ('/help', '/'):
            self.action_commands()
        elif command in ('/new', '/clear'):
            self.action_new()
        elif command == '/sidebar':
            self.action_sidebar()
        elif command == '/repo':
            self._status(f'Workspace locked to: {self.displayed_workspace}')
        elif command in ('/stop', '/cancel'):
            await self.action_cancel()
        elif command in ('/quit', '/exit'):
            await self.action_shutdown()
        elif command == '/providers':
            if self.mode == 'evaluation':
                self._show_evaluation_picker('Providers')
                return
            self._session('Providers')
            seen = set()
            profiles = [self._profile()] if self.mode == 'evaluation' else [resolve_profile(self.config, name, 'product') for name in self.config.profiles]
            for profile in profiles:
                key, key_name = resolve_credential(profile, self.mode)
                identity = (profile.provider, profile.endpoint, key_name)
                if identity in seen:
                    continue
                seen.add(identity)
                active = ' · active' if self.profile_name is not None and profile.endpoint == self._profile().endpoint else ''
                self._log(f'{profile.provider}{active}\n  {profile.endpoint}\n  {key_name}={"***" if key else "not set"}')
            self._status('Use /doctor to check the active connection.' if self.mode == 'evaluation' else 'Use /connect to add or replace a key.')
        elif command == '/connect':
            if self.mode == 'evaluation':
                self._show_evaluation_picker('Providers')
                return
            if self._busy():
                self._status('Provider connection is locked during a running task.')
                return
            items = []
            seen = set()
            for name in self.config.profiles:
                profile = resolve_profile(self.config, name, self.mode)
                connector = (profile.provider, profile.endpoint, profile.api_key_env)
                if connector not in seen:
                    seen.add(connector)
                    key, _ = resolve_credential(profile, self.mode)
                    items.append((name, f'{profile.provider}  ·  ' + ('key: ***' if key else 'not connected')))
            self.push_screen(Picker('Connect a provider', items, 'Keys entered here are kept only for this Ion session.'), self._connect_profile)
        elif command in ('/history', '/sessions'):
            rows = self.store.recent() if self.store else []
            self.push_screen(Picker('Sessions', [(row['task_id'], f"{row['task']['text'][:55]}  ·  {row['status']}") for row in rows], 'Select a task to inspect its result · esc close'), lambda value: self._inspect(value) if value else None)
        elif command.startswith('/inspect '):
            self._inspect(command.removeprefix('/inspect ').strip())
        elif command.startswith('/resume '):
            self._resume(command.removeprefix('/resume ').strip())
        elif command == '/doctor':
            if not self._require_model():
                return
            self._session('Connection diagnostics')
            self._status('Checking provider connection…')
            checks = await Doctor().run(self.config, self.profile_name, self.mode, self._profile(), self.catalog)
            self._log('\n'.join(f'{item.status}  {item.name} — {item.detail}' for item in checks))
            self._status('Connection diagnostics complete.')
        elif command in ('/logs', '/debug'):
            path = self._data_root() / 'logs' / 'ion.jsonl'
            records = recent_diagnostics(path)
            self._session('Activity logs')
            self._log('Recent activity · local time · oldest to newest')
            if not records:
                self._log('No activity yet. Submit a task to see progress here.')
            for record in records:
                self._log(format_diagnostic(record))
            self._status(f'{len(records)} recent events · Detailed log: {path}')
        elif command.startswith('/steer ') and self.engine:
            await self.engine.steer(command.removeprefix('/steer '))
        else:
            self._status('Unknown command. Type /help to see available commands.')

    def _connect_profile(self, name: str | None) -> None:
        if not name:
            return
        profile = resolve_profile(self.config, name, self.mode)
        previous = os.environ.get(profile.api_key_env)
        def save(value: str | None) -> None:
            if value:
                os.environ[profile.api_key_env] = value
                self.catalog = None
                self._status('Validating provider key…')
                self.run_worker(self._validate_connection(name, profile, value, previous), exclusive=True, group='catalog')
        self.push_screen(EntryDialog(f'Connect {profile.provider}', hint='Masked input · session only · never saved to the repository', secret=True), save)

    def _new_catalog(self, credential: str) -> ModelCatalog:
        ttl = int(self.config.model_catalog.get('cache_ttl_seconds', 300))
        return ModelCatalog(credential, cache_ttl_seconds=ttl)

    async def _validate_connection(self, name: str, profile: ModelProfile, credential: str, previous: str | None) -> None:
        catalog = self._new_catalog(credential)
        result = await catalog.list(profile, refresh=True)
        if result.status in {'authentication_failed', 'access_denied'}:
            if previous is None:
                os.environ.pop(profile.api_key_env, None)
            else:
                os.environ[profile.api_key_env] = previous
            self._status(f'Connection rejected: {result.detail}')
            self._profile_label()
            return
        self.profile_name, self.profile_override = name, None
        self.catalog = catalog
        self._profile_label()
        if result.status == 'available':
            configured = next((item for item in result.entries if item.model_id == profile.model_id and item.available), None)
            if configured:
                if configured.free is True and profile.provider == 'openrouter':
                    quota = await catalog.quota(profile, refresh=True)
                    if quota.status == 'available' and quota.remaining == 0:
                        self._status('Provider connected, but its free-model daily quota is exhausted.')
                        return
                self._status('Provider connected and configured model is available.')
            else:
                self._status('Provider connected, but the configured model is unavailable. Choose another model with /models.')
        else:
            self._status(f'Key saved for this session; validation unavailable: {result.detail or result.status}.')

    def _inspect(self, task_id: str) -> None:
        row = self.store.inspect(task_id) if self.store else None
        if not row:
            self._status('Task not found.')
            return
        self._session(row['task']['text'])
        self._log(Panel(Text(row['task']['text']), title='TASK', border_style='#36515a'))
        self._log(f"{row['status']} · {row['task']['repo_path']}")
        if row['result']:
            self._log(row['result']['summary'])
        for event in row['events'][-15:]:
            self._log(f"{event['phase']}  {event['message']}")

    def _resume(self, task_id: str) -> None:
        if not self.store or not task_id:
            self._status('Usage: /resume TASK_ID')
            return
        row = self.store.inspect(task_id)
        if not row:
            self._status('Task not found.')
            return
        unresolved = self.store.unresolved_operations(task_id)
        if unresolved:
            self._session('Recovery required')
            self._log(f"Cannot resume {task_id}: {len(unresolved)} operation(s) have unknown outcomes.")
            self._log('Inspect the workspace and reconcile those operations before granting another writer.')
            self._status('Resume blocked until operation reconciliation is recorded.')
            return
        self._session(row['task']['text'])
        self.query_one(Composer).text = row['task']['text']
        self._status('Task is safe to re-submit; the previous engine run is not replayed.')

    def _show_evaluation_picker(self, title: str = 'Models') -> None:
        if self._busy():
            self._status('Model selection is locked during a task.')
            return
        names = self.config.evaluation_profiles
        if len(names) < 2:
            self._status('Model selection is locked to the configured evaluation profile.')
            return
        items = []
        for name in names:
            profile = resolve_profile(self.config, name, 'evaluation')
            label = {'deepseek': 'DeepSeek', 'qwen': 'Qwen'}.get(profile.provider, profile.provider)
            if profile.provider == 'qwen' and '://dashscope.aliyuncs.com/' in profile.endpoint:
                label += ' (Beijing)'
            active = '● ' if name == self.profile_name else '  '
            items.append((name, f'{active}{label} / {profile.model_id}  ·  {profile.endpoint}'))

        def select(name: str | None) -> None:
            if name not in names or self._busy():
                return
            self.profile_name, self.profile_override = name, None
            self.catalog = None
            self._profile_label()
            self._status(f'{self._profile().provider} selected · using exported AI_API_KEY. Use /doctor to check the connection.')

        self.push_screen(Picker(title, items, 'Choose the provider that issued AI_API_KEY · no key re-entry · Esc to cancel'), select)

    async def show_models(self) -> None:
        if self.mode == 'evaluation':
            self._show_evaluation_picker()
            return
        if self._busy():
            self._status('Model selection is locked during a task.')
            return
        profile = self._profile() if self.profile_name is not None else None
        key = resolve_credential(profile, self.mode)[0] if profile else ''
        choices = {}
        result = None
        if key and self.config.model_catalog.get('allow_runtime_discovery', True):
            self._status('Loading models…')
            self.catalog = self.catalog or self._new_catalog(key)
            result = await self.catalog.list(profile)
            for index, model in enumerate(result.entries):
                require_tools = self.config.model_catalog.get('require_tools', False)
                if model.available and model.text_only and model.context_window and (not require_tools or model.supports_tools is True):
                    token = f'model:{model.model_id}'
                    choices[token] = model
            self._status('' if result.status == 'available' else f'Live catalog unavailable: {result.detail or result.status}. Configured profiles are still listed.')
        live_ids = {item.model_id for item in result.entries} if result and result.status == 'available' else set()
        unavailable_profiles = set()
        items = []
        for name, raw in self.config.profiles.items():
            configured = resolve_profile(self.config, name, self.mode)
            suffix = ''
            if result and result.status == 'available' and configured.endpoint == profile.endpoint and configured.model_id not in live_ids:
                unavailable_profiles.add(name)
                suffix = '  ·  unavailable'
            selected = name == self.profile_name and self.profile_override is None
            items.append((f'profile:{name}', ('● ' if selected else '  ') + f"{raw['provider']} / {raw.get('model', raw.get('model_id', ''))}{suffix}"))
        for token, model in choices.items():
            price = 'free' if model.free else 'paid' if model.free is False else 'price unknown'
            items.append((token, f'{model.model_id}  ·  {price}'))
        if self._busy():
            return
        def select(value: str | None) -> None:
            if not value or self._busy():
                return
            if value.startswith('profile:'):
                name = value.split(':', 1)[1]
                if name in unavailable_profiles:
                    self._status('That configured model is unavailable. Select a live catalog model instead.')
                    return
                self.profile_name, self.profile_override = name, None
                self.catalog = None
            elif value in choices:
                try:
                    self.profile_override = select_model(profile, choices[value], self.mode)
                except ValueError as exc:
                    self._status(str(exc))
                    return
            self._profile_label()
            self._status('Model selected for the next task.')
        self.push_screen(Picker('Models', items, 'Configured profiles + live catalog · /connect to add a key'), select)

    def _clear_issue_context(self) -> None:
        self._issue_url = None
        self._issue_workspace = None
        self._issue_markdown = None

    async def _run_github(self, url: str, *, task_text: str | None = None, reuse: bool = False) -> None:
        from ion.tools.github import import_issue, prepare_issue_workspace
        if not reuse or self._issue_url != url:
            self._clear_issue_context()
            self._issue_url = url
        self._session(f'GitHub issue: {url}')
        self.query_one(Composer).text = ''
        self.query_one('#run', Button).label = 'preparing…'
        self._log(f'Preparing issue: {url}')
        started = monotonic()
        stage = 'Preparing the GitHub issue workspace'

        def refresh_preparation():
            self._status(f'{stage} · {int(monotonic() - started)}s · Esc to cancel')

        def progress(message: str):
            nonlocal stage
            stage = message
            self.query_one('#context-info', Static).update(message)
            refresh_preparation()

        progress(stage)
        timer = self.set_interval(1, refresh_preparation)
        self.query_one('#cancel', Button).disabled = False
        self.query_one('#cancel', Button).display = True
        try:
            if self._issue_workspace is None:
                progress('Cloning the GitHub issue repository')
                self._log('Cloning repository; download progress appears in the status and context panel.')
                self._issue_workspace = await prepare_issue_workspace(url, self._data_root() / 'workspaces', on_progress=progress)
            checkout = self._issue_workspace
            if not checkout.is_dir():
                raise ValueError('The issue checkout is missing. Paste the issue URL again to create a new checkout.')
            self._session(f'GitHub issue: {url}')
            self._log(f'Issue checkout: {checkout}')
            self._repo_label(checkout)
            if self._issue_markdown is None:
                progress('Fetching GitHub issue and comments')
                self._issue_markdown = await import_issue(url, checkout)
            markdown = self._issue_markdown
            task = f'Investigate and fix the issue at {url} in this repository. Understand the code, implement the fix, and run relevant tests.'
            if task_text and task_text != url:
                task += f'\nUser request: {task_text}'
            timer.stop()
            await self._run_task(task, context_markdown=markdown, workspace_root=checkout)
        except asyncio.CancelledError:
            self._status('GitHub import cancelled.')
        except TimeoutError:
            self._status('GitHub clone or import timed out. Check network access and retry.')
        except (ValueError, OSError) as exc:
            self._status(f'Cannot import GitHub issue: {exc}')
        finally:
            timer.stop()
            self.query_one('#run', Button).label = 'send ↵'
            self.query_one('#cancel', Button).disabled = True
            self.query_one('#cancel', Button).display = False

    async def _run_task(self, text: str | None = None, *, context_markdown: str | None = None, workspace_root: Path | None = None) -> None:
        if not self._require_model():
            return
        text = self.query_one(Composer).text if text is None else text
        profile = self.profile_override or resolve_profile(self.config, self.profile_name, self.mode)
        credential, key_name = resolve_credential(profile, self.mode)
        if not credential:
            self._status(f"Set {key_name} in this terminal before starting a live task.")
            return
        self.catalog = self.catalog or self._new_catalog(credential)
        self._status('Checking the selected model connection…')
        catalog_result = await self.catalog.list(profile)
        if catalog_result.status in {'authentication_failed', 'access_denied'}:
            hint = 'Use /model to select the provider matching AI_API_KEY.' if self.mode == 'evaluation' else 'Use /connect to replace the key.'
            self._status(f'Cannot use provider: {catalog_result.detail}. {hint}')
            return
        if catalog_result.status == 'available':
            model = next((item for item in catalog_result.entries if item.model_id == profile.model_id and item.available), None)
            if model is None:
                self._status(f'Model unavailable: {profile.model_id}. Choose a current model with /models.')
                return
            if self.config.model_catalog.get('require_tools', False) and model.supports_tools is not True:
                self._status(f'Model does not advertise required tool calling: {profile.model_id}. Choose another model with /models.')
                return
            try:
                profile = select_model(profile, model, self.mode) if self.mode == 'product' and not profile.locked else profile
            except ValueError as exc:
                self._status(f'Cannot use model: {exc}')
                return
            if model.free is True and profile.provider == 'openrouter':
                quota = await self.catalog.quota(profile)
                if quota.status == 'available' and quota.remaining == 0:
                    self._status(f'Free-model quota exhausted (0/{quota.limit} remaining). Choose a paid model or wait for reset.')
                    return
        lease = None
        memory_store = None
        try:
            path = Path(workspace_root or self.repo_path).expanduser().resolve(strict=True)
            self._repo_label(path)
            task = validate_task({"text": text, "repo_path": str(path), "profile_name": self.profile_name, "mode": self.mode})
            data_root = self._data_root()
            lease = WorkspaceLease(path, data_root / 'ownership' / f'{WorkspaceLease.workspace_id(path)}.json', task.task_id)
            lease.acquire()
            workspace = Workspace.capture(path)
            artifacts = ArtifactStore(data_root / "artifacts" / task.task_id)
            if context_markdown is not None:
                artifact = artifacts.put(context_markdown.encode('utf-8'), 'github_issue')
                text += f'\nFull external evidence: artifact {artifact.artifact_id} (use artifact_read/search).\nUntrusted external evidence excerpt:\n' + context_markdown[:4000]
                task = task.model_copy(update={'text': text})
            memory_store = MemoryStore(data_root / "memory" / f"{WorkspaceLease.workspace_id(path)}.sqlite3")
            supervisor = CommandSupervisor(workspace, artifacts, credential)
            # Product runs need the bounded local command tool so completion can
            # be independently verified. CommandSupervisor still constrains the
            # environment, timeout, output, and process group.
            dispatcher = ToolDispatcher(workspace, artifacts, supervisor, allow_commands=True, lint_command=self.config.tools.lint_command)
            diagnostics = DiagnosticLogger(data_root / 'logs' / 'ion.jsonl', task.task_id)
            self.engine = Engine(self.config, OpenAICompatibleProvider(profile, credential), dispatcher, profile_override=profile, diagnostics=diagnostics, operation_store=self.store, memory_store=memory_store)
            assert self.store and self.session_service
            self.session_service.start(task)
            self.active_task_id = task.task_id
            self.query_one("#run", Button).label = "steer ↵"
            self.query_one("#cancel", Button).disabled = False
            self._session(text)
            # Issue submission already cleared the input before cloning. Keep
            # any new draft the user typed while repository preparation ran.
            if context_markdown is None:
                self.query_one(Composer).text = ''
            self.query_one('#cancel', Button).display = True
            self._log(Panel(Text(text), title='TASK', border_style='#36515a'))
            self._log(Text(f'{profile.provider} / {profile.model_id}', style='#82b7b5'))
            self._status("Running task…")
            self.query_one('#changes-info', Static).update('No edits yet')
            self.query_one('#verification-info', Static).update('Pending')
            consumer = asyncio.create_task(self._consume_events())
            try:
                result = await self.engine.run(task)
                await consumer
                self.store.finish(result)
                self.query_one('#changes-info', Static).update('\n'.join(result.changed_files) or 'No files changed')
                self.query_one('#verification-info', Static).update(f'{result.outcome.value} · {len(result.verification_ids)} records')
                self.active_task_id = None
            finally:
                if not consumer.done():
                    consumer.cancel()
            self._log(f"Result: {result.outcome.value}\n{result.summary}\nChanged: {', '.join(result.changed_files) or 'none'}\nVerification records: {len(result.verification_ids)}")
            for limitation in result.limitations:
                self._log(limitation)
            if result.patch_artifact_id:
                self._log(Syntax(artifacts.read(result.patch_artifact_id).decode('utf-8', 'replace')[:12000], 'diff', theme='monokai', background_color='#0e1113', word_wrap=True))
            # A refresh callback scheduled by _session can run after the task
            # finishes and clear RichLog while its deferred writes are still
            # pending. Reflow synchronously once all final output is present so
            # callers awaiting running_task observe a complete transcript.
            self._reflow_transcript()
            self._status(f"{result.outcome.value}: {result.summary[:160]}")
            result_summary = result.summary.lower()
            if "quota exhausted" in result_summary:
                self._log("The selected free-model quota is exhausted. Choose a paid model with /models or wait for reset.")
            elif "rate limit" in result_summary:
                self._log("Task incomplete because provider capacity was reached. Partial changes remain; resume when capacity returns.")
            elif "authentication failed" in result_summary or "access denied" in result_summary:
                self._log('The provider rejected this credential. Use /model to select the provider matching AI_API_KEY.' if self.mode == 'evaluation' else 'The provider rejected this credential. Replace it with /connect.')
            elif "model unavailable" in result_summary:
                self._log("The selected model is no longer available. Choose a live model with /models.")
            elif "credits exhausted" in result_summary:
                self._log("This provider account has insufficient credits. Choose a free model with /models or add provider credit.")
        except WorkspaceRecoveryRequired as exc:
            self._status(str(exc))
        except asyncio.CancelledError:
            self._status("Task cancelled. Partial repository changes remain.")
        except Exception as exc:
            self._status(f"Cannot run task: {type(exc).__name__}: {exc}")
        finally:
            if self.active_task_id and self.store:
                unresolved = self.store.unresolved_operations(self.active_task_id)
                if unresolved and lease:
                    lease.mark_recovery_required([item['operation_id'] for item in unresolved])
                self.store.interrupt(self.active_task_id)
                self.active_task_id = None
            if lease:
                lease.release()
            if memory_store:
                memory_store.close()
            self.engine = None
            self.query_one("#run", Button).label = "send ↵"
            self.query_one("#cancel", Button).disabled = True
            self.query_one('#cancel', Button).display = False

    async def _consume_events(self) -> None:
        assert self.engine
        async for event in self.engine.events():
            if self.store and self.active_task_id:
                if self.session_service:
                    self.session_service.append(self.active_task_id, event)
            if event.message.startswith(('Request ', 'Usage:')):
                self.query_one('#context-info', Static).update(event.message)
            else:
                label = PHASE_LABELS.get(event.phase.value, 'Working')
                time = datetime.now().strftime('%H:%M:%S')
                self._log(Text(f'{time}  {label}  {activity_message(event.message)}', style='#aaaaaa'))
            self._status(activity_message(event.message)[:180])
