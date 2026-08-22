# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The TUI application: Overview/Plan/Build screens over one prompt, a
# command registry shared with Tab completion and the command palette,
# and a '!' shell escape. Everything is read from the engine on open --
# no separate TUI model, beyond build_state which outlives its screen.

import os
import subprocess

from textual import command
from textual.app import App
from textual.css.query import NoMatches
from textual.widgets import Static

from seine.tui import ai, commands
from seine.tui.base import BaseScreen, Indicators, Prompt, TargetIndicator
from seine.tui.build import BuildScreen, BuildState
from seine.tui.chat import ChatScreen
from seine.tui.context import Context
from seine.tui.filesystem import FilesystemScreen, FilesystemState
from seine.tui.history import History
from seine.tui.issues import IssuesScreen
from seine.tui.render import (render_analyze, render_artifacts, render_cache,
                              render_doctor, render_overview, render_packages,
                              render_plan)
from seine.tui.target import TargetState
from seine.tui.target_screen import TargetScreen

class OverviewScreen(BaseScreen):
    # Not reported from SeineApp.on_mount(): push_screen() schedules this
    # screen's mount rather than composing it inline, so the status bar
    # isn't there to query yet at that point.
    def on_mount(self):
        super().on_mount()
        if self.app._startup_error:
            self.say(self.app._startup_error, error=True)

    def update_body(self):
        self.query_one("#body", Static).update(render_overview(self.app.context))

class PlanScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_plan(self.app.context))

class ArtifactsScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_artifacts(self.app.context))

class PackagesScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_packages(self.app.context))

class AnalyzeScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_analyze(self.app.context))

# Not spec-scoped -- a cache and the environment are shared by every
# build, so these read nothing from 'self.app.context'.
class CacheScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_cache())

class DoctorScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_doctor())

# What /diff last computed -- not spec-scoped, so reads app.diff_text
# rather than app.context.
class DiffScreen(BaseScreen):
    def update_body(self):
        text = self.app.diff_text or (
            "no diff yet -- '/diff OLD.spdx.json NEW.spdx.json'\n")
        self.query_one("#body", Static).update(text)

SCREENS = {"overview": OverviewScreen, "plan": PlanScreen, "build": BuildScreen,
          "artifacts": ArtifactsScreen, "filesystem": FilesystemScreen,
          "packages": PackagesScreen, "analyze": AnalyzeScreen,
          "cache": CacheScreen, "doctor": DoctorScreen, "diff": DiffScreen,
          "issues": IssuesScreen, "chat": ChatScreen, "target": TargetScreen}

# Offers the same command registry through Ctrl+P. Selecting one fills the
# prompt and focuses it rather than running it -- a second, deliberate
# Enter in the prompt itself is what actually runs a command.
class RegistryProvider(command.Provider):
    async def discover(self):
        for c in commands.REGISTRY.values():
            yield command.DiscoveryHit(c.name, self._fill(c.name), help=c.help)

    async def search(self, query):
        matcher = self.matcher(query)
        for c in commands.REGISTRY.values():
            score = matcher.match(c.name)
            if score > 0:
                yield command.Hit(score, matcher.highlight(c.name),
                                  self._fill(c.name), help=c.help)

    def _fill(self, name):
        def callback():
            prompt = self.app.screen.query_one(Prompt)
            prompt.value = "/" + name + " "
            prompt.cursor_position = len(prompt.value)
            prompt.focus()
        return callback

