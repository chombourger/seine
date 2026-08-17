# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Prompt widget and shared screen base: history, completion, the '!'
# escape, command registry. Split from app.py to avoid an app.py <->
# build.py import cycle.

from textual.app import Screen
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
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

# The '@fragment' completion pane. Never focused -- Prompt drives
# Up/Down/Enter into it directly -- and hidden when there's nothing to
# offer.
class PathCompletions(OptionList):
    can_focus = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.display = False
        self._matches = []

    # No-op on identical matches -- rebuilding resets 'highlighted' to
    # 0, which would stomp the entry Up/Down just selected.
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

class Prompt(Input):
    BINDINGS = Input.BINDINGS + [
        Binding("up", "history_prev", "history", show=False),
        Binding("down", "history_next", "history", show=False),
    ]

    def __init__(self, history, completions, **kwargs):
        super().__init__(
            placeholder="/command (/help lists them, ! for shell, @ for a path)",
            suggester=CommandSuggester(),
            # Input's default selects the whole value on focus; wrong
            # for a prompt regaining focus mid-edit, like a shell prompt.
            select_on_focus=False,
            **kwargs)
        self.history = history
        self.completions = completions
        # Last history string recalled verbatim, so submit can tell
        # "recalled unedited" from "edited/typed" -- only the latter
        # belongs back in history.
        self.recalled = None

    # While the '@' pane is open, Up/Down/Enter move its selection
    # instead of history/recall -- one interaction, not three.
    def action_history_prev(self):
        if self.completions.display:
            self.completions.action_cursor_up()
            return
        line = self.history.prev()
        if line is not None:
            self.value = line
            self.cursor_position = len(line)
            self.recalled = line

    def action_history_next(self):
        if self.completions.display:
            self.completions.action_cursor_down()
            return
        self.value = self.history.next() or ""
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
    # updates synchronously but the pane's own Changed handler lags a
    # tick, so fast Enter must re-sync here rather than trust a stale
    # pane.
    def _sync_completions(self):
        active = self._active_fragment()
        if active is None:
            self.completions.hide()
            return
        _, fragment = active
        matches = complete(fragment)
        # A single match equal to what's already typed has nothing
        # left to offer -- don't dangle it in front of Enter.
        if matches == [fragment]:
            matches = []
        self.completions.show(matches)

    # Re-lists matches on every keystroke, like shell completion.
    def on_input_changed(self, event):
        if self.recalled is not None and self.value != self.recalled:
            self.recalled = None
        self._sync_completions()

    # Enter accepts the highlighted completion if the pane is showing;
    # re-syncs synchronously first since a fast 'exa<Enter>' can
    # otherwise beat the pane's own Changed handler. Falls through to a
    # real submit otherwise.
    async def action_submit(self):
        self._sync_completions()
        try:
            showing = self.completions.display and self.completions.option_count > 0
        except Exception:
            showing = False
        if showing and self._accept_completion():
            return
        await super().action_submit()

    # Broad except: best-effort UI sugar, not build-critical -- any
    # stale-OptionList exception here means "nothing to accept", not a
    # crash.
    def _accept_completion(self):
        active = self._active_fragment()
        if active is None:
            self.completions.hide()
            return False
        # get_option_at_index() + highlighted, not the
        # highlighted_option property -- missing in Debian trixie's
        # python3-textual (2.1.2).
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

class BaseScreen(Screen):
    # Status-line chips, keyed for subclasses to update/add/remove
    # rather than redefining HINT wholesale -- hand-typed copies used
    # to drift (FilesystemScreen once silently lost its own
    # '→ complete' chip this way).
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

    # '/' refocuses the prompt from elsewhere; not a priority binding,
    # so a focused Input consumes it as a normal keystroke instead.
    BINDINGS = [Binding("/", "focus_prompt", show=False)]

    # An empty prompt gets the summoning '/' preloaded; a prompt with
    # existing text is only focused, left untouched.
    def action_focus_prompt(self):
        prompt = self.query_one(Prompt)
        if not prompt.value:
            prompt.value = "/"
            prompt.cursor_position = len(prompt.value)
        prompt.focus()

    # Spec tree on the left, screen content on the right. BuildScreen
    # overrides compose() outright for its own layout.
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            # markup=False: arbitrary spec/diff text -- a YAML list
            # like 'partitions: [a, b]' would else be read as a markup
            # tag.
            StaticPane(Static(id="body", markup=False), id="cmd"),
            id="main",
        )
        yield from self.footer()

    # Completions pane, prompt, status/indicators/hint in that order --
    # status stays under the prompt, like a shell's own line.
    def footer(self):
        completions = PathCompletions(id="completions")
        yield completions
        yield Prompt(self.app.history, completions, id="prompt")
        # markup=False: same as #body -- engine text (a path, an argv,
        # an exception message) can contain a bare '['.
        yield Static(id="status", markup=False)
        yield Indicators(id="indicators")
        yield Static(self.HINT, id="hint")

    def on_mount(self):
        self.refresh_data()
        self.query_one(Prompt).focus()
        # Ticks on every screen, not only BuildScreen's own (which has a
        # separate, faster tick for #tail/#tasklist), so a build kept
        # running still lights up wherever the spec tree currently is.
        self._scrolled_to = None
        self.set_interval(1.0, self._tick_highlight)
        self._tick_highlight()

    def _tick_highlight(self):
        tree = self.query_one(SpecTree)
        wanted = spectree.highlight_active(tree, self.app.build_state)
        self._scrolled_to = spectree.scroll_to_active(tree, wanted, self._scrolled_to)

    # Kept on the base class so every subclass gets it for free on
    # mount and after /use, rather than repeating the call. Subclasses
    # override update_body(), not this.
    def refresh_data(self):
        self.query_one(SpecTree).load(
            self.app.context, previous_spec=self.app.context.extended_from)
        self.query_one(Indicators).refresh_text()
        self.update_body()

    # Overridden by each screen: what goes in the body ('#cmd') pane.
    def update_body(self):
        pass

    # CSS class, not markup, for the same reason #status is markup=False.
    def say(self, text, error=False):
        status = self.query_one("#status", Static)
        status.set_class(error, "error")
        status.update(text)

    async def on_input_submitted(self, event):
        line = event.value
        event.input.value = ""
        if not line.strip():
            return
        # /extend's highlight is one-shot: any other prompt input
        # clears it. Checked before dispatch, so a fresh /extend's own
        # highlight isn't wiped right back out.
        context = self.app.context
        if context.extended_from is not None:
            context.extended_from = None
            self.query_one(SpecTree).load(context)
        # An unmodified recall is already in history; only a new/edited
        # line is worth adding.
        if event.input.recalled != line:
            self.app.history.add(line)
        if line.startswith("!"):
            self.app.shell_escape(line[1:])
            return
        # Neither a command nor shell: a question for the AI chat, once
        # it's configured -- '/' still means "run a command" either way,
        # so an unconfigured typo gets the usual dispatch() error, not a
        # confusing "AI isn't set up" instead.
        if not line.startswith("/"):
            from seine.tui import ai
            if ai.configured():
                ai.ask(self.app, line)
                return
        try:
            commands.dispatch(self.app, line)
        except commands.CommandError as e:
            self.say(str(e), error=True)
