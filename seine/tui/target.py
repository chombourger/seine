# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional '/target' support: driving a real device through mtda
# (github.com/siemens/mtda), a gRPC service exposing power/storage/USB/
# console control. mtda is a system package, never a pip dependency of
# seine; available() below is the runtime gate '/target' and its AI
# tools check before doing anything. pyte is required too, for the
# console pane (ConsoleAdapter).

import time

from seine.tui.console import CONSOLE_COLUMNS, CONSOLE_LINES
from seine.tui.console import ConsoleAdapter, render_console

_available = None

# 'app' here is duck-typed, not always a real SeineApp: tests exercise
# this module against a bare types.SimpleNamespace() with no '_socket_send'.
def _socket_send(app, event):
    send = getattr(app, "_socket_send", None)
    if send is not None:
        send(event)

# Real imports, not importlib.util.find_spec: proves mtda.client and
# pyte actually load, not just that they're on the path. Cached after
# the first call.
def available():
    global _available
    if _available is None:
        try:
            import mtda.client  # noqa: F401
            import pyte  # noqa: F401
            _available = True
        except Exception:
            _available = False
    return _available

# Raised by every function below; '/target' and the AI tools each
# catch it and translate it their own way.
class Unavailable(Exception):
    pass

# History group name shared with target_screen.py's _history_add()
# override, so the two sides can't drift apart.
HISTORY_GROUP = "target"

# grpc-core's thread pool (mtda's transport) blocks a real fork() until
# it's idle, which an open channel prevents forever. seine routes its
# own subprocesses around this via posix_spawn, but libguestfs's own
# qemu appliance launch does a real fork()+exec() in C that seine can't
# avoid. os.register_at_fork and pthread_atfork don't help here (neither
# fires for this C-level fork on this glibc), so instead imager.py's
# before_launch()/after_launch() hooks are wired here to disconnect the
# channel before g.launch() and reconnect after, only if this call was
# the one that actually disconnected it.
def _wire_launch_guard(app):
    from seine import imager
    def before():
        app._launch_guard_disconnected = getattr(app, "_target_client", None) is not None
        if app._launch_guard_disconnected:
            disconnect(app)
    def after():
        if getattr(app, "_launch_guard_disconnected", False):
            try:
                get_client(app)
            except Exception:
                pass
    imager.before_launch = before
    imager.after_launch = after

# The real dial, shared by get_client() (lazy, no host) and connect()
# (explicit, optional host) below. host=None reads mtda's own local
# config, same as mtda-cli with no '--remote'; MTDA_REMOTE overrides it
# the same way.
#
# Also starts the console/event subscription when there's somewhere to
# subscribe to: client.console_remote() builds a RemoteConsole reusing
# the RPC client's own host/ctrlport, so one connect pays for both.
# Skipped silently when agent.remote is None (a fully in-process mtda
# with no gRPC, outside what this integration targets).
def _connect(app, host=None):
    import mtda.client
    client = mtda.client.Client(host=host)
    client.start()
    _wire_launch_guard(app)
    remote = getattr(client.agent, "remote", None)
    state = getattr(app, "target_state", None)
    if state is not None:
        state.agent = remote if remote else "Local"
        session = client.session()
        # agent_info() echoes back the server-resolved session id, e.g.
        # "cert:alice[my-session]" for a certificate-bound agent.
        try:
            resolved = client.agent_info().get("session")
        except Exception:
            resolved = None
        state.session = resolved or session
    if remote:
        adapter = ConsoleAdapter(app)
        # Prime from mtda's buffer before console_remote() starts the
        # live EVT stream: Subscribe() is forward-only, so without this
        # a long-idle target shows a blank pane. Best-effort.
        try:
            dump = client.console_dump()
        except Exception:
            dump = None
        if dump:
            adapter.print(dump)
        app._target_console_primed = bool(dump)
        client.console_remote(remote, adapter)
        app._target_console = adapter
    app._target_client = client
    # A fresh, in-memory-only console recall for this connection; see
    # history.side_load() for why this never touches disk.
    history = getattr(app, "history", None)
    if history is not None:
        history.side_load(HISTORY_GROUP)
    _socket_send(app, {"type": "target_connected",
                       "agent": state.agent if state is not None else None})
    return client

