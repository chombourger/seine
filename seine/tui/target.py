# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional '/target' support: driving a real device through mtda
# (github.com/siemens/mtda), a gRPC service already exposing
# power/storage/USB/console control. mtda is a system package (like
# python3-guestfs), never a pip dependency of seine -- see
# doctor.check_mtda()/check_pyte() for the install-time report;
# available() below is the runtime gate '/target' and its AI tools check
# before doing anything. pyte (setup.py's 'tui' extra) is required for
# the console pane (ConsoleAdapter) -- '/target' is unavailable without
# either module.

import time

from seine.tui.console import CONSOLE_COLUMNS, CONSOLE_LINES
from seine.tui.console import ConsoleAdapter, render_console

_available = None

# 'app' here is duck-typed, not always a real SeineApp -- tests/tui/
# target.py's own Actions class exercises this module against a bare
# types.SimpleNamespace(), which has no '_socket_send' at all.
def _socket_send(app, event):
    send = getattr(app, "_socket_send", None)
    if send is not None:
        send(event)

# Real imports, not importlib.util.find_spec: proves mtda.client and
# pyte actually load (catches a broken install), not just that they are
# on the path. Cached after the first call -- this only needs to run
# once per process. '/target' needs both: mtda for the device link and
# pyte for the console pane.
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

# Raised by every function below -- '/target' (commands.py) and the AI
# tools (ai.py) each catch it and translate it their own way, same as
# context.side_load()'s OSError/ValueError is handled in each caller.
class Unavailable(Exception):
    pass

# History group name shared with target_screen.py's own _history_add()
# override -- one name to keep the two sides from drifting apart.
HISTORY_GROUP = "target"

# grpc-core's own thread pool (mtda's client/agent transport) refuses to
# let a real fork() proceed until it reports idle -- but with a channel
# open, its background global timer never stops rescheduling itself, so
# that wait never ends. seine's own subprocess calls are routed around
# this through posix_spawn (spawn_own_pgroup in utils.py), which never
# calls fork() at all -- but the 'image' task's libguestfs launches its
# qemu appliance with a real fork()+exec() of its own, in C, that seine
# has no say over.
#
# os.register_at_fork looked like the fix (a CPython-level hook run
# before ANY fork()), but it isn't one: CPython only invokes those
# callbacks from its own os.fork()/subprocess implementation, never for
# a fork() a C extension calls directly -- confirmed against this
# libguestfs by watching a stuck build's own worker thread (py-spy
# --native) sitting inside guestfs_launch -> fork() -> grpc's C++
# thread_pool.cc wait, with no os.register_at_fork callback having run.
# A real pthread_atfork(3) registration (glibc, fires for every fork()
# in the process, C or Python) would cover it, but dlsym can't resolve
# the symbol through ctypes on this glibc/Debian build to register one.
#
# So this is targeted, not generic: imager.py exposes before_launch()/
# after_launch() no-ops around its one g.launch() call, and wiring them
# here to disconnect()/reconnect is enough, because libguestfs's is the
# only fork this process makes that seine doesn't already route through
# posix_spawn. before() only tears the channel down if it finds one
# actually connected (so this never fights an explicit '/target
# disconnect' made while a build is running), and after() only
# reconnects if before() was the one that disconnected it.
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

# The real dial, shared by get_client() (lazy, no host of its own) and
# connect() (explicit, optional host) below. host=None reads mtda's own
# local config, exactly like running mtda-cli with no '--remote' does;
# MTDA_REMOTE (main.py:1652) already covers overriding it, same as for
# mtda-cli.
#
# Also starts the console/event subscription, but only when there is
# somewhere to subscribe to: client.console_remote() (main.py:348-366)
# builds and starts a RemoteConsole for us, reusing the same host/
# ctrlport the RPC client already resolved -- one connect pays for both
# the RPC client and the live console/EVT stream, same "first touch"
# moment either way. Skipped silently (no live console, no crash, and
# no pyte import either -- see ConsoleAdapter) when agent.remote is
# None: a fully in-process mtda with no gRPC at all, outside what this
# integration targets.
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
        # "cert:alice[my-session]" when the agent binds identity to a
        # verified client certificate -- falls back to the plain
        # client-side tag.
        try:
            resolved = client.agent_info().get("session")
        except Exception:
            resolved = None
        state.session = resolved or session
    if remote:
        adapter = ConsoleAdapter(app)
        # Prime from mtda's buffer before console_remote() starts the live
        # EVT stream -- Subscribe() is forward-only, so without this a
        # long-idle target shows a blank pane. Runs first so nothing races
        # the live feed; best-effort since a failed dump isn't worth
        # failing the whole connect over.
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
    # A fresh, in-memory-only console recall for this connection -- see
    # seine.tui.history.side_load()'s own comment for why this never
    # touches disk.
    history = getattr(app, "history", None)
    if history is not None:
        history.side_load(HISTORY_GROUP)
    _socket_send(app, {"type": "target_connected",
                       "agent": state.agent if state is not None else None})
    return client

