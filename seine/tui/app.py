# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The TUI application: Overview/Plan/Build screens over one prompt, a
# command registry shared with Tab completion and the command palette,
# and a '!' shell escape. Everything is read from the engine on open --
# no separate TUI model, beyond build_state which outlives its screen.

import asyncio
import os
import subprocess
import socket
import threading
import json

from textual import command
from textual.app import App
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import RichLog, Static

from seine.tui import ai, commands
from seine.tui.base import BaseScreen, Indicators, Prompt, StaticPane, TargetIndicator
from seine.tui.build import BuildScreen, BuildState
from seine.tui.chat import ChatScreen
from seine.tui.context import Context
from seine.tui.filesystem import FilesystemScreen, FilesystemState
from seine.tui.history import History
from seine.tui.issues import IssuesScreen
from seine.tui.render import (render_analyze, render_artifacts, render_cache,
                              render_doctor, render_overview, render_packages,
                              render_plan)
from seine.tui.spectree import SpecTree
from seine.tui.target import TargetState
from seine.tui.target_screen import TargetScreen
from seine.tui.testing import TestState

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

# A live view over app.test_state, the same "own tick, redraw #body"
# shape BuildScreen's own #tasklist uses, plus a #tail output pane fed
# by TestState.output_lines -- BuildScreen's own row/CSS ids (#tail,
# #buildrow) reused as-is rather than a new DEFAULT_CSS block, safe
# since only one screen is ever mounted at a time.
class TestScreen(BaseScreen):
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            StaticPane(Static(id="body", markup=False), id="cmd"),
            id="main",
        )
        yield Horizontal(RichLog(id="tail", markup=False, wrap=True, max_lines=4000),
                         id="buildrow")
        yield from self.footer()

    def on_mount(self):
        super().on_mount()
        self._output_run_id = None
        self._output_offset = 0
        self._timer = self.set_interval(1.0, self._tick)

    def on_unmount(self):
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()

    def _tick(self):
        self._follow()
        self.update_body()

    def update_body(self):
        state = self.app.test_state
        self.query_one("#body", Static).update(state.render())
        if state.message:
            self.say(state.message, error=state.error)

    # A fresh run (state.run_id bumped by TestState.reset()) starts the
    # pane over, the same "remounted widget starts from the current
    # log's beginning" spirit BuildScreen's own Tail class follows for
    # a real file -- there is no file here, so 'new since last tick' is
    # just a list slice instead of a seek.
    def _follow(self):
        state = self.app.test_state
        tail = self.query_one("#tail", RichLog)
        if state.run_id != self._output_run_id:
            self._output_run_id = state.run_id
            self._output_offset = 0
            tail.clear()
        new_lines = state.output_lines[self._output_offset:]
        if new_lines:
            tail.write("\n".join(new_lines))
            self._output_offset = len(state.output_lines)