# Lazily dialled: whichever side (typed command or AI tool call)
# touches the target first pays the connection cost, then both reuse
# the same cached mtda.client.Client.
def get_client(app):
    if not available():
        raise Unavailable("mtda or pyte not installed -- '/target' is disabled (run '/doctor' to check)")
    client = getattr(app, "_target_client", None)
    if client is None:
        client = _connect(app)
    return client

# Explicit '/target connect [agent]': always tears down whatever is
# currently connected first, even for a bare reconnect with no host --
# unlike get_client(), a real "try again".
def connect(app, host=None):
    if not available():
        raise Unavailable("mtda or pyte not installed -- '/target' is disabled (run '/doctor' to check)")
    disconnect(app)
    return _connect(app, host)

# '/target disconnect', and the first step of connect() above. '.stop()'
# is the real teardown on mtda.client.Client, not '.close()'. Errors
# from an already-dead channel aren't this call's problem to report.
#
# client.stop() only closes the RPC channel -- the live console/EVT
# stream started by console_remote() is a second, separate grpc channel
# (client.agent.console_output) that client.stop() never touches. Left
# open, that's exactly what keeps grpc-core's thread pool from ever
# reporting idle; see _wire_launch_guard() above.
def disconnect(app):
    client = getattr(app, "_target_client", None)
    if client is not None:
        try:
            console = getattr(client.agent, "console_output", None)
            if console is not None:
                console.stop()
        except Exception:
            pass
        try:
            client.stop()
        except Exception:
            pass
    adapter = getattr(app, "_target_console", None)
    if adapter is not None:
        adapter.close()
    app._target_client = None
    app._target_console = None
    if hasattr(app, "target_state"):
        app.target_state = TargetState()
    history = getattr(app, "history", None)
    if history is not None:
        history.side_unload(HISTORY_GROUP)

# Shared by every mutating caller: '/target', the Remote Target
# screen's clickable status tokens, and a bare typed line. No
# confirmation here, unlike the AI's gated tools: a person just typed
# or clicked this themselves. Still a thread worker, since the RPC
# call must stay off the UI thread.
def run_and_report(app, name, action):
    def run():
        try:
            action()
        except Exception as e:
            app.call_from_thread(app.say, "target: %s" % e, error=True)
            return
        app.call_from_thread(app.say, "%s: done" % name)
    app.run_worker(run, thread=True, exclusive=True, group="target")

# --- Power ---

_POWER_VERBS = {"on": "target_on", "off": "target_off", "toggle": "target_toggle"}

def power(app, state):
    client = get_client(app)
    return getattr(client, _POWER_VERBS[state])()

# --- USB ---

_USB_VERBS = {"on": "usb_on", "off": "usb_off", "toggle": "usb_toggle"}

def usb(app, port, state):
    client = get_client(app)
    return getattr(client, _USB_VERBS[state])(int(port))

# --- Storage ---

def storage_to_host(app):
    result = get_client(app).storage_to_host()
    _socket_send(app, {"type": "target_storage_on_host"})
    return result

def storage_to_target(app):
    return get_client(app).storage_to_target()

# storage_write_image() only opens/copies/closes the shared storage
# device; it never re-attaches storage to the target, so that's a
# second, explicit call here, matching 'storage write' + 'storage
# target' by hand on mtda-cli.
def write_image(app, path):
    client = get_client(app)
    client.storage_write_image(path)
    client.storage_to_target()
    _socket_send(app, {"type": "target_storage_write_completed", "path": path})

def snapshot(app):
    return get_client(app).storage_commit()

def rollback(app):
    return get_client(app).storage_rollback()

# --- Console ---

