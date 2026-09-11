# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Prompt widget and shared screen base: history, completion, the '!'
# escape, command registry. Split from app.py to avoid an app.py <->
# build.py import cycle.

from textual.app import Screen
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.suggester import Suggester
from textual.widgets import Input, OptionList, Static

from seine.tui import commands, spectree
from seine.tui.paths import complete
from seine.tui.spectree import SpecTree

# Suggests command names, then flags once one is picked. Only for
# '/'-prefixed input -- unprefixed text never dispatches as a command.
class CommandSuggester(Suggester):
    async def get_suggestion(self, value):
        if not value.startswith("/"):
            return None
        body = value[1:]
        if not body:
            return None
        if " " not in body:
            for name in commands.REGISTRY:
                if name != "q" and name.startswith(body):
                    return "/" + name
            return None
        head, _, tail = body.rpartition(" ")
        options = commands.OPTIONS.get(body.split(" ", 1)[0], [])
        if tail.startswith("--") and len(tail) > 2:
            fragment = tail[2:].rstrip("=")
            for option in options:
                name = option.rstrip("=")
                if name.startswith(fragment):
                    return "/%s --%s%s" % (head, name, "=" if option.endswith("=") else "")
        return None

# The '@fragment' completion pane. Never focused: Prompt drives
# Up/Down/Enter into it directly. Hidden when there's nothing to offer.
class PathCompletions(OptionList):
    can_focus = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False
        self._matches = []

    # No-op on identical matches: rebuilding would reset 'highlighted'
    # to 0, stomping the entry Up/Down just selected.
    def show(self, matches):
        if len(matches) == 0:
            self.hide()
            return
        if self.display and matches == self._matches:
            return
        self._matches = matches
        self.clear_options()
        self.add_options(matches)
        self.highlighted = 0
        self.display = True

    def hide(self):
        self.display = False
        self._matches = []
        self.clear_options()

# The prompt's placeholder when nothing special is going on --
# app.py swaps in a progress line while startup commands run, then
# restores this.
DEFAULT_PLACEHOLDER = "/command (/help lists them, ! for shell, @ for a path)"

