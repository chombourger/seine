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
from textual.binding import Binding
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import RichLog, Static

from seine.tui import ai, commands
from seine.tui.base import (BaseScreen, Indicators, VendorIndicator, Prompt,
                            StaticPane, TargetIndicator)
from seine.tui.build import BuildScreen, BuildState
from seine.tui.chat import ChatScreen
from seine.tui.context import Context
from seine.tui.filesystem import FilesystemScreen, FilesystemState
from seine.tui.history import History
from seine.tui.issues import IssuesScreen
from seine.tui.vendor import VendorScreen, VendorState
from seine.tui.render import (append_logs_section, render_analyze,
                              render_artifacts, render_cache, render_doctor,
                              render_image_node, render_node, render_overview,
                              render_packages, render_plan, render_root_node,
                              render_test_node)
from seine.tui.spectree import SpecTree
from seine.tui.target import TargetState
from seine.tui.target_screen import TargetScreen
from seine.tui.testing import TestState

# A read failure shows inline rather than raising, same as
# FilesystemScreen's own preview_failed() does for a bad file. Split
# out from LogViewer/_refresh_log_pane() so it's testable on its own,
# without touching RichLog's internal render state.
def _read_log(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError as e:
        return "could not read %s: %s" % (path, e)

# Read-only, scrollable -- same widget class TestScreen/BuildScreen use
# for '#tail'. Hidden until a log link is clicked (see
# OverviewScreen._refresh_log_pane()), sharing the tree's own slot
# rather than the right pane's, per how the feature was asked for.
class LogViewer(RichLog):
    def __init__(self, **kwargs):
        super().__init__(markup=False, wrap=True, max_lines=20000, **kwargs)
        self.display = False

# Replay pane: a focusable Static showing one CastPlayer frame, in
# the same left slot SpecTree/LogViewer share -- only one of the
# three shows at a time. Keys are consumed locally (ConsolePane's own
# precedent on the target screen), never reaching the hidden tree:
# space pauses, left/right walk the speed steps, Esc stops.
class CastPane(StaticPane):
    can_focus = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.display = False

    def on_key(self, event):
        if self.screen._cast_key(event.key):
            event.stop()
            event.prevent_default()

# Reads the 'log-click'/'cast-play' markers render.py puts in a
# span's meta -- a plain value, not Rich's '@click' action-link
# in a span's meta -- a plain value, not Rich's '@click' action-link
# string, which Textual overlays with its own link style regardless
# (same reasoning as target_screen.py's TargetStatusStatic, the
# precedent this mirrors).
class BodyStatic(Static):
    def on_click(self, event):
        path = event.style.meta.get("log-click")
        if path:
            self.screen.action_show_log(path)
            return
        cast = event.style.meta.get("cast-play")
        if cast:
            self.screen.action_replay_cast(cast)

# How wide the image-node boxes may be: the right pane's own content
# width, not render.py's BOX_WIDTH default -- the pane only owns a
# third of the screen, so the default overflows it on a standard
# 80-column terminal. A scrollbar is reserved when none is shown yet:
# tall boxes always earn one once they land, which would otherwise
# shrink the pane after measuring and wrap the box art. Zero (before
# first layout) falls back to the default.
def _body_width(screen):
    from seine.tui.render import BOX_WIDTH
    body = screen.query_one("#body", Static)
    pane = screen.query_one("#cmd")
    width = body.content_size.width
    if width <= 0:
        return BOX_WIDTH
    if not pane.show_vertical_scrollbar:
        width -= pane.scrollbar_size_vertical or 2
    return width

class OverviewScreen(BaseScreen):
    HINT_ADD = [("complete", "logback", "Esc back (viewing a log/replay)")]

    # No-op unless a log or replay is being viewed -- same shape as
    # FilesystemScreen's own 'escape' -> action_close_preview.
    BINDINGS = BaseScreen.BINDINGS + [Binding("escape", "close_log", show=False)]

    # The replay pane takes over the tree's own slot, the same one
    # LogViewer already shares: '#body' (node content, Logs: links)
    # stays put on the right throughout.
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            LogViewer(id="logviewer"),
            CastPane(Static(id="cast", markup=False), id="castpane"),
            StaticPane(BodyStatic(id="body", markup=False), id="cmd"),
            id="main",
        )
        yield from self.footer()

    # Not reported from SeineApp.on_mount(): the status bar isn't
    # mounted yet at that point.
    def on_mount(self):
        # Set before super().on_mount(): it calls refresh_data(), which
        # calls update_body() (overridden below) right away -- too soon
        # to read these attributes if they were set after.
        #
        # SpecTree.path_for() of whichever node is currently selected,
        # so the right pane can follow it; None shows the whole-context
        # overview, same as every screen before selection started
        # driving this one.
        self._selected_path = None
        # Absolute path of the log currently shown in '#logviewer', or
        # None when the tree is showing instead.
        self._log_path = None
        # Absolute path of the screencast playing in '#cast', or None
        # when nothing is replaying; the player owns the clock.
        self._cast_path = None
        self._player = None
        self._cast_timer = None
        super().on_mount()
        if self.app._startup_error:
            self.say(self.app._startup_error, error=True)
        # '/replay' from another screen lands here with the cast to
        # play stashed on the app (see commands._replay()).
        pending = getattr(self.app, "_pending_replay", None)
        if pending is not None:
            self.app._pending_replay = None
            self.action_replay_cast(pending)

    def on_unmount(self):
        timer = getattr(self, "_cast_timer", None)
        if timer is not None:
            timer.stop()

    # Re-resolved by path rather than kept as a raw node reference:
    # SpecTree.load() rebuilds every node from scratch on each
    # refresh_data(), which would otherwise orphan it. A path that no
    # longer matches (an unrelated reload renamed/removed it) just
    # falls back to the overview in update_body() below.
    def on_tree_node_selected(self, event):
        self._track_selection(event.node)

    # Cursor moves (up/down) re-render the pane too -- requiring Enter
    # to see a node's content lagged one keystroke behind navigation.
    def on_tree_node_highlighted(self, event):
        self._track_selection(event.node)

    def _track_selection(self, node):
        self._selected_path = self.query_one(SpecTree).path_for(node)
        self.update_body()

    # Swaps the tree for a log's text -- Esc (action_close_log) swaps
    # back. A read failure shows inline rather than raising, same as
    # FilesystemScreen's own preview_failed() does for a bad file.
    # 'path' is absolute (render.py resolves index.json-relative
    # entries via logindex.resolve() before putting them in the
    # click meta), but a relative path still resolves against
    # logs_root() here rather than the process cwd.
    def action_show_log(self, path):
        from seine import logindex
        self.action_close_cast()
        self._log_path = logindex.resolve(path)
        self._refresh_left_pane()

    # Esc closes whichever of the two is showing -- a replay in
    # progress first, since it owns the focused pane.
    def action_close_log(self):
        if self._cast_path is not None:
            self.action_close_cast()
        elif self._log_path is not None:
            self._log_path = None
            self._refresh_left_pane()

    def _refresh_left_pane(self):
        tree = self.query_one(SpecTree)
        viewer = self.query_one(LogViewer)
        cast = self.query_one(CastPane)
        showing_log = self._log_path is not None and self._cast_path is None
        showing_cast = self._cast_path is not None
        if showing_log:
            viewer.clear()
            viewer.write(_read_log(self._log_path))
        tree.display = not (showing_log or showing_cast)
        viewer.display = showing_log
        cast.display = showing_cast
        if showing_cast:
            self._redraw_cast()
            cast.focus()
        else:
            (viewer if showing_log else tree).focus()

    # Backwards-compatible name: the only caller outside this screen
    # is tests pinning the log-swap behaviour.
    def _refresh_log_pane(self):
        self._refresh_left_pane()

    # Starts replaying a .cast in the tree's slot -- Esc
    # (action_close_log) stops it and restores the tree. A read or
    # parse failure shows on the status line rather than raising.
    def action_replay_cast(self, path):
        from seine.tui.cast import CastPlayer, TICK, Unavailable
        player = CastPlayer()
        try:
            player.load(os.path.abspath(path))
        except Unavailable as e:
            self.say(str(e), error=True)
            return
        except (OSError, ValueError) as e:
            self.say("replay: %s" % e, error=True)
            return
        self.action_close_cast()
        self._log_path = None
        self._player = player
        self._cast_path = player.path
        self._cast_timer = self.set_interval(TICK, self._tick_cast)
        self._refresh_left_pane()
        self.say("replaying %s -- space pauses, left/right set speed, Esc stops"
                 % os.path.basename(path))
        self.set_timer(2.5, lambda: self.say(""))

    def action_close_cast(self):
        timer = getattr(self, "_cast_timer", None)
        if timer is not None:
            timer.stop()
            self._cast_timer = None
        if self._cast_path is not None or self._player is not None:
            self._cast_path = None
            self._player = None
            self._refresh_left_pane()

    # CastPane's key handler: True when the key drove the replay (so
    # the pane stops it there), False for anything else. No-op when
    # nothing is replaying -- the tree's own keys apply instead.
    def _cast_key(self, key):
        if self._cast_path is None or self._player is None:
            return False
        if key == "space":
            self._player.toggle_pause()
        elif key == "left":
            self._player.slower()
        elif key == "right":
            self._player.faster()
        elif key == "escape":
            self.action_close_cast()
            return True
        else:
            return False
        self._redraw_cast()
        return True

    def _tick_cast(self):
        if self._player is None:
            return
        from seine.tui.cast import TICK
        if self._player.tick(TICK):
            self._redraw_cast()
        else:
            self._update_cast_border()

    def _redraw_cast(self):
        pane = self.query_one(CastPane)
        available = max(1, pane.size.height - 2)
        self.query_one("#cast", Static).update(
            self._player.render(max_lines=available))
        self._update_cast_border()

    def _update_cast_border(self):
        self.query_one(CastPane).border_subtitle = self._player.status()

    def update_body(self):
        tree = self.query_one(SpecTree)
        node = None
        if self._selected_path is not None:
            node = tree.node_for(self._selected_path)
            if node is None:
                self._selected_path = None
        if node is None:
            text = render_overview(self.app.context)
        elif node.parent is tree.root and node.children:
            # Root/group node: one of tree.root's own children, built
            # with the same children a build always has (see
            # SpecTree.load()) -- the no-active-spec leaf is also a
            # direct child of tree.root, but has none, so 'node.children'
            # tells the two apart without asking context.active.
            index = tree.root.children.index(node)
            context = self.app.context
            text = render_root_node(context.groups[index], context.builds[index])
        elif node.data == "image" and node.parent is not None and node.parent.parent is tree.root:
            index = tree.root.children.index(node.parent)
            build = self.app.context.builds[index]
            text = render_image_node(build.spec.get("image") or {},
                                     width=_body_width(self))
            text = append_logs_section(
                text, build.spec["distribution"]["release"],
                build.spec["distribution"]["architecture"], ("image",))
        elif (node.parent is not None and node.parent.parent is tree.root
              and node.data in ("distribution", "packages", "playbook")):
            # Task-backed branches without a dedicated renderer of their
            # own yet: generic fallback content, with whatever logs
            # exist for the task(s) spectree.branch_for() maps them to
            # appended below.
            index = tree.root.children.index(node.parent)
            build = self.app.context.builds[index]
            text = append_logs_section(
                render_node(node), build.spec["distribution"]["release"],
                build.spec["distribution"]["architecture"], (node.data,))
        elif (self._selected_path is not None and len(self._selected_path) >= 2
              and self._selected_path[1] == "test"):
            # Anything under the spec's 'test:' branch lists with the
            # same marks the Test screen uses (✔/✘/○), green/red for
            # passed/failed. A scalar field below a case (no test of
            # its own) falls back to the generic listing.
            state = self.app.test_state
            if not getattr(state, "test_paths", None):
                # No run yet this session: derive the paths from the
                # selected group's own spec so unexecuted tests still
                # list with ○ rather than falling back to plain names.
                try:
                    index = next(i for i, c in enumerate(tree.root.children)
                                 if c.data == self._selected_path[0])
                except StopIteration:
                    index = 0
                builds = self.app.context.builds
                if 0 <= index < len(builds):
                    import types
                    from seine.tui.spectree import test_paths as _test_paths
                    state = types.SimpleNamespace(
                        test_paths=_test_paths(builds[index].spec),
                        rows={}, result=None)
            text = render_test_node(
                node, self._selected_path[1:], state)
            if text is None:
                text = render_node(node)
        else:
            text = render_node(node)
        self.query_one("#body", Static).update(text)

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