# raw=True (default): 'data' is sent as-is, for callers that already
# hold exact bytes. raw=False makes mtda's console_send() run
# codecs.escape_decode() first, so typing '\n' or '\x03' at the
# freeform prompt gets the one byte it means.
def console_send(app, data, raw=True):
    return get_client(app).console_send(data, raw=raw)

def console_run(app, cmd):
    return get_client(app).console_run(cmd)

# What console_run() blocks on: get (new_prompt=None) reads mtda's
# current setting, which rarely matches a real shell's PS1 (its default
# is '=> ', a U-Boot prompt). seine.testing's 'Log In' keyword sets it
# after login.
def console_prompt(app, new_prompt=None):
    return get_client(app).console_prompt(newPrompt=new_prompt)

# Drops mtda's server-side read buffer -- console_wait/console_run
# would otherwise match old accumulated text (a previous boot's
# 'login:', say). power_cycle() calls this right after powering off.
def console_clear(app):
    return get_client(app).console_clear()

# Read-only, ungated for the AI's tool: it has to see what a target is
# doing, not just poke it blind with send/run.
def console_dump(app):
    return get_client(app).console_dump()

# First/last line only, so the model can check "did it boot" without
# paying full-buffer tokens for console_dump() every time.
def console_head(app):
    return get_client(app).console_head()

def console_tail(app):
    return get_client(app).console_tail()

def console_wait(app, what, timeout=None):
    return get_client(app).console_wait(what, timeout=timeout)

# --- Video (a real framebuffer capture, for a GUI target with nothing
# on its serial console -- a Wayland/Qt app has only pixels, no text) ---

# (bytes, content_type) straight off mtda's VideoSnapshot RPC, or
# (None, None) if this agent has no video source configured.
def video_snapshot(app):
    return get_client(app).video_snapshot()

# --- Keyboard / mouse (HID injection, for a target with no console and
# no ssh -- a Wayland/Qt kiosk, a BIOS/UEFI menu with no serial) ---

def keyboard_press(app, key, repeat=1, ctrl=False, shift=False, alt=False, meta=False):
    return get_client(app).keyboard_press(
        key, repeat=repeat, ctrl=ctrl, shift=shift, alt=alt, meta=meta)

def keyboard_write(app, what):
    return get_client(app).keyboard_write(what)

def mouse_move(app, x, y, buttons=0):
    return get_client(app).mouse_move(x, y, buttons)

# --- Status (one-shot reads; TargetState below is the live counterpart) ---

def status(app):
    client = get_client(app)
    return {"power": client.target_status(),
            "uptime": client.target_uptime(),
            "storage": client.storage_status(),
            "usb": client.usb_ports()}

# --- Live state (fed by the 'EVT' topic on mtda's Subscribe stream) ---