# Lazily dialled: whichever side (typed command or AI tool call) touches
# the target first pays the connection cost, then both reuse the same
# mtda.client.Client cached on the app -- unaffected by connect()/
# disconnect() below, which only add an explicit way to pick or drop an
# agent on top of this implicit one.
def get_client(app):
    if not available():
        raise Unavailable("mtda or pyte not installed -- '/target' is disabled (run '/doctor' to check)")
    client = getattr(app, "_target_client", None)
    if client is None:
        client = _connect(app)
    return client

# Explicit '/target connect [agent]': always tears down whatever is
# currently connected first (see disconnect() below), even for a bare
# reconnect with no host -- unlike get_client(), a real "try again", not
# just "connect if not already".
def connect(app, host=None):
    if not available():
        raise Unavailable("mtda or pyte not installed -- '/target' is disabled (run '/doctor' to check)")
    disconnect(app)
    return _connect(app, host)

# '/target disconnect', and the first step of connect() above. '.stop()'
# is the real teardown on the installed mtda.client.Client -- not
# '.close()', that only exists on the (unmerged) TLS branch's rewritten
# client. Errors from a channel that may already be dead are not this
# call's problem to report.
#
# client.stop() only closes the RPC channel (client._impl) -- the live
# console/EVT stream started by console_remote() is a second, unrelated
# grpc channel of its own (mtda/console/remote.py's RemoteConsole builds
# and Subscribe()s on it directly), tracked as client.agent.console_output
# and never touched by client.stop(). Left open, it is exactly what keeps
# grpc-core's thread pool from ever reporting idle -- see
# _wire_launch_guard()'s comment above for why that matters here.
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

# Shared by every mutating caller -- '/target' (commands.py), the
# Remote Target screen's clickable status tokens, and a bare line typed
# at that screen. No confirmation here: unlike the AI's own gated
# tools (which act on their own judgement), every one of these is a
# real-time hardware action a person just typed or clicked themselves
# -- asking them to confirm what they just did is friction, not
# safety. Still a thread worker: the RPC call itself must stay off the
# UI thread, same as every other real network call here.
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

# storage_write_image() itself only opens/copies/closes the shared
# storage device (mtda/client.py's own Client.storage_write_image) --
# it never re-attaches storage to the target afterwards, so that is a
# second, explicit call here, matching what mtda-cli's own 'storage
# write' + a following 'storage target' would do by hand.
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

# raw=True (default): 'data' is sent as-is, for callers that already hold
# exact bytes (raw-keystroke mode, AI tool calls). raw=False makes mtda's
# own console_send() run codecs.escape_decode() first, so a human typing
# '\n' or '\x03' at the freeform prompt gets the one byte it means --
# _handle_freeform() (target_screen.py) is the only caller that passes it.
def console_send(app, data, raw=True):
    return get_client(app).console_send(data, raw=raw)

def console_run(app, cmd):
    return get_client(app).console_run(cmd)

# What console_run() blocks on -- get (new_prompt=None) reads mtda's
# current setting, which rarely matches a real shell's PS1 (its own
# default is '=> ', a U-Boot prompt). seine.testing's 'Log In' keyword
# sets both together after login, which is what makes console_run
# usable afterwards.
def console_prompt(app, new_prompt=None):
    return get_client(app).console_prompt(newPrompt=new_prompt)

# Drops mtda's own server-side read buffer -- console_wait/console_run
# match against whatever accumulated there, including text from before
# whoever is waiting now even started (a previous boot's own 'login:',
# say). seine.testing.library.target.power_cycle() calls this right
# after powering off, while nothing can add anything new yet, so
# whatever wait comes next only matches this cycle's own output.
def console_clear(app):
    return get_client(app).console_clear()

# Read-only, ungated for the AI's own tool (ai.py): the model has to be
# able to see what a target is doing, not just poke it blind with send/run.
def console_dump(app):
    return get_client(app).console_dump()

# First/last line only -- lets the model check "did it boot" or "what's
# the last line" without paying full-buffer tokens for console_dump()
# every time.
def console_head(app):
    return get_client(app).console_head()

def console_tail(app):
    return get_client(app).console_tail()

def console_wait(app, what, timeout=None):
    return get_client(app).console_wait(what, timeout=timeout)

# --- Video (a real framebuffer capture, unrelated to the console/pyte
# decode above -- what a GUI target with nothing on its serial console
# needs: a Wayland/Qt app has no text to wait for, only pixels) ---

# (bytes, content_type) straight off mtda's own VideoSnapshot RPC, or
# (None, None) if this agent has no video source configured. Not decoded
# or written anywhere here -- same "one thin wrapper, callers decide
# what to do with it" shape as console_dump()/console_tail().
def video_snapshot(app):
    return get_client(app).video_snapshot()