# Not spec-scoped: cache and environment are shared by every build.
class CacheScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_cache())

class DoctorScreen(BaseScreen):
    def update_body(self):
        self.query_one("#body", Static).update(render_doctor())

# What /diff last computed -- not spec-scoped, so reads app.diff_text.
class DiffScreen(BaseScreen):
    def update_body(self):
        text = self.app.diff_text or (
            "no diff yet -- '/diff OLD.spdx.json NEW.spdx.json'\n")
        self.query_one("#body", Static).update(text)

# Live view over app.test_state: own tick redraws #body, plus a #tail
# output pane. Reuses BuildScreen's #tail/#buildrow ids -- safe since
# only one screen is mounted at a time.
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

    # A fresh run (state.run_id bumped by reset()) starts the pane over.
    # No file here, so 'new since last tick' is a list slice, not a seek.
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
          "test": TestScreen, "vendor": VendorScreen}

# Offers the command registry through Ctrl+P. Selecting one fills the
# prompt rather than running it; a deliberate Enter runs it.
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
    /* 'round', not Input's default 'tall': 'tall' uses eighth-block
       glyphs some terminal fonts lack, breaking the border. */
    #spectree, #tail, #logviewer, #castpane { width: 2fr; height: 100%; }
    #prompt, #spectree, #tail, #fslist, #previewpane, #logviewer, #castpane {
        border: round $foreground 40%;
    }
    #prompt:focus, #spectree:focus, #tail:focus, #fslist:focus,
    #previewpane:focus, #logviewer:focus, #castpane:focus {
        border: round $border;
    }
    /* Black like the target screen's own console pane, so a replay
       frame reads as a console rather than another text pane. */
    #castpane { background: black; border-subtitle-align: right; border-subtitle-color: $warning; }
    #cast { width: 80; height: auto; max-height: 40; }
    /* While startup commands run the prompt is shut (see
       _run_startup_commands() below) with a progress line in place
       of the default placeholder, italic so it reads as status. */
    #prompt.startup > .input--placeholder { text-style: italic; }
    #cmd, #tasks { width: 1fr; height: 100%; border: round $foreground 40%; }
    #body { padding: 1 2; }
    #tasklist { padding: 1 2; }
    #tail, #logviewer { padding: 0 1; }
    /* Vendor screen: own ids, 1fr:1fr both rows (others are 2fr:1fr). */
    #vendormain, #vendorrow { height: 1fr; }
    #vendorspectree, #vendortail, #vendorstatspane, #vendortaskspane {
        width: 1fr; height: 100%; border: round $foreground 40%;
    }
    #vendorspectree:focus, #vendortail:focus {
        border: round $border;
    }
    #vendorstats, #vendortasks { padding: 1 2; }
    #vendortail { padding: 0 1; }
    #fslist { height: 1fr; }
    #previewpane { height: 1fr; padding: 1 2; }
    #hint { color: $text-muted; padding: 0 2; }
    #infobar { height: 1; }
    #status { padding: 0 2; height: 1; width: 1fr; }
    #status.error { color: $error; }
    #status.warning { color: $warning; }
    /* '$accent', not '$text-muted' like '#hint': clickable, like a link. */
    #indicators { color: $accent; padding: 0 2; height: 1; width: auto; }
    #vendor-indicator { color: $accent; padding: 0 2; height: 1; width: auto; }
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
        # "N build" chip's finish edge; the start edge is start_build()
        # calling refresh_indicators() directly.
        self.build_state.on_finished = self._build_finished
        # Follows a build onto the vendor screen for its 'vendor' task,
        # then back -- see _build_task_started/_build_task_finished below.
        self.build_state.on_task_started = self._build_task_started
        self.build_state.on_task_finished = self._build_task_finished
        self.vendor_state = VendorState()
        self.vendor_state.on_finished = self._vendor_finished
        self.fs_state = FilesystemState()
        self.ai_state = ai.AIState()
        # Back-reference so AIState can trigger socket notifications
        # without importing app here.
        self.ai_state.app = self
        self.target_state = TargetState()
        self.test_state = TestState()
        self.test_state.on_finished = self._test_finished
        self.test_state.on_started = self._test_started
        self.test_state.on_changed = self._test_changed
        self.diff_text = None
        # Set by commands.py's _issues() right before app.show("issues");
        # IssuesScreen.update_body() reads these back.
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
        # Interaction socket, enabled via --interaction-socket: creates
        # the socket (overwriting any stale file) and starts a background
        # thread accepting newline-delimited JSON messages, dispatched
        # via _handle_socket_message.
        self._socket_path = interaction_socket
        self._socket_clients: list[socket.socket] = []
        self._socket_lock = threading.Lock()
        if self._socket_path:
            self._start_socket_server()

    # Interaction-socket helpers, only reachable when --interaction-socket
    # is passed. Run in background threads and marshal UI actions via
    # self.call_from_thread().
    def _start_socket_server(self) -> None:
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

    # Accepts connections, spinning a handler thread per client.
    def _socket_accept_loop(self) -> None:
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

    # Reads newline-delimited JSON messages from 'conn', handing each
    # to _handle_socket_message. Closes on error or disconnect.
    def _socket_client_handler(self, conn: socket.socket) -> None:
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

    # Dispatches a JSON message from an external client: "input" types
    # msg["text"] into the prompt and submits it; "ai_input" forwards
    # msg["prompt"] straight to the AI chat.
    def _handle_socket_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "input":
            text = msg.get("text", "")
            if not isinstance(text, str):
                return
            # Runs on the app's own thread (via call_from_thread below),
            # so action_submit() can just be awaited directly here.
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

    # Broadcasts a JSON message to all connected socket clients, dropping
    # any that raise on send.
    def _socket_send(self, data: dict) -> None:
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

    # Emits new assistant messages not yet sent, called from
    # AIState.changed after persisting. Streaming chunks are omitted.
    def _socket_send_ai_messages(self) -> None:
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
        # Input stays shut until deferred startup commands have run, so
        # a command typed in the first tick can't slip in front of them
        # (or land on a screen they then switch away from). Only engaged
        # when there is anything to wait for -- the common empty case
        # behaves exactly as before.
        self._running_startup = len(current["startup_commands"]) > 0
        if self._no_spec_given:
            self.push_screen(DoctorScreen())
        else:
            self.push_screen(OverviewScreen())
        # Deferred to after the initial screen's mount -- a startup
        # command like /plan needs a screen already on the stack.
        self.call_after_refresh(self._run_startup_commands, current["startup_commands"])

    def _run_startup_commands(self, lines):
        # Nothing seeded: leave the freshly mounted screen exactly as
        # its own on_mount() left it -- in particular, don't steal focus
        # back to the prompt a tick later while it is being used.
        if not lines:
            self._running_startup = False
            return
        total = len(lines)
        try:
            for index, line in enumerate(lines, 1):
                self._startup_progress(index, total)
                try:
                    commands.dispatch(self, line)
                except commands.CommandError as e:
                    self.say(str(e), error=True)
        finally:
            self._running_startup = False
            self._startup_progress(None, total)

    # Progress in the prompt's own placeholder ("Running startup
    # command (1/4)..."), cleared back to the default afterwards. Best
    # effort: a startup command may leave a modal (no prompt) on top.
    def _startup_progress(self, index, total):
        from seine.tui.base import DEFAULT_PLACEHOLDER
        try:
            prompt = self.screen.query_one(Prompt)
        except NoMatches:
            return
        if index is None:
            prompt.disabled = False
            prompt.placeholder = DEFAULT_PLACEHOLDER
            prompt.remove_class("startup")
            prompt.focus()
        else:
            prompt.disabled = True
            prompt.placeholder = "Running startup command (%d/%d)..." % (index, total)
            prompt.add_class("startup")

    # NoMatches below: worker callbacks (build/vendor finish, target
    # events) can land mid-transition, when the current screen's widgets
    # aren't composed yet -- a missed repaint is fine, a crash is not.
    def say(self, text, error=False, warning=False):
        if isinstance(self.screen, BaseScreen):
            try:
                self.screen.say(text, error=error, warning=warning)
            except NoMatches:
                pass

    def refresh_screens(self):
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    # Refreshes every chip regardless of which one a caller actually
    # changed -- ConsoleAdapter.on_event() needs TargetIndicator kept
    # current too, not just Indicators.
    def refresh_indicators(self):
        if isinstance(self.screen, BaseScreen):
            try:
                self.screen.query_one(Indicators).refresh_text()
                self.screen.query_one(VendorIndicator).refresh_text()
                self.screen.query_one(TargetIndicator).refresh_text()
            except NoMatches:
                pass

    def _build_finished(self):
        self._socket_send({"type": "build_finished",
                           "error": self.build_state.error,
                           "message": self.build_state.message})
        self.refresh_indicators()
        # The build's analyze record and plan baseline now exist, so
        # whatever is on screen (overview, plan, build) is stale -- not
        # just the build screen's own task list.
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()
        # One-shot: only ai.py's _start_ai_build sets this, and it must
        # not fire again for whatever build runs next.
        if self.build_state.notify_ai:
            self.build_state.notify_ai = False
            # Only switch to chat if still on the Build screen; if they
            # navigated away themselves, leave that choice alone.
            if isinstance(self.screen, BuildScreen):
                self.show("chat")
            ai.notify_build_finished(self)

    # A build's 'vendor' task is the one part of a build the vendor
    # screen already knows how to show. Followed there while it runs,
    # then back to the build screen once done -- only while still on
    # the screen this following put the user on, same rule as
    # _build_finished().
    def _build_task_started(self, name):
        if name == "vendor" and isinstance(self.screen, BuildScreen):
            self.show("vendor")

    def _build_task_finished(self, name, failed=False):
        if name == "vendor" and isinstance(self.screen, VendorScreen):
            self.show("build")

    def _vendor_finished(self):
        self._socket_send({"type": "vendor_finished",
                           "error": self.vendor_state.error,
                           "message": self.vendor_state.message})
        self.refresh_indicators()
        if isinstance(self.screen, VendorScreen):
            self.screen.update_body()
        # One-shot, same as BuildState's own; only ai.py's start-vendor sets this.
        if self.vendor_state.notify_ai:
            self.vendor_state.notify_ai = False
            if isinstance(self.screen, VendorScreen):
                self.show("chat")
            ai.notify_vendor_finished(self)

    def _test_started(self):
        # A fresh run clears the previous per-test states, so
        # whatever is on screen (overview, test) is stale -- same
        # reason _test_finished() refreshes after a run.
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    def _test_changed(self):
        # One test just started or finished, so the overview's test
        # listing is one mark behind -- repaint the right pane while
        # the run is still going, rather than waiting for
        # _test_finished(). Pane only, not refresh_data(): the spec
        # tree itself is unchanged mid-run, so keep the selection,
        # expansion and highlights exactly where they are.
        if isinstance(self.screen, OverviewScreen):
            self.screen.update_body()

    def _test_finished(self):
        self._socket_send({"type": "test_finished",
                           "error": self.test_state.error,
                           "message": self.test_state.message})
        # A finished test leaves new per-test states behind, so
        # whatever is on screen (overview, test) is stale -- same
        # reason _build_finished() refreshes after a build.
        if isinstance(self.screen, BaseScreen):
            self.screen.refresh_data()

    def show(self, name):
        target = SCREENS[name]
        if type(self.screen) is not target:
            self.switch_screen(target())
            self._socket_send({"type": "screen_changed", "screen": name})
        else:
            self.screen.refresh_data()

    # '!<command>' / bare '!': runs a real shell via App.suspend(), then
    # reports the exit status once the TUI resumes.
    def shell_escape(self, cmdline):
        shell = os.environ.get("SHELL", "/bin/sh")
        argv = [shell, "-c", cmdline] if cmdline.strip() else [shell]
        with self.suspend():
            result = subprocess.run(argv)
        self.say("$ %s  -> exit %d" % (cmdline or shell, result.returncode))

    # textual#5525: a fenced code block's MarkdownFence can call
    # _retheme() before its Static is mounted, raising NoMatches. Not
    # seine's bug (vendored dependency), and the default handler would
    # exit the whole session over it -- log and continue instead. Every
    # other exception still panics as normal.
    def _handle_exception(self, error):
        if _is_markdown_retheme_race(error):
            self.log.warning("ignored a known textual race in "
                             "MarkdownFence._retheme (textual#5525): %s" % error)
            return
        super()._handle_exception(error)

# Split out so the check is testable with a synthetic traceback,
# without needing a live MarkdownFence race to trigger it.
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

# Entry point for the TUI. 'argv' may contain --interaction-socket
# (or --interaction-socket=PATH) followed by zero or more spec files;
# the socket argument is stripped before the rest are treated as specs.
def run(argv=None):
    # Manual parsing, consistent with the rest of the CLI's getopt use.
    spec_files: list[str] = []
    socket_path: str | None = None
    if argv:
        it = iter(argv)
        for arg in it:
            if arg.startswith("--interaction-socket"):
                if arg == "--interaction-socket":
                    try:
                        socket_path = next(it)
                    except StopIteration:
                        raise ValueError("--interaction-socket requires a path")
                else:
                    _, _, path = arg.partition("=")
                    if not path:
                        raise ValueError("--interaction-socket requires a path")
                    socket_path = path
                continue
            spec_files.append(arg)
    SeineApp(files=spec_files or None, interaction_socket=socket_path).run()