class SeineApp(App):
    TITLE = "seine"
    COMMANDS = App.COMMANDS | {RegistryProvider}
    CSS = """
    #main, #buildrow { height: 1fr; }
    /* 'round', not Input's own default 'tall': 'tall' draws with
       eighth-block glyphs some real terminal fonts (Monaco/iTerm2) lack,
       breaking the border. 'round' is plain box-drawing every font has. */
    #spectree, #tail { width: 2fr; height: 100%; }
    #prompt, #spectree, #tail, #fslist, #previewpane { border: round $foreground 40%; }
    #prompt:focus, #spectree:focus, #tail:focus, #fslist:focus, #previewpane:focus {
        border: round $border;
    }
    #cmd, #tasks { width: 1fr; height: 100%; border: round $foreground 40%; }
    #body { padding: 1 2; }
    #tasklist { padding: 1 2; }
    #tail { padding: 0 1; }
    #fslist { height: 1fr; }
    #previewpane { height: 1fr; padding: 1 2; }
    #hint { color: $text-muted; padding: 0 2; }
    #infobar { height: 1; }
    #status { padding: 0 2; height: 1; width: 1fr; }
    #status.error { color: $error; }
    #status.warning { color: $warning; }
    /* '$accent', not '$text-muted' like '#hint' -- this is clickable
       and worth noticing, closer to a link than a caption. */
    #indicators { color: $accent; padding: 0 2; height: 1; width: auto; }
    #target-indicator { color: $accent; padding: 0 2; height: 1; width: auto; }
    #completions {
        height: auto;
        max-height: 5;
        border: none;
        margin: 0 2;
    }
    #completions > .option-list--option-highlighted {
        background: $accent;
        color: $text;
    }
    """

    def __init__(self, files=None):
        super().__init__()
        self.context = Context()
        self.history = History()
        self.build_state = BuildState()
        # "N build" chip's finish edge, wired for the App's own lifetime
        # so it updates regardless of which screen is open. The start
        # edge is start_build() calling refresh_indicators() directly.
        self.build_state.on_finished = self._build_finished
        self.fs_state = FilesystemState()
        self.ai_state = ai.AIState()
        self.target_state = TargetState()
        self.diff_text = None
        # Set by commands.py's own _issues() right before app.show("issues")
        # -- IssuesScreen.update_body() reads these back, the same
        # app-state-then-show() shape diff_text/_diff() above already uses.
        self.issues_filter = None
        self.issues_min_urgency = None
        self.issues_rescan = False
        self._startup_error = None
        # No spec at all, not a bad one -- a spec given but failed to
        # load still opens on Overview, where its error is expected.
        self._no_spec_given = not files
        if files:
            try:
                self.context.use(files)
            except (OSError, ValueError) as e:
                self._startup_error = str(e)

    # Nothing to build without a spec, so a bare 'seine tui' opens on
    # Doctor rather than an empty Overview.
    def on_mount(self):
        from seine import settings
        current = settings.load()
        # An unset/hand-edited theme is silently skipped, not an error.
        if current["theme"] in commands.THEMES:
            self.theme = commands.THEMES[current["theme"]]
        if self._no_spec_given:
            self.push_screen(DoctorScreen())
        else:
            self.push_screen(OverviewScreen())
        # Deferred to after the initial screen's mount -- a startup
        # command like /plan needs a screen already on the stack.
        self.call_after_refresh(self._run_startup_commands, current["startup_commands"])

    def _run_startup_commands(self, lines):
        for line in lines:
            try:
                commands.dispatch(self, line)
            except commands.CommandError as e:
                self.say(str(e), error=True)

    def say(self, text, error=False, warning=False):
        if isinstance(self.screen, BaseScreen):
            self.screen.say(text, error=error, warning=warning)

    def refresh_screens(self):
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    # Refreshes both chips regardless of which one a caller actually
    # changed -- ConsoleAdapter.on_event() (target.py) needs
    # TargetIndicator kept current too, not just Indicators.
    def refresh_indicators(self):
        if isinstance(self.screen, BaseScreen):
            self.screen.query_one(Indicators).refresh_text()
            self.screen.query_one(TargetIndicator).refresh_text()

    def _build_finished(self):
        self.refresh_indicators()
        if isinstance(self.screen, BuildScreen):
            self.screen.update_body()
        # One-shot: only a build 'start-build' itself started sets this
        # (seine/tui/ai.py's _start_ai_build), and it must not fire
        # again for whatever build runs next.
        if self.build_state.notify_ai:
            self.build_state.notify_ai = False
            # Still on the Build screen start-build's own app.show()
            # switched to -- shown, not left unnoticed, since nothing
            # suggests they went looking elsewhere. Any other screen
            # means they navigated away themselves; that choice is
            # left alone.
            if isinstance(self.screen, BuildScreen):
                self.show("chat")
            ai.notify_build_finished(self)

    def show(self, name):
        target = SCREENS[name]
        if type(self.screen) is not target:
            self.switch_screen(target())
        else:
            self.screen.refresh_data()

    # '!<command>' / bare '!': a real shell, handed the real terminal via
    # 'App.suspend()', then a one-line exit status once the TUI resumes.
    def shell_escape(self, cmdline):
        shell = os.environ.get("SHELL", "/bin/sh")
        argv = [shell, "-c", cmdline] if cmdline.strip() else [shell]
        with self.suspend():
            result = subprocess.run(argv)
        self.say("$ %s  -> exit %d" % (cmdline or shell, result.returncode))

    # Textualize/textual#5525 (proposed, closed without merging -- still
    # unfixed as of textual 2.1.2, the version this pins): a fenced code
    # block's own MarkdownFence widget watches 'self.app.theme' with
    # 'init=True', which can fire _retheme() -- and its
    # 'get_child_by_type(Static)' -- before compose() has actually
    # mounted that Static, raising NoMatches. Hit live rendering a chat
    # reply with several fenced blocks (seine/tui/chat.py's own
    # Markdown(content) use). Not seine's bug to fix (a vendored
    # dependency file), and 'App._handle_exception()' always exits the
    # whole session otherwise -- losing a live chat/build over a code
    # fence that just won't re-theme this once is a worse outcome than
    # logging it and moving on. Every other exception still panics as
    # normal: only this exact, identified race is swallowed.
    def _handle_exception(self, error):
        if _is_markdown_retheme_race(error):
            self.log.warning("ignored a known textual race in "
                             "MarkdownFence._retheme (textual#5525): %s" % error)
            return
        super()._handle_exception(error)

# Split out from _handle_exception() so the identification itself is
# testable without a live MarkdownFence race to trigger it -- a
# synthetic traceback with the same frame identity proves the check.
def _is_markdown_retheme_race(error):
    if not isinstance(error, NoMatches):
        return False
    tb = error.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        if code.co_name == "_retheme" and code.co_filename.endswith("_markdown.py"):
            return True
        tb = tb.tb_next
    return False

def run(files=None):
    SeineApp(files).run()