# --- Keyboard / mouse (HID injection -- what a target with no console
# and no ssh, only a screen and input, needs to be driven at all: a
# Wayland/Qt kiosk app, a BIOS/UEFI menu with no serial redirection) ---

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
# render -- kept apart from any widget so it is testable without a
# running App, the same split build.py's BuildState uses. Fed by
# console.py's own ConsoleAdapter.on_event(), wired up by get_client()'s
# console_remote() call.
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
        # A local clock, not mtda's own -- 'EVT' never carries an uptime
        # figure, only ON/OFF transitions. None while off/unknown.
        self.power_on_at = None
        # Set by ai.py's 'mtda-console-wait' tool while its background
        # worker is running, so a second call is refused rather than
        # racing two waits on the same console.
        self.waiting = False
        self.wait_what = None

    # RemoteConsole's EVT stream is forward-only -- it never replays
    # past state, so on_event() alone leaves power/storage blank until
    # something happens to change while a screen is open. One-shot
    # primer off status(app)'s own RPC read, for whoever just connected
    # (TargetScreen.on_mount(), typically). Doesn't touch writing/
    # write_* -- storage_status()'s 'writing' is a bare bool, no byte
    # counts, so there is nothing meaningful to show a percent from.
    def seed(self, status):
        self.power = status["power"]
        location, _writing, _written = status["storage"]
        self.storage = location
        # Backdated by the real uptime status() just read, not started
        # fresh from now -- a target already up before this screen
        # connected shows its real age, not zero.
        self.power_on_at = time.time() - status["uptime"] if self.power == "ON" else None

    # One line off the 'EVT' topic, exactly as mtda's main.py:notify()
    # publishes it: f"{domain} {info}" -- 'POWER ON', 'STORAGE TARGET',
    # 'STORAGE WRITING <read> <total> <speed> <written>' (main.py:757-
    # 764's _storage_event()/1235-1248's _power_event(), the WRITING
    # shape from writer.py per mtda.md). mtda-cli's own AppOutput.
    # on_event() is only a reference for the WRITING case -- it ignores
    # every other domain, so POWER/STORAGE-location handling below is
    # derived straight from main.py's notify() call sites instead.
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
            # LOCKED/UNLOCKED/OPENED/CORRUPTED/INITIALIZED/??? -- no
            # seine-visible state depends on these yet.


# --- Status pane ---

# A pure function of TargetState alone (no RPC call, no client) -- one
# less thing that can raise while rendering. Uptime/USB rows are left
# out for now: unlike power/storage they have no live event, so showing
# them means an RPC read from inside a render, a real complication not
# asked for yet -- add them once that is worth doing.
#
# POWER and STORAGE both render as clickable tokens -- meta carries a
# plain marker ("power"/("storage", where)), not Rich's own '@click'
# action-link string chat.py:171-172 uses for its tool rows: Textual
# overlays its own link style (an auto-contrast colour, underline) on
# *any* span whose meta contains '@click', unconditionally on top of
# whatever colour/underline this function already set -- exactly what
# broke the colour here and forced every storage token underlined
# regardless of which one is actually active. TargetStatusStatic
# (target_screen.py) reads this marker in its own on_click(), calling
# the matching TargetScreen.action_target_power_toggle()/
# action_target_storage(where) directly. Clicking never touches
# TargetState directly: only a subsequent real STORAGE/POWER event
# does, so this stays truthful even while an RPC is still in flight.
def render_target_status(state):
    from rich.style import Style
    from rich.text import Text
    text = Text()

    # state.agent is None until connect()/get_client() actually dial --
    # no more auto-connect on screen mount, so this is a real, common
    # "haven't tried yet" state, not just a brief startup flicker.
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

    # Two icons, no 'POWER'/'STORAGE' labels or 'ON'/'HOST' words --
    # colour carries power's own state (dark_orange on, grey off);
    # storage instead changes shape (floppy attached to the target,
    # eject on the host -- storage physically leaving the target, the
    # same metaphor a real eject button uses) since 'attached to the
    # target' is the one state worth colouring dark_orange like power's
    # own "on", the same way an OS colours removable media once it's
    # safe to pull versus still mounted. Each click always names the
    # *other* value of a two-way state (power's own on/off, storage's
    # host/target) -- a toggle, not a fixed destination. Indented one
    # column past 'Controls:', with a plain space between the two
    # icons so they don't read as a single glued token.
    #
    # Not connected: both icons grey regardless of the last-seen power/
    # storage value, and neither carries a 'target-click' meta at all --
    # TargetStatusStatic.on_click() (target_screen.py) no-ops when that
    # key is absent, which is the entire "disabled" mechanism, no
    # separate click-guard needed.
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
