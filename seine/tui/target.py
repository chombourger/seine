# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional '/target' support: driving a real device through mtda
# (github.com/siemens/mtda), a gRPC service already exposing
# power/storage/USB/console control. mtda is a system package (like
# python3-guestfs), never a pip dependency of seine -- see
# doctor.check_mtda() for the install-time report; available() below is
# the runtime gate '/target' and its AI tools check before doing
# anything. pyte (setup.py's 'tui' extra) is only imported inside
# ConsoleAdapter below, not here -- everything above it works without
# pyte installed, same lazy-import discipline as mtda.client itself.

import re
import time

_available = None

# A real import, not importlib.util.find_spec: proves mtda.client
# actually loads (catches a broken system install), not just that it is
# on the path. Cached after the first call -- this only needs to run
# once per process.
def available():
    global _available
    if _available is None:
        try:
            import mtda.client  # noqa: F401
            _available = True
        except Exception:
            _available = False
    return _available

# Raised by every function below -- '/target' (commands.py) and the AI
# tools (ai.py) each catch it and translate it their own way, same as
# context.side_load()'s OSError/ValueError is handled in each caller.
class Unavailable(Exception):
    pass

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
        state.session = client.session()
    if remote:
        adapter = ConsoleAdapter(app)
        client.console_remote(remote, adapter)
        app._target_console = adapter
    app._target_client = client
    return client

# Lazily dialled: whichever side (typed command or AI tool call) touches
# the target first pays the connection cost, then both reuse the same
# mtda.client.Client cached on the app -- unaffected by connect()/
# disconnect() below, which only add an explicit way to pick or drop an
# agent on top of this implicit one.
def get_client(app):
    if not available():
        raise Unavailable("mtda is not installed on this system")
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
        raise Unavailable("mtda is not installed on this system")
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
    app._target_client = None
    app._target_console = None
    if hasattr(app, "target_state"):
        app.target_state = TargetState()

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
    return get_client(app).storage_to_host()

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
# ConsoleAdapter.on_event() below, wired up by get_client()'s
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

# --- Console pane (fed by the same client.console_remote() subscription) ---

# 80x25 is the VGA text-mode/BIOS convention (mtda.md's targets are
# QEMU-emulated PCs), but the screen is grown well past the 25-line
# half of it: this firmware's boot menu addresses absolute rows up to
# 31 via 'CSI row;colH', and a too-short pyte screen scrolls those
# absolute-positioned redraws instead of just overwriting them --
# stale and fresh copies of the same row both stay on screen, looking
# like duplicated text. 40 leaves real margin above the observed max.
CONSOLE_COLUMNS = 80
CONSOLE_LINES = 40

# pyte's CSI parser only special-cases '?' as a private-mode marker;
# any other unexpected character in the parameter area gets treated as
# the CSI's own final byte, dispatching a no-op and leaking the rest of
# the sequence as literal text. This firmware hits two such shapes:
#   - 'CSI = <n> <letter>' (a legacy mode-set convention, '\x1b[=3h') --
#     '=' has no standard meaning as a CSI parameter, so unrecoverable;
#     the whole sequence is stripped.
#   - 'CSI <row>;:<col>H' -- a stray ':' where a digit was expected
#     ('\x1b[25;:0H'). ':' is ECMA-48's sub-parameter separator, which
#     pyte doesn't implement, so deleting it recovers the cursor move
#     the firmware meant. Left unrecovered, the cursor never reaches
#     its intended row and a black SGR background bleeds across
#     redraws instead of just a leaked "0H".
# Real pyte limitations, fixed on the decoded text before pyte's parser
# sees it, not patched inside pyte itself.
_STRAY_COLON_IN_CSI = re.compile(r"(\x1b\[[0-9;]*);:+(?=[0-9;A-Za-z])")
# Anything else unrecognised has no safe reconstruction -- stripped
# generally, not as an allow-list of just the '=' shape seen so far.
_UNSUPPORTED_CSI = re.compile(r"\x1b\[[0-9;?]*[^0-9;?A-Za-z][0-9;?]*[A-Za-z]")
# Holds back a chunk's unterminated CSI tail rather than feeding it to
# pyte early -- needed whether it turns out malformed (only
# recognisable once complete) or normal (pyte already buffers those
# correctly on its own; no need to tell which in advance).
_UNSUPPORTED_CSI_PARTIAL = re.compile(r"\x1b(?:\[[^A-Za-z]*)?\Z")

def _strip_unsupported_csi(data, pending):
    data = pending + data
    data = _STRAY_COLON_IN_CSI.sub(r"\1;", data)
    data = _UNSUPPORTED_CSI.sub("", data)
    m = _UNSUPPORTED_CSI_PARTIAL.search(data)
    if m:
        return data[:m.start()], data[m.start():]
    return data, ""

