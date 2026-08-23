# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The '/target' screen: a live console + status pane for a real device
# driven through mtda (seine/tui/target.py). No spec tree here, unlike
# every other screen -- this one has nothing to do with a specification.
# Width split 3:2 (console : status), console being the reason to be on
# this screen (same framing IssuesScreen's table gets).
#
# Two ways to type at the target: a line at the Prompt/Input every
# other screen already uses (submit-on-Enter -- a non-'/', non-'!' line
# goes to console_send() instead of AI chat), or focusing ConsolePane
# itself for raw keystrokes (arrows, Ctrl combos, Esc -- e.g. to catch a
# PC target's "press Esc for BIOS setup" window, which a line-buffered
# Enter-to-submit input can never do in time).
#
# No confirmation for entering raw mode -- confirming every keystroke
# individually would be unusable, and focusing the pane is already a
# deliberate act on its own. Just a transient reminder in the status
# line (a couple of seconds, #status.warning) that keystrokes are now
# going straight to the target, not seine.

import os
import queue
import threading
import time

from textual.containers import Horizontal
from textual.widgets import Static

from seine.progress import SPINNER, elapsed
from seine.tui.base import BaseScreen, StaticPane

# Same braille spinner chat.py's own '#chatcol' border uses while the
# model is working.
FRAMES = SPINNER[True]

# VT100/ANSI byte sequences for the keys that don't already produce a
# literal character (event.character) -- finite, known shape. 'tab' is
# deliberately not here: Tab already means "switch pane" app-wide
# (BaseScreen's own HINT chip), and overriding that just inside raw mode
# would be a surprising, easy-to-forget exception to it.
_KEY_BYTES = {
    "escape": b"\x1b", "enter": b"\r", "backspace": b"\x7f", "delete": b"\x1b[3~",
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "home": b"\x1b[H", "end": b"\x1b[F", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
    "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
}

def _key_to_bytes(event):
    mapped = _KEY_BYTES.get(event.key)
    if mapped is not None:
        return mapped
    if event.key.startswith("ctrl+") and len(event.key) == 6 and event.key[5].isalpha():
        return bytes([ord(event.key[5].lower()) - ord("a") + 1])
    if event.character is not None:
        return event.character.encode("utf-8")
    return None

# Reads the 'target-click' marker render_target_status() (target.py)
# puts in a span's meta -- a plain value, not Rich's own '@click'
# action-link string, which Textual overlays with its own link colour/
# underline on any span carrying it, regardless of what that span's
# own style already set.
class TargetStatusStatic(Static):
    def on_click(self, event):
        clicked = event.style.meta.get("target-click")
        if clicked == "power":
            self.screen.action_target_power_toggle()
        elif isinstance(clicked, tuple) and clicked[0] == "storage":
            self.screen.action_target_storage(clicked[1])

# Unlike every other StaticPane on other screens: this one is the
# reason to be on this screen (mtda.md's own framing, same as
# IssuesScreen's table), so it takes a Tab stop like the prompt does.
class ConsolePane(StaticPane):
    can_focus = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raw_mode = False
        self._raw_queue = None
        self._raw_stop = None

    # Enabled immediately, no confirm -- a persistent drain worker for
    # as long as the pane stays focused. One thread only, so keystrokes
    # stay in order (a second, concurrent one racing it could deliver
    # them out of order).
    def on_focus(self, event):
        self._raw_queue = queue.Queue()
        self._raw_stop = threading.Event()
        self.raw_mode = True
        self.screen.say("raw keystrokes now going straight to the target", warning=True)
        self.set_timer(2.5, lambda: self.screen.say(""))
        self.app.run_worker(self._raw_drain_loop, thread=True, exclusive=True,
                            group="target-raw")

    def on_blur(self, event):
        self.raw_mode = False
        if self._raw_stop is not None:
            self._raw_stop.set()

    def _raw_drain_loop(self):
        from seine.tui import target
        while not self._raw_stop.is_set():
            try:
                data = self._raw_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                target.console_send(self.app, data)
            except Exception as e:
                self.app.call_from_thread(self.app.say, "target: %s" % e, error=True)

    def on_key(self, event):
        if not self.raw_mode or self._raw_queue is None:
            return
        data = _key_to_bytes(event)
        if data is not None:
            self._raw_queue.put(data)
            event.stop()
            event.prevent_default()