class Prompt(Input):
    BINDINGS = Input.BINDINGS + [
        Binding("up", "history_prev", "history", show=False),
        Binding("down", "history_next", "history", show=False),
    ]

    def __init__(self, history, completions, scopes, **kwargs):
        super().__init__(
            placeholder=DEFAULT_PLACEHOLDER,
            suggester=CommandSuggester(),
            # Input's default selects the whole value on focus; wrong
            # for a prompt regaining focus mid-edit, like a shell prompt.
            select_on_focus=False,
            **kwargs)
        self.history = history
        self.completions = completions
        # Which history.py scopes this screen's Up/Down walks, e.g.
        # {'commands', 'chat'} here, {'commands', 'target'} on the
        # Remote Target screen. This cursor is per-Prompt, not shared,
        # since different screens walk different scope sets.
        self.scopes = scopes
        self.at = len(history.entries(scopes))
        # Last history string recalled verbatim, so submit can tell
        # "recalled unedited" from "edited/typed" -- only the latter
        # belongs back in history.
        self.recalled = None

    # While the '@' pane is open, Up/Down/Enter move its selection
    # instead of history/recall.
    def action_history_prev(self):
        if self.completions.display:
            self.completions.action_cursor_up()
            return
        entries = self.history.entries(self.scopes)
        if self.at > 0:
            self.at -= 1
        line = entries[self.at]["line"] if self.at < len(entries) else None
        if line is not None:
            self.value = line
            self.cursor_position = len(line)
            self.recalled = line

    def action_history_next(self):
        if self.completions.display:
            self.completions.action_cursor_down()
            return
        entries = self.history.entries(self.scopes)
        if self.at < len(entries):
            self.at += 1
        self.value = entries[self.at]["line"] if self.at < len(entries) else ""
        self.cursor_position = len(self.value)
        self.recalled = self.value

    # The '@fragment' token under the cursor, if any.
    def _active_fragment(self):
        before_cursor = self.value[:self.cursor_position]
        at = before_cursor.rfind("@")
        if at == -1:
            return None
        fragment = before_cursor[at + 1:]
        if " " in fragment or "\t" in fragment:
            return None
        return at, fragment

    # Called from both on_input_changed and action_submit: self.value
    # updates synchronously but the pane's Changed handler lags a tick,
    # so a fast Enter must re-sync here instead of trusting a stale pane.
    def _sync_completions(self):
        active = self._active_fragment()
        if active is None:
            self.completions.hide()
            return
        _, fragment = active
        matches = complete(fragment)
        # A single match equal to what's already typed offers nothing.
        if matches == [fragment]:
            matches = []
        self.completions.show(matches)

    # Re-lists matches on every keystroke, like shell completion.
    def on_input_changed(self, event):
        if self.recalled is not None and self.value != self.recalled:
            self.recalled = None
        self._sync_completions()

    # Enter accepts the highlighted completion if the pane is showing;
    # re-syncs first since a fast 'exa<Enter>' can otherwise beat the
    # pane's Changed handler. Falls through to a real submit otherwise.
    async def action_submit(self):
        self._sync_completions()
        try:
            showing = self.completions.display and self.completions.option_count > 0
        except Exception:
            showing = False
        if showing and self._accept_completion():
            return
        await super().action_submit()

    # Broad except: best-effort UI sugar, not build-critical. Any
    # stale-OptionList exception here just means "nothing to accept".
    def _accept_completion(self):
        active = self._active_fragment()
        if active is None:
            self.completions.hide()
            return False
        # get_option_at_index() + highlighted, not highlighted_option:
        # missing in Debian trixie's python3-textual (2.1.2).
        try:
            index = self.completions.highlighted
            option = None if index is None else self.completions.get_option_at_index(index)
            replacement = str(option.prompt) if option is not None else None
        except Exception:
            replacement = None
        if replacement is None:
            self.completions.hide()
            return False
        at, _ = active
        before = self.value[:at]
        after = self.value[self.cursor_position:]
        self.value = "%s@%s %s" % (before, replacement, after)
        self.cursor_position = len(before) + 1 + len(replacement) + 1
        self.completions.hide()
        return True

# Unfocusable: VerticalScroll is focusable by default, which would add
# a third Tab stop beside prompt and spec tree.
class StaticPane(VerticalScroll):
    can_focus = False

# Persistent "N build" status chip, clickable to jump to the Build
# screen; hidden when idle. A count rather than a boolean since a
# future multiconfig build could run more than one at once.
class Indicators(Static):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False

    def on_click(self, event):
        if self.app.build_state.running:
            self.app.show("build")

    # Uses state.done, not BuildState.running: Worker.is_running can
    # still read True one tick before the worker actually finishes.
    def refresh_text(self):
        state = self.app.build_state
        count = 1 if (state.worker is not None and not state.done) else 0
        if count == 0:
            self.display = False
            return
        self.update("%d build%s" % (count, "" if count == 1 else "s"))
        self.display = True

# Same role as Indicators, for a vendor run. A separate widget rather
# than folded in: a vendor run and a real build never run at once, so
# there's nothing to count -- just running or not.
class VendorIndicator(Static):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False

    def on_click(self, event):
        if self.app.vendor_state.running:
            self.app.show("vendor")

    # See Indicators.refresh_text(): same is_running/done caveat.
    def refresh_text(self):
        state = self.app.vendor_state
        if state.worker is None or state.done:
            self.display = False
            return
        self.update("vendoring")
        self.display = True

# Same role as Indicators, for a target storage write. Shows live
# progress since there's only ever one target. Click switches to the
# Remote Target screen for a closer look.
class TargetIndicator(Static):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False

    def on_click(self, event):
        commands.dispatch(self.app, "/target")

    def refresh_text(self):
        state = self.app.target_state
        if not state.writing or state.write_total <= 0:
            self.display = False
            return
        percent = int(state.write_read * 100 / state.write_total)
        self.update("writing image %d%%" % percent)
        self.display = True