# pyte.ByteStream.feed() decodes then dispatches in one call, no seam
# to strip unsupported CSI sequences from the decoded text before the
# parser sees it -- so this subclass re-does ByteStream.feed()'s own
# two lines (reusing self.utf8_decoder/self.use_utf8 from
# ByteStream.__init__) and inserts the strip in between, rather than
# reimplementing incremental UTF-8 decoding ourselves. Feed it raw
# bytes, not text decoded per-chunk: a chunk boundary landing
# mid-character is exactly what pyte's own incremental decoder exists
# to get right (decoding each chunk independently corrupts a split
# character into U+FFFD on both sides of the cut).
#
# Defined lazily (module-level 'import pyte' would defeat the point of
# ConsoleAdapter's own lazy import -- everything above it in this file
# still needs to work with no pyte installed).
_ByteStream = None

def _get_byte_stream_class():
    global _ByteStream
    if _ByteStream is None:
        import pyte

        class _ByteStreamImpl(pyte.ByteStream):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._pending_csi = ""

            def feed(self, data):
                if self.use_utf8:
                    text = self.utf8_decoder.decode(data)
                else:
                    text = "".join(map(chr, data))
                text, self._pending_csi = _strip_unsupported_csi(text, self._pending_csi)
                pyte.Stream.feed(self, text)

        _ByteStream = _ByteStreamImpl
    return _ByteStream

# The duck-typed 'screen' object mtda's RemoteConsole/ConsoleOutput
# actually calls: print(data) for raw console bytes (ConsoleOutput.
# write() -> print() -> screen.print(), output.py), on_event(event) for
# one 'EVT' line (remote.py:40-48). Never spawns mtda-cli or its
# interactive menu -- that has its own prefix-key menu which can mutate
# hardware outside seine's confirm gate.
class ConsoleAdapter:
    def __init__(self, app):
        import pyte
        self.app = app
        self.screen = pyte.Screen(CONSOLE_COLUMNS, CONSOLE_LINES)
        # pyte defaults to DECAWM (auto-wrap) on, matching a real
        # vt100. Real VGA/BIOS text mode clips at the screen edge
        # instead, and this firmware relies on that without ever
        # sending 'CSI ?7l' itself -- left on, its two-column layout
        # (menu left, help text right) wraps long help text onto the
        # next row's left half, overwriting menu items drawn there.
        self.screen.reset_mode(pyte.modes.DECAWM)
        self.stream = _get_byte_stream_class()(self.screen)
        # A boot log arrives as hundreds of small chunks a second --
        # redrawing on every single one made the console look like it
        # was printing one character at a time. dirty is a plain flag,
        # no thread marshal; TargetScreen's own tick redraws instead,
        # same "poll, don't push" precedent BuildScreen's tail uses.
        self.dirty = False

    def print(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.stream.feed(data)
        self.dirty = True

    def on_event(self, event):
        self.app.target_state.on_event(event)
        self.app.call_from_thread(self.app.refresh_indicators)

# pyte gives one Char per cell (fg/bg as an ANSI name or a bare hex
# triplet for 256/true-color) -- mapped straight to a Rich Style per
# cell. Naive: no run-length merging of same-style neighbours, so a
# redraw is one Text.append() per cell (up to CONSOLE_COLUMNS *
# CONSOLE_LINES); add merging if that ever shows up as slow in practice.
def _pyte_color(value):
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return "#" + value
    return value

def _pyte_style(char):
    from rich.style import Style
    fg = None if char.fg in (None, "default") else _pyte_color(char.fg)
    bg = None if char.bg in (None, "default") else _pyte_color(char.bg)
    return Style(color=fg, bgcolor=bg, bold=char.bold, italic=char.italics,
                underline=char.underscore, strike=char.strikethrough,
                reverse=char.reverse)

# max_lines: the pyte screen (CONSOLE_LINES) is taller than this
# firmware's own content ever needs, purely as scroll-drift margin (see
# CONSOLE_LINES's own comment) -- rows beyond what this BIOS actually
# draws into are never meant to be seen. So the *widget* showing this
# doesn't need to reserve room for all of them either: pass however
# many rows actually fit the available space, and only that many get
# rendered, from the top (where the real content lives).
def render_console(screen, max_lines=None):
    from rich.text import Text
    # no_wrap/overflow="crop": pyte already wrapped this at exactly
    # screen.columns -- a real terminal doesn't reflow when its display
    # is narrower than its own width, it clips. Rewrapping here would
    # scramble the fixed 80-column grid BIOS/serial-console output
    # actually assumes.
    text = Text(no_wrap=True, overflow="crop")
    lines = screen.lines if max_lines is None else min(max_lines, screen.lines)
    for y in range(lines):
        row = screen.buffer[y]
        for x in range(screen.columns):
            char = row[x]
            text.append(char.data, style=_pyte_style(char))
        if y < lines - 1:
            text.append("\n")
    return text

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