class TargetScreen(BaseScreen):
    DEFAULT_CSS = """
    /* min-width: CONSOLE_COLUMNS (80) plus border chrome -- Textual
       doesn't auto-shrink a fixed-width child for an undersized
       parent, and this firmware needs all 80 columns. No min-height:
       CONSOLE_LINES (40) is taller than this firmware ever uses, so
       #console's height is capped, not fixed, and _redraw_console()
       only asks for as many rows as actually fit. align centers the
       console horizontally, black fills anything wider. */
    #console-pane {
        width: 3fr; min-width: 84; height: 100%;
        border: round $foreground 40%; align: center middle; background: black;
        border-subtitle-align: right; border-subtitle-color: $warning;
    }
    #console-pane:focus { border: round $border; }
    #console { width: 80; height: auto; max-height: 40; }
    #targetstatus-pane { width: 2fr; height: 100%; border: round $foreground 40%; background: $background; }
    #targetstatus { background: $background; padding: 1 2; }
    """

    # Set here, not on_mount(): '/target connect' (commands.py) can call
    # this screen's own connect() right after app.show("target")
    # constructs it, and that worker's _redraw_status() may reach
    # _update_console_border() before on_mount() runs on the event loop.
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._spinner_frame = 0

    def compose(self):
        yield Horizontal(
            ConsolePane(Static(id="console", markup=False), id="console-pane"),
            StaticPane(TargetStatusStatic(id="targetstatus", markup=False), id="targetstatus-pane"),
            id="main",
        )
        yield from self.footer()

    def on_mount(self):
        super().on_mount()
        # Status pane (+ the console pane's own uptime/spinner border,
        # same tick): BuildScreen's own precedent for "good enough" --
        # a 1s tick, not a push per POWER/STORAGE event (those are rare
        # state transitions, not worth their own signalling path).
        self._status_timer = self.set_interval(1.0, self._redraw_status)
        # Console pane: a bounded-rate poll of ConsoleAdapter.dirty, not
        # a push per incoming chunk -- see target.py's ConsoleAdapter
        # for why a push-per-chunk design was the actual cause of a
        # boot log looking like it was printing one character at a time.
        # ~30/s (measured ~4ms per render_console() call, plenty of
        # headroom) reads as live for interactive typing while still
        # capping a boot log's redraw rate at a small fraction of the
        # hundreds/sec that caused the original problem.
        self._console_timer = self.set_interval(1 / 30, self._maybe_redraw_console)
        # No auto-connect otherwise -- '/target connect [agent]' is now
        # how a first connection happens; this only re-dials on its own
        # when there's already a live client to refresh (navigated back
        # to an already-connected screen) or $MTDA_REMOTE names a default
        # agent to reach, same signal mtda-cli itself honors.
        if getattr(self.app, "_target_client", None) is not None or \
                os.environ.get("MTDA_REMOTE"):
            self.connect()

    def on_unmount(self):
        for timer in (getattr(self, "_status_timer", None),
                     getattr(self, "_console_timer", None)):
            if timer is not None:
                timer.stop()

    # force=True is '/target connect [agent]' (commands.py, any screen):
    # always a real teardown-and-redial, even with no host, so it's a
    # genuine "try again". force=False is on_mount()'s own refresh: dial
    # only if nothing is connected yet.
    def connect(self, host=None, force=False):
        self.app.run_worker(lambda: self._connect(host, force), thread=True,
                            exclusive=True, group="target-connect")

    # Runs in a worker: dialling mtda is a real network call, kept off
    # the UI thread like every other target action here.
    def _connect(self, host=None, force=False):
        from seine.tui import target
        try:
            if force:
                target.connect(self.app, host)
            else:
                target.get_client(self.app)
        except Exception as e:
            self.app.call_from_thread(self.say, "target: %s" % e, error=True)
            return
        # Primer read for power/storage: the EVT stream is forward-only.
        # gRPC dials lazily, so this is often the first RPC to actually
        # reach the host -- a failure here is a real connect failure, so
        # it rolls the connection back instead of leaving it half-open.
        try:
            self.app.target_state.seed(target.status(self.app))
        except Exception as e:
            target.disconnect(self.app)
            self.app.call_from_thread(self.say, "target: %s" % e, error=True)
            return
        self.app.call_from_thread(self._redraw_console)
        self.app.call_from_thread(self._redraw_status)

    def update_body(self):
        self._redraw_console()
        self._redraw_status()

    # Runs on the UI thread already (Textual's own set_interval) -- no
    # call_from_thread needed here, only the worker-thread paths
    # (_connect(), ConsoleAdapter.on_event()) cross that boundary.
    def _maybe_redraw_console(self):
        adapter = getattr(self.app, "_target_console", None)
        if adapter is not None and adapter.dirty:
            adapter.dirty = False
            self._redraw_console()

    # try/except around the render itself, not just the RPC calls
    # elsewhere: this runs off a bare set_interval tick, with nothing
    # upstream to catch a bad frame (e.g. a colour pyte hands back that
    # Rich's Style() rejects) -- unguarded, that reaches Textual's own
    # fatal-error handler instead of a reportable "target: ..." message.
    def _redraw_console(self):
        from seine.tui import target
        adapter = getattr(self.app, "_target_console", None)
        try:
            if adapter is None:
                text = "(not connected -- no remote configured, or not reached yet)"
            else:
                # -2 for #console-pane's own border (round, one row top and
                # bottom) -- how many of the pyte screen's rows actually fit
                # right now, not the fixed CONSOLE_LINES ceiling.
                available = max(1, self.query_one("#console-pane").size.height - 2)
                text = target.render_console(adapter.screen, max_lines=available)
            self.query_one("#console", Static).update(text)
        except Exception as e:
            timer = getattr(self, "_console_timer", None)
            if timer is not None:
                timer.stop()
            self.say("target console: %s -- live redraw stopped" % e, error=True)

    def _redraw_status(self):
        from seine.tui import target
        try:
            self.query_one("#targetstatus", Static).update(
                target.render_target_status(self.app.target_state))
            self._update_console_border()
        except Exception as e:
            timer = getattr(self, "_status_timer", None)
            if timer is not None:
                timer.stop()
            self.say("target status: %s -- live redraw stopped" % e, error=True)

    # '#console-pane' spends its own border on the target's uptime
    # while it's on -- gone the moment power_on_at clears (target off,
    # or not connected yet), same as '#chatcol' clears its own
    # 'Working…' subtitle once a turn finishes.
    def _update_console_border(self):
        state = self.app.target_state
        pane = self.query_one("#console-pane")
        if state.power_on_at is None:
            pane.border_subtitle = ""
            return
        frame = FRAMES[self._spinner_frame % len(FRAMES)]
        self._spinner_frame += 1
        pane.border_subtitle = "%s up %s" % (frame, elapsed(time.time() - state.power_on_at))

    # '/command' and '!shell' still go into the normal persisted
    # history -- only a genuine console line is diverted to the
    # in-memory-only 'target' record set (target.connect()/disconnect()
    # own its side_load()/side_unload()), so a password typed at a
    # login prompt never reaches history.json.
    def _history_add(self, line):
        from seine.tui import target
        if line.startswith("/") or line.startswith("!"):
            self.app.history.add(line)
        else:
            self.app.history.add_side(target.HISTORY_GROUP, line)

    # A line that's neither '/command' nor '!shell', typed at this
    # screen: goes to the target's console instead of the AI chat.
    # raw=False: lets '\n', '\x03', etc. be typed as the two-character
    # escape they look like (see console_send()'s own comment) -- a
    # single-line Input has no other way to send a literal control byte.
    def _handle_freeform(self, line):
        from seine.tui import target
        target.run_and_report(self.app, "target console send",
                              lambda: target.console_send(self.app, line, raw=False))
        return True

    def action_target_power_toggle(self):
        from seine.tui import target
        target.run_and_report(self.app, "target toggle", lambda: target.power(self.app, "toggle"))

    def action_target_storage(self, where):
        from seine.tui import target
        fn = target.storage_to_host if where == "host" else target.storage_to_target
        target.run_and_report(self.app, "target storage %s" % where, lambda: fn(self.app))