class BaseScreen(Screen):
    # Without this, Textual's default auto-focus would land on
    # TargetScreen's ConsolePane (composed before the prompt) and fire
    # its on_focus() before anyone asked for it.
    AUTO_FOCUS = "#prompt"

    # Which history.py scopes this screen's Prompt recalls with
    # Up/Down. 'commands' is universal; TargetScreen swaps 'chat' for
    # 'target' (its own in-memory console lines).
    HISTORY_SCOPES = {"commands", "chat"}

    # Status-line chips, keyed for subclasses to update/add/remove
    # rather than redefining HINT wholesale.
    HINT_CHIPS = [
        ("command",  "/command"),
        ("shell",    "! shell"),
        ("tab",      "Tab switch pane"),
        ("complete", "→ complete"),
        ("palette",  "Ctrl+P palette"),
        ("quit",     "/quit"),
    ]

    # Per-screen deltas applied on top of HINT_CHIPS:
    #  - HINT_UPDATE: key -> replacement text, same position ('tab'
    #    stays generic since panes vary per screen).
    #  - HINT_ADD: (after_key, key, text), splices after after_key
    #    (None appends at the end).
    #  - HINT_REMOVE: keys to drop.
    HINT_UPDATE = {}
    HINT_ADD = []
    HINT_REMOVE = set()

    @property
    def HINT(self):
        chips = [(k, self.HINT_UPDATE.get(k, t)) for k, t in BaseScreen.HINT_CHIPS]
        for after, key, text in self.HINT_ADD:
            index = (len(chips) if after is None else
                     next(i for i, (k, _) in enumerate(chips) if k == after) + 1)
            chips.insert(index, (key, text))
        return " · ".join(t for k, t in chips if k not in self.HINT_REMOVE)

    # '/' refocuses the prompt; not a priority binding, so a focused
    # Input still consumes it as a normal keystroke.
    BINDINGS = [Binding("/", "focus_prompt", show=False)]

    # An empty prompt gets the summoning '/' preloaded; existing text
    # is left untouched.
    def action_focus_prompt(self):
        prompt = self.query_one(Prompt)
        if not prompt.value:
            prompt.value = "/"
            prompt.cursor_position = len(prompt.value)
        prompt.focus()

    # Spec tree on the left, screen content on the right. BuildScreen
    # overrides compose() for its own layout.
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            # markup=False: a YAML list like 'partitions: [a, b]' would
            # else be read as a markup tag.
            StaticPane(Static(id="body", markup=False), id="cmd"),
            id="main",
        )
        yield from self.footer()

    # Completions pane, prompt, status/indicators/hint in that order,
    # status under the prompt like a shell's own line.
    def footer(self):
        completions = PathCompletions(id="completions")
        yield completions
        yield Prompt(self.app.history, completions, self.HISTORY_SCOPES, id="prompt")
        # markup=False: engine text (a path, an argv, an exception
        # message) can contain a bare '['.
        yield Horizontal(
            Static(id="status", markup=False),
            Indicators(id="indicators"),
            VendorIndicator(id="vendor-indicator"),
            TargetIndicator(id="target-indicator"),
            id="infobar",
        )
        yield Static(self.HINT, id="hint")

    def on_mount(self):
        self.refresh_data()
        prompt = self.query_one(Prompt)
        # A screen mounted by a startup command itself (/cache pushes
        # CacheScreen mid-startup): its prompt starts shut like the
        # first screen's, instead of taking focus with live input.
        if getattr(self.app, "_running_startup", False):
            prompt.disabled = True
        else:
            prompt.focus()
        # Ticks on every screen so a running build stays highlighted
        # wherever the spec tree currently is (BuildScreen has its own
        # faster tick for #tail/#tasklist).
        self._scrolled_to = None
        self.set_interval(1.0, self._tick_highlight)
        self._tick_highlight()

    # NoMatches guard: TargetScreen has no #spectree.
    def _tick_highlight(self):
        try:
            tree = self.query_one(SpecTree)
        except NoMatches:
            return
        wanted = spectree.highlight_active(tree, self.app.build_state)
        wanted |= spectree.highlight_active_test(tree, self.app.test_state)
        self._scrolled_to = spectree.scroll_to_active(tree, wanted, self._scrolled_to)

    # Kept on the base class so every subclass gets it for free.
    # Subclasses override update_body(), not this. NoMatches throughout:
    # worker callbacks can land mid-transition, when half the widgets
    # aren't composed yet -- a missed repaint is fine, a crash is not.
    def refresh_data(self):
        try:
            try:
                tree = self.query_one(SpecTree)
            except NoMatches:
                pass
            else:
                tree.load(self.app.context, previous_spec=self.app.context.changed_from)
            self.query_one(Indicators).refresh_text()
            self.query_one(VendorIndicator).refresh_text()
            self.query_one(TargetIndicator).refresh_text()
            self.update_body()
        except NoMatches:
            pass

    # Overridden by each screen: what goes in the body ('#cmd') pane.
    def update_body(self):
        pass

    def say(self, text, error=False, warning=False):
        # Bumps the copy-notice token so a pending copy hint's delayed
        # clear can't wipe a newer message (e.g. an error landing right
        # after a copy). on_mouse_up() sets the token after its own
        # say(), so its timer still matches until the next say().
        self._copy_notice_token = getattr(self, "_copy_notice_token", 0) + 1
        status = self.query_one("#status", Static)
        status.set_class(error, "error")
        status.set_class(warning, "warning")
        status.update(text)

    # Drag-selecting any text copies it to the clipboard on mouse
    # release, with a transient reminder in the status line (the
    # dynamic footer). A plain click selects nothing -- Textual clears
    # the selection before this bubbles -- so it stays a no-op. The
    # token guards the delayed clear against a newer message landing
    # in the meantime (an error right after a copy must not be wiped).
    def on_mouse_up(self, event):
        text = None
        try:
            text = self.get_selected_text()
        except Exception:
            text = None
        if not text or not text.strip():
            # Prompt/Input keeps its own selection apart from the
            # screen's -- selecting prompt text must copy too.
            try:
                for widget in self.query(Input):
                    try:
                        selected = widget.selected_text
                    except Exception:
                        continue
                    if selected and selected.strip():
                        text = selected
                        break
            except Exception:
                pass
        if not text or not text.strip():
            return
        try:
            self.app.copy_to_clipboard(text)
        except Exception:
            return
        count = len(text)
        self.say("copied %d character%s to clipboard" % (count, "" if count == 1 else "s"))
        token = getattr(self, "_copy_notice_token", 0) + 1
        self._copy_notice_token = token

        def _clear(expected=token):
            if getattr(self, "_copy_notice_token", None) == expected:
                try:
                    self.say("")
                except Exception:
                    pass

        self.set_timer(2.5, _clear)

    async def on_input_submitted(self, event):
        line = event.value
        event.input.value = ""
        if not line.strip():
            return
        # /side-load's (or /side-unload's) highlight is one-shot: any
        # other prompt input clears it. Checked before dispatch so a
        # fresh highlight isn't wiped right back out.
        context = self.app.context
        if context.changed_from is not None:
            context.changed_from = None
            try:
                self.query_one(SpecTree).load(context)
            except NoMatches:
                pass
        # An unmodified recall is already in history; only a new/edited
        # line is worth adding. Resets the cursor to "just past the
        # newest" so the next Up shows what was just added.
        if event.input.recalled != line:
            self._history_add(line)
            event.input.at = len(self.app.history.entries(event.input.scopes))
        if line.startswith("!"):
            self.app.shell_escape(line[1:])
            return
        # Neither a command nor shell: overridable, so a screen with
        # something better to do with a bare line (TargetScreen sends
        # it to the target's console) can.
        if not line.startswith("/") and self._handle_freeform(line):
            return
        try:
            commands.dispatch(self.app, line)
        except commands.CommandError as e:
            self.say(str(e), error=True)

    # 'commands' for a '/command' or '!shell' line, 'chat' for anything
    # else. Overridden by TargetScreen so its freeform console lines go
    # into an in-memory-only record instead, never written to disk.
    def _history_add(self, line):
        scope = "commands" if line.startswith(("/", "!")) else "chat"
        self.app.history.add(line, scope=scope)

    # A question for the AI chat, once configured. Returns whether the
    # line was handled, so on_input_submitted() knows whether to fall
    # through.
    def _handle_freeform(self, line):
        from seine.tui import ai
        if ai.configured():
            ai.ask(self.app, line)
            return True
        return False