SCREENS = {"overview": OverviewScreen, "plan": PlanScreen, "build": BuildScreen,
          "artifacts": ArtifactsScreen, "filesystem": FilesystemScreen,
          "packages": PackagesScreen, "analyze": AnalyzeScreen,
          "cache": CacheScreen, "doctor": DoctorScreen, "diff": DiffScreen,
          "issues": IssuesScreen, "chat": ChatScreen, "target": TargetScreen,
          "test": TestScreen}

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

    def __init__(self, files=None, interaction_socket=None):
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
        # Give AIState a back-reference to the app so it can trigger socket
        # notifications (assistant messages) without importing ``app`` here.
        self.ai_state.app = self
        self.target_state = TargetState()
        self.test_state = TestState()
        self.test_state.on_finished = self._test_finished
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
        # -----------------------------------------------------------------
        # Interaction socket handling -- optional, enabled when the CLI passes
        # ``--interaction-socket``. The socket is created (overwriting any stale
        # file) and a background thread is started to accept connections.
        # Clients send JSON messages terminated by a newline. Incoming messages
        # are dispatched via ``_handle_socket_message``.
        # -----------------------------------------------------------------
        self._socket_path = interaction_socket
        self._socket_clients: list[socket.socket] = []
        self._socket_lock = threading.Lock()
        if self._socket_path:
            self._start_socket_server()

    # Interaction-socket helpers -- only reachable when --interaction-socket
    # is passed. They run in background threads and marshal UI actions via
    # self.call_from_thread().
    def _start_socket_server(self) -> None:
        """Create the UNIX socket and launch the acceptor thread.

        The socket file is removed if it already exists. A daemon thread runs
        ``_socket_accept_loop`` which spawns a per-connection handler.
        """
        path = self._socket_path
        if not path:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen()
        self._socket_server = server
        threading.Thread(target=self._socket_accept_loop, daemon=True).start()

    def _socket_accept_loop(self) -> None:
        """Accept connections and spin a handler thread for each client."""
        server: socket.socket = getattr(self, "_socket_server", None)
        if server is None:
            return
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                break
            with self._socket_lock:
                self._socket_clients.append(conn)
            threading.Thread(target=self._socket_client_handler, args=(conn,), daemon=True).start()

    def _socket_client_handler(self, conn: socket.socket) -> None:
        """Read newline-delimited JSON messages from *conn*.

        Each line is parsed as JSON and handed to ``_handle_socket_message``.
        The connection is closed on any error or when the client disconnects.
        """
        with conn:
            buffer = b""
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                buffer += data
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        message = json.loads(line.decode())
                    except json.JSONDecodeError:
                        continue
                    self._handle_socket_message(message)
        # Clean up client list.
        with self._socket_lock:
            if conn in self._socket_clients:
                self._socket_clients.remove(conn)

    def _handle_socket_message(self, msg: dict) -> None:
        """Dispatch a JSON message received from an external client.

        Supported ``type`` values:
        * ``"input"`` -- simulate a user typing. ``msg["text"]`` is the string
          to type. The TUI will clear the current prompt, feed each character
          with a tiny delay (to emulate typing) and finally send an ``Enter``.
        * ``"ai_input"`` -- forward a prompt directly to the AI chat (equivalent
          to the user typing a line that does not start with ``/``). ``msg["prompt"]``
          contains the text.
        """
        t = msg.get("type")
        if t == "input":
            text = msg.get("text", "")
            if not isinstance(text, str):
                return
            # type_and_submit() itself already runs on the app's own thread
            # (invoked below via call_from_thread) -- awaiting action_submit()
            # directly here, rather than a second call_from_thread(), since
            # that call only works from a thread other than the app's own.
            async def type_and_submit():
                try:
                    prompt = self.screen.query_one(Prompt)
                except NoMatches:
                    return
                prompt.value = ""
                prompt.cursor_position = 0
                for ch in text:
                    prompt.value += ch
                    prompt.cursor_position = len(prompt.value)
                    await asyncio.sleep(0.02)
                await prompt.action_submit()
            self.call_from_thread(type_and_submit)
        elif t == "ai_input":
            p = msg.get("prompt")
            if isinstance(p, str):
                self.call_from_thread(ai.ask, self, p)

    def _socket_send(self, data: dict) -> None:
        """Broadcast a JSON message to all connected socket clients.

        ``data`` is serialized with ``json.dumps`` and terminated by a newline.
        Clients that raise an exception on send are removed from the list.
        """
        if not hasattr(self, "_socket_clients"):
            return
        raw = (json.dumps(data) + "\n").encode()
        with self._socket_lock:
            dead = []
            for client in self._socket_clients:
                try:
                    client.sendall(raw)
                except OSError:
                    dead.append(client)
            for client in dead:
                try:
                    client.close()
                finally:
                    self._socket_clients.remove(client)

    def _socket_send_ai_messages(self) -> None:
        """Emit any new assistant messages that have not yet been sent.

        Called from ``AIState.changed`` after persisting. Only finished
        assistant replies (role == "assistant") are sent -- intermediate
        streaming chunks are omitted.
        """
        msgs = self.ai_state._new_assistant_messages()
        for msg in msgs:
            self._socket_send({"type": "ai_message", "content": msg.get("content", "")})
        self.ai_state._mark_sent()

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
        self._socket_send({"type": "build_finished",
                           "error": self.build_state.error,
                           "message": self.build_state.message})
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

    def _test_finished(self):
        self._socket_send({"type": "test_finished",
                           "error": self.test_state.error,
                           "message": self.test_state.message})

    def show(self, name):
        target = SCREENS[name]
        if type(self.screen) is not target:
            self.switch_screen(target())
            self._socket_send({"type": "screen_changed", "screen": name})
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

def run(argv=None):
    """Entry point for the TUI.

    ``argv`` is a list of command-line arguments as passed from the CLI.
    It may contain ``--interaction-socket`` (or ``--interaction-socket=PATH``)
    followed by zero or more specification files. The socket argument is
    stripped from the list before the remaining items are treated as spec
    files.
    """
    # Basic manual parsing -- we avoid pulling in ``argparse`` to keep the
    # import surface small and to stay consistent with the rest of the CLI
    # which does manual ``getopt`` parsing.
    spec_files: list[str] = []
    socket_path: str | None = None
    if argv:
        it = iter(argv)
        for arg in it:
            if arg.startswith("--interaction-socket"):
                # ``--socket=PATH`` or ``--socket PATH``
                if arg == "--interaction-socket":
                    try:
                        socket_path = next(it)
                    except StopIteration:
                        raise ValueError("--interaction-socket requires a path")
                else:
                    # ``--interaction-socket=PATH``
                    _, _, path = arg.partition("=")
                    if not path:
                        raise ValueError("--interaction-socket requires a path")
                    socket_path = path
                continue
            spec_files.append(arg)
    # ``SeineApp`` now accepts an optional ``interaction_socket`` argument.
    SeineApp(files=spec_files or None, interaction_socket=socket_path).run()