# What the footer chip and the Remote Target screen's status pane both
# render, kept apart from any widget so it's testable without a running
# App. Fed by ConsoleAdapter.on_event(), wired up by console_remote().
class TargetState:
    def __init__(self):
        self.agent = None      # remote host:port, or 'Local'; None until get_client() connects
        self.session = None    # client.session(); None until get_client() connects
        self.power = None      # CONSTS.POWER value ('ON'/'OFF'/...), None until seen
        self.storage = None    # CONSTS.STORAGE location ('HOST'/'NETWORK'/'TARGET'), None until seen
        self.writing = False
        self.write_read = 0
        self.write_total = 0
        self.write_speed = 0.0
        self.write_written = 0
        # A local clock, not mtda's own: 'EVT' never carries an uptime
        # figure, only ON/OFF transitions.
        self.power_on_at = None
        # Set by ai.py's 'mtda-console-wait' tool while its background
        # worker runs, so a second call is refused rather than racing
        # two waits on the same console.
        self.waiting = False
        self.wait_what = None

    # RemoteConsole's EVT stream is forward-only: it never replays past
    # state, so on_event() alone leaves power/storage blank until
    # something changes while a screen is open. One-shot primer off
    # status(app)'s RPC read, for whoever just connected. Doesn't touch
    # writing/write_*: storage_status()'s 'writing' is a bare bool,
    # with no byte counts to show a percent from.
    def seed(self, status):
        self.power = status["power"]
        location, _writing, _written = status["storage"]
        self.storage = location
        # Backdated by the real uptime status() just read, not started
        # fresh from now, so an already-up target shows its real age.
        self.power_on_at = time.time() - status["uptime"] if self.power == "ON" else None

    # One line off the 'EVT' topic, exactly as mtda publishes it:
    # f"{domain} {info}" -- 'POWER ON', 'STORAGE TARGET', 'STORAGE
    # WRITING <read> <total> <speed> <written>'.
    def on_event(self, line):
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        info = line.split()
        if not info:
            return
        domain, rest = info[0], info[1:]
        if not rest:
            return
        if domain == "POWER":
            self.power = rest[0]
            if rest[0] == "ON":
                self.power_on_at = time.time()
            elif rest[0] == "OFF":
                self.writing = False
                self.power_on_at = None
        elif domain == "STORAGE":
            if rest[0] == "WRITING" and len(rest) == 5:
                self.writing = True
                self.write_read = int(rest[1])
                self.write_total = int(rest[2])
                self.write_speed = float(rest[3])
                self.write_written = int(rest[4])
            elif rest[0] in ("HOST", "NETWORK", "TARGET"):
                self.storage = rest[0]
                self.writing = False
            # LOCKED/UNLOCKED/OPENED/CORRUPTED/INITIALIZED/etc: nothing
            # here depends on these yet.


# --- Status pane ---

# A pure function of TargetState alone (no RPC call): one less thing
# that can raise while rendering. Uptime/USB rows are left out for now
# since, unlike power/storage, they have no live event to show without
# an RPC read from inside a render.
#
# POWER and STORAGE render as clickable tokens using a plain marker
# meta ("power"/("storage", where)), not Rich's '@click' action-link
# string: Textual overlays its own link style on any span whose meta
# contains '@click', which broke the colour here. TargetStatusStatic
# reads this marker in its on_click(), calling the matching
# TargetScreen action directly. Clicking never touches TargetState
# itself; only a real STORAGE/POWER event does.
def render_target_status(state):
    from rich.style import Style
    from rich.text import Text
    text = Text()

    # state.agent is None until connect()/get_client() actually dial:
    # no auto-connect on screen mount, so this is a real, common
    # "haven't tried yet" state, not a brief startup flicker.
    connected = state.agent is not None

    text.append("Agent:\n\n", style=Style())
    if connected:
        text.append(" %s\n" % state.agent, style=Style())
        text.append(" %s\n\n" % (state.session or ""), style=Style())
    else:
        text.append(
            " not connected -- '/target connect [agent]'\n\n",
            style=Style(color="grey50"))

    text.append("Controls:\n\n", style=Style())

    # Two icons, no labels or ON/HOST words: colour carries power's
    # state (dark_orange on, grey off); storage changes shape instead
    # (floppy attached to the target, eject on the host), since
    # 'attached to the target' is the one state worth colouring like
    # power's "on". Each click names the *other* value of the two-way
    # state, a toggle rather than a fixed destination.
    #
    # Not connected: both icons grey, and neither carries a
    # 'target-click' meta -- TargetStatusStatic.on_click() no-ops when
    # that key is absent, the entire "disabled" mechanism.
    text.append(" ")
    power_meta = {"target-click": "power"} if connected else {}
    text.append("⏻ ", style=Style(
        bold=True,
        color="dark_orange" if connected and state.power == "ON" else "grey50",
        meta=power_meta))
    text.append(" ")

    on_target = state.storage == "TARGET"
    storage_meta = ({"target-click": ("storage", "host" if on_target else "target")}
                     if connected else {})
    text.append("💾" if on_target else "⏏", style=Style(
        bold=True, color="dark_orange" if connected and on_target else "grey50",
        meta=storage_meta))
    text.append("\n")

    if state.writing and state.write_total > 0:
        percent = int(state.write_read * 100 / state.write_total)
        text.append("\nWRITING  %d%%\n" % percent)

    return text
