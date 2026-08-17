# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# /settings: a modal overlay, same shape as /help. Two focusable lists,
# Tab-cycled: GeneralSettings for jobs/theme, StartupCommands for the
# list. jobs and startup commands edit through '#editrow' (Input),
# pre-filled, empty submit clearing the row; theme is a closed choice
# (commands.THEMES) so it gets ThemePicker instead. Del clears the
# highlighted row on either list.

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from seine import settings

ADD_LABEL = "+ add command…"

HINT = "Tab switch · Up/Down move · Enter edit · Del clear · Esc close"

# refresh_from() reuses render_settings()'s own lines rather than
# formatting each setting a second way; KEYS maps a row index back to
# which setting it was.
class GeneralSettings(OptionList):
    KEYS = ["jobs", "theme", "llm_model", "llm_api_base"]

    def refresh_from(self):
        from seine.tui.render import render_settings
        previous = self.highlighted
        self.clear_options()
        for line in render_settings().splitlines():
            self.add_option(line)
        self.highlighted = min(previous, self.option_count - 1) if previous is not None else 0

    def key_at(self, index):
        return self.KEYS[index] if index is not None and 0 <= index < len(self.KEYS) else None

# One row per startup command, plus an always-last, dimmed "add" row --
# never a separate key to remember for "add a new one", and never
# ambiguous about which row that is.
class StartupCommands(OptionList):
    def refresh_from(self, lines):
        previous = self.highlighted
        self.clear_options()
        for line in lines:
            self.add_option(line)
        self.add_option(Text(ADD_LABEL, style="dim italic"))
        self.highlighted = min(previous, self.option_count - 1) if previous is not None else 0

    def is_placeholder(self, index):
        return index == self.option_count - 1

# Reads commands.THEMES directly rather than a second, parallel list,
# so /set theme and this can never drift apart.
class ThemePicker(OptionList):
    def refresh_from(self, current_value):
        from seine.tui.commands import THEMES
        self.clear_options()
        names = list(THEMES)
        for name in names:
            self.add_option(name)
        self.highlighted = names.index(current_value) if current_value in names else 0

# OptionList never highlights a row on its own -- clear_options() leaves
# highlighted None, so every list above sets it explicitly, clamped to
# where it already was.

class SettingsScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss",       show=False),
        Binding("delete", "clear_selected", show=False),
    ]

    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    #settingspane {
        width: 70%; height: 70%;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #settingstitle { color: blue; text-style: bold; }
    #generallabel { text-style: bold; }
    #general { height: 6; border: round $border-blurred; }
    #general:focus { border: round $border; }
    #startuplabel { text-style: bold; padding-top: 1; }
    #startup { height: 1fr; border: round $border-blurred; }
    #startup:focus { border: round $border; }
    #editlabel { text-style: bold; padding-top: 1; }
    #themepicker { height: 4; border: round $border-blurred; }
    #themepicker:focus { border: round $border; }
    #settingshint { color: $text-muted; padding-top: 1; height: auto; }
    """

    def __init__(self):
        super().__init__()
        # (section, index) of the row Enter opened an editor for, or
        # None while a list has focus.
        self._editing = None

    def compose(self):
        with Vertical(id="settingspane"):
            yield Static("Settings", id="settingstitle")
            yield Static("GENERAL", id="generallabel")
            yield GeneralSettings(id="general")
            yield Static("STARTUP COMMANDS", id="startuplabel")
            yield StartupCommands(id="startup")
            yield Static(id="editlabel")
            yield Input(id="editrow")
            yield ThemePicker(id="themepicker")
            yield Static(HINT, id="settingshint")

    def on_mount(self):
        self._redraw(focus="general")

    def _editing_key(self):
        if self._editing is None or self._editing[0] != "general":
            return None
        return self.query_one(GeneralSettings).key_at(self._editing[1])

    # 'focus' names which list gets focus back once editing is done;
    # None leaves focus alone. Resets the hint line to HINT -- the one
    # caller that wants a message to stay up (_edit_error()) skips this.
    def _redraw(self, focus=None):
        self.query_one(GeneralSettings).refresh_from()
        self.query_one(StartupCommands).refresh_from(settings.load()["startup_commands"])
        self.query_one("#settingshint", Static).update(HINT)
        editing = self._editing is not None
        theme_edit = self._editing_key() == "theme"
        self.query_one("#editlabel", Static).display = editing
        self.query_one("#editrow", Input).display = editing and not theme_edit
        self.query_one(ThemePicker).display = theme_edit
        self.query_one(GeneralSettings).display = not editing
        self.query_one(StartupCommands).display = not editing
        if theme_edit:
            self.query_one(ThemePicker).focus()
        elif editing:
            self.query_one("#editrow", Input).focus()
        elif focus == "general":
            self.query_one(GeneralSettings).focus()
        elif focus == "startup":
            self.query_one(StartupCommands).focus()

    # Esc backs out one level at a time: an edit in progress first (back
    # to whichever list it came from, unchanged), only then the whole
    # screen -- same two-step 'HelpScreen.action_dismiss()' already uses.
    def action_dismiss(self):
        if self._editing is not None:
            section, _ = self._editing
            self._editing = None
            self._redraw(focus=section)
            return
        self.app.pop_screen()

    # Input claims 'delete' itself while focused, so this only ever
    # runs while a list has focus -- has_focus below picks which one.
    def action_clear_selected(self):
        if self._editing is not None:
            return
        general = self.query_one(GeneralSettings)
        startup = self.query_one(StartupCommands)
        if general.has_focus:
            self._clear_general(general.highlighted)
        elif startup.has_focus:
            self._clear_startup(startup.highlighted)

    def _clear_general(self, index):
        key = self.query_one(GeneralSettings).key_at(index)
        if key is None:
            return
        current = settings.load()
        current[key] = None
        settings.save(current)
        self._redraw(focus="general")

    def _clear_startup(self, index):
        startup = self.query_one(StartupCommands)
        if index is None or startup.is_placeholder(index):
            return
        current = settings.load()
        del current["startup_commands"][index]
        settings.save(current)
        self._redraw(focus="startup")

    # Enter on a row: 'theme' opens #themepicker (a closed choice);
    # everything else opens #editrow pre-filled with its current text.
    # #editlabel says which setting either way.
    def on_option_list_option_selected(self, event):
        if event.option_list.id == "themepicker":
            from seine.tui.commands import THEMES
            self._commit_theme(list(THEMES)[event.option_index])
            return
        section = "general" if event.option_list.id == "general" else "startup"
        index = event.option_index
        self._editing = (section, index)
        if section == "general" and self.query_one(GeneralSettings).key_at(index) == "theme":
            current_theme = settings.load()["theme"] or "dark"
            self.query_one(ThemePicker).refresh_from(current_theme)
            self.query_one("#editlabel", Static).update("theme")
            self._redraw()
            return
        editrow = self.query_one("#editrow", Input)
        if section == "general":
            key = self.query_one(GeneralSettings).key_at(index)
            value = settings.load()[key]
            editrow.value = str(value) if value is not None else ""
            label = key
        else:
            lines = settings.load()["startup_commands"]
            editing_add_row = index >= len(lines)
            editrow.value = "" if editing_add_row else lines[index]
            label = "new startup command" if editing_add_row else "startup command"
        self.query_one("#editlabel", Static).update(label)
        editrow.cursor_position = len(editrow.value)
        self._redraw()

    def on_input_submitted(self, event):
        section, index = self._editing
        value = event.value.strip()
        if section == "general":
            self._commit_general(index, value)
        else:
            self._commit_startup(index, value)

    # 'theme' is picked, not typed, so never reaches this. 'jobs' is
    # validated (a bad value leaves #editrow open to fix); llm_model/
    # llm_api_base are free text, litellm's to judge, not this screen's.
    def _commit_general(self, index, value):
        key = self.query_one(GeneralSettings).key_at(index)
        current = settings.load()
        if not value:
            current[key] = None
        elif key == "jobs":
            try:
                jobs = int(value)
            except ValueError:
                self._edit_error("jobs expects a number")
                return
            if jobs < 1:
                self._edit_error("jobs shall be at least 1")
                return
            current[key] = jobs
        else:
            current[key] = value
        settings.save(current)
        self._editing = None
        self._redraw(focus="general")

    def _commit_theme(self, value):
        from seine.tui.commands import THEMES
        current = settings.load()
        current["theme"] = value
        settings.save(current)
        self.app.theme = THEMES[value]
        self._editing = None
        self._redraw(focus="general")

    # Empty text on a real row clears it, non-empty updates it; empty on
    # the "add" row is a no-op, non-empty appends one and the
    # placeholder reappears at the end.
    def _commit_startup(self, index, value):
        current = settings.load()
        lines = current["startup_commands"]
        if index < len(lines):
            if value:
                lines[index] = value
            else:
                del lines[index]
        elif value:
            lines.append(value)
        settings.save(current)
        self._editing = None
        self._redraw(focus="startup")

    def _edit_error(self, message):
        self.query_one("#settingshint", Static).update(message)
