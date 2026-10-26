from __future__ import annotations

from pathlib import Path

from PIL import Image
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option


class Transcript(RichLog):
    def on_resize(self, event) -> None:
        # Rebuild after this widget receives its final width, rather than the
        # earlier app resize event (which precedes sidebar/layout changes).
        if event.size.width != getattr(self, '_last_width', None):
            self._last_width = event.size.width
            self.call_after_refresh(self.app._reflow_transcript)


class Brand(Static):
    """Render the packaged Ion mark with a compact terminal wordmark."""

    def render(self) -> Text:
        with Image.open(Path(__file__).parent / 'assets' / 'logo.png') as source:
            gray = source.convert('L')
            bounds = gray.point(lambda value: 255 if value > 48 else 0).getbbox()
            pixels = gray.crop(bounds).resize((14, 14), Image.Resampling.LANCZOS)

        output = Text()
        lockup = {
            1: ('█ █▀█ █▄ █', 'bold #e5e9e8'),
            2: ('█ █▄█ █ ▀█', 'bold #e5e9e8'),
            4: ('AUTONOMOUS REPOSITORY WORKBENCH', '#82b7b5'),
        }
        for row in range(7):
            for col in range(14):
                top = pixels.getpixel((col, row * 2)) > 84
                bottom = pixels.getpixel((col, row * 2 + 1)) > 84
                cell = '█' if top and bottom else '▀' if top else '▄' if bottom else ' '
                output.append(cell, style='#82b7b5')
            label = lockup.get(row)
            if label:
                output.append('    ' + label[0], style=label[1])
            if row != 6:
                output.append('\n')
        return output


class Composer(TextArea):
    suggestions: OptionList | None = None

    BINDINGS = [
        Binding('enter', 'submit', 'Send', priority=True),
        Binding('tab', 'complete_command', 'Complete command', priority=True),
        Binding('escape', 'dismiss_commands', 'Dismiss commands', priority=True),
        Binding('shift+enter', 'line_break', 'New line', priority=True),
        Binding('ctrl+j', 'line_break', 'New line', priority=True),
    ]

    class Submitted(Message):
        pass

    class CompleteCommand(Message):
        pass

    class DismissCommands(Message):
        pass

    @property
    def suggesting(self) -> bool:
        return self.suggestions is not None and self.suggestions.display

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ('complete_command', 'dismiss_commands'):
            return self.suggesting
        return super().check_action(action, parameters)

    def action_submit(self) -> None:
        if self.suggesting:
            self.suggestions.action_select()
        else:
            self.post_message(self.Submitted())

    def action_complete_command(self) -> None:
        self.post_message(self.CompleteCommand())

    def action_dismiss_commands(self) -> None:
        self.post_message(self.DismissCommands())

    def action_cursor_up(self, select: bool = False) -> None:
        if self.suggesting and not select:
            self.suggestions.action_cursor_up()
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if self.suggesting and not select:
            self.suggestions.action_cursor_down()
        else:
            super().action_cursor_down(select)

    def action_line_break(self) -> None:
        self.insert('\n')

class Picker(ModalScreen[str | None]):
    BINDINGS = [('escape', 'dismiss(None)', 'Close')]

    def __init__(self, title: str, items: list[tuple[str, str]], hint: str = '') -> None:
        super().__init__()
        self.heading, self.items, self.hint = title, items, hint

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Static(self.heading, id='dialog-title', markup=False)
            yield Input(placeholder='Search…', id='filter')
            yield OptionList(*(Option(label, id=key) for key, label in self.items), id='choices', markup=False)
            yield Static(self.hint or '↑↓ navigate   enter select   esc close', id='dialog-hint', markup=False)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.casefold()
        choices = self.query_one(OptionList)
        choices.clear_options()
        choices.add_options(Option(label, id=key) for key, label in self.items if query in label.casefold())
        if choices.option_count:
            choices.highlighted = 0

    def on_key(self, event) -> None:
        if event.key in ('up', 'down') and self.query_one(Input).has_focus:
            choices = self.query_one(OptionList)
            choices.action_cursor_down() if event.key == 'down' else choices.action_cursor_up()
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(OptionList).action_select()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class SecretInput(Input):
    @property
    def _value(self) -> Text:
        # Keep one cell per character so cursor movement and editing still work.
        return Text('*' * len(self.value), no_wrap=True, overflow='ignore', end='')


class EntryDialog(ModalScreen[str | None]):
    BINDINGS = [('escape', 'dismiss(None)', 'Close')]

    def __init__(self, title: str, value: str = '', hint: str = '', secret: bool = False) -> None:
        super().__init__()
        self.heading, self.value, self.hint, self.secret = title, value, hint, secret

    def compose(self) -> ComposeResult:
        with Vertical(id='entry-dialog'):
            yield Static(self.heading, id='dialog-title', markup=False)
            field = SecretInput if self.secret else Input
            yield field(value=self.value, password=self.secret, placeholder='API key' if self.secret else 'Repository path', id='entry')
            yield Static(self.hint, id='dialog-hint', markup=False)

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())
