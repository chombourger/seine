# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The Remote Target screen's console pane: pyte-backed terminal
# emulation over mtda's console byte stream, plus optional raw/
# asciinema capture. Split out of target.py, which grew too large.

import contextlib
import json
import re
import time

# --- Console pane (fed by the same client.console_remote() subscription) ---

# 80x25 is the VGA text-mode/BIOS convention, but the screen is grown
# past the 25-line half: this firmware's boot menu addresses absolute
# rows up to 31 via 'CSI row;colH', and a too-short pyte screen scrolls
# those redraws instead of overwriting them, leaving duplicated text.
# 40 leaves real margin above the observed max.
CONSOLE_COLUMNS = 80
CONSOLE_LINES = 40

# pyte's CSI parser treats any unexpected character in the parameter
# area as the CSI's final byte, dispatching a no-op and leaking the
# rest as literal text. This firmware hits two such shapes: 'CSI = <n>
# <letter>' (unrecoverable, stripped whole) and 'CSI <row>;:<col>H'
# (a stray ':' pyte doesn't implement as a sub-parameter separator;
# deleting it recovers the intended cursor move). Fixed on the decoded
# text before pyte's parser sees it, not patched inside pyte itself.
_STRAY_COLON_IN_CSI = re.compile(r"(\x1b\[[0-9;]*);:+(?=[0-9;A-Za-z])")
# Anything else unrecognised has no safe reconstruction, so it's
# stripped generally, not as an allow-list of just the '=' shape seen so far.
_UNSUPPORTED_CSI = re.compile(r"\x1b\[[0-9;?]*[^0-9;?A-Za-z][0-9;?]*[A-Za-z]")
# Holds back a chunk's unterminated CSI tail rather than feeding it to
# pyte early, since it's only recognisable as malformed once complete.
_UNSUPPORTED_CSI_PARTIAL = re.compile(r"\x1b(?:\[[^A-Za-z]*)?\Z")

def _strip_unsupported_csi(data, pending):
    data = pending + data
    data = _STRAY_COLON_IN_CSI.sub(r"\1;", data)
    data = _UNSUPPORTED_CSI.sub("", data)
    m = _UNSUPPORTED_CSI_PARTIAL.search(data)
    if m:
        return data[:m.start()], data[m.start():]
    return data, ""

# pyte.ByteStream.feed() decodes then dispatches in one call, with no
# seam to strip unsupported CSI sequences before the parser sees them.
# This subclass re-does feed()'s own two lines (reusing
# self.utf8_decoder/self.use_utf8) and inserts the strip in between,
# rather than reimplementing incremental UTF-8 decoding: feeding raw
# bytes matters because a chunk boundary landing mid-character is
# exactly what pyte's incremental decoder exists to get right.
#
# Defined lazily: a module-level 'import pyte' would defeat the point
# of ConsoleAdapter's own lazy import, since '/target' still needs to
# work with no pyte installed.
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

# Asciinema v2 writer for the console stream: one JSON header plus
# [elapsed, "o", data] lines, replayable with `asciinema play`. Only
# used when RunContext provides console_cast_path; interactive
# '/target' never sets it. Created once per adapter, appended across
# reconnects.
class _AsciinemaWriter:
    def __init__(self, path):
        self.path = path
        self._start = time.time()
        header = {
            "version": 2,
            "width": CONSOLE_COLUMNS,
            "height": CONSOLE_LINES,
            "timestamp": int(self._start),
            "env": {"TERM": "xterm-256color"},
        }
        # Truncate on first open of this run; later reconnects append.
        # If the file already exists, keep it and reuse its start time
        # so elapsed stays relative to the run's first byte.
        import os as _os
        if _os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    first = f.readline()
                    data = json.loads(first) if first else {}
                    ts = data.get("timestamp")
                    if isinstance(ts, (int, float)):
                        self._start = float(ts)
            except Exception:
                pass
            self._fh = open(path, "a", encoding="utf-8")
        else:
            self._fh = open(path, "w", encoding="utf-8")
            json.dump(header, self._fh)
            self._fh.write("\n")
            self._fh.flush()

    def write(self, data: bytes):
        elapsed = time.time() - self._start
        text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
        line = json.dumps([elapsed, "o", text], ensure_ascii=False)
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self):
        if getattr(self, "_fh", None) is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None


# The duck-typed 'screen' object mtda's RemoteConsole/ConsoleOutput
# actually calls: print(data) for raw console bytes, on_event(event)
# for one 'EVT' line. Never spawns mtda-cli or its interactive menu,
# which could mutate hardware outside seine's confirm gate.
class ConsoleAdapter:
    def __init__(self, app):
        import pyte
        self.app = app
        # Raw console capture, opt-in via 'console_log_path'
        # (RunContext sets it; interactive '/target' never does).
        # Append: several connects in one run share one continuous
        # transcript rather than overwriting each other.
        log_path = getattr(app, "console_log_path", None)
        self._log = open(log_path, "ab") if log_path else None
        cast_path = getattr(app, "console_cast_path", None)
        self._cast = _AsciinemaWriter(cast_path) if cast_path else None
        # One writer per test (RunContext creates the header file on
        # start_test); the global writer above stays for the whole run.
        self._casts = {}
        self._casts_lock = None
        # Only needed when writes come from mtda's background thread
        try:
            import threading as _thr
            self._casts_lock = _thr.Lock()
        except Exception:
            pass
        self.screen = pyte.Screen(CONSOLE_COLUMNS, CONSOLE_LINES)
        # pyte defaults to DECAWM (auto-wrap) on, matching a real
        # vt100. Real VGA/BIOS text mode clips at the screen edge
        # instead, and this firmware relies on that without sending
        # 'CSI ?7l' -- left on, its two-column layout wraps long help
        # text onto the next row, overwriting menu items drawn there.
        self.screen.reset_mode(pyte.modes.DECAWM)
        self.stream = _get_byte_stream_class()(self.screen)
        # A boot log arrives as hundreds of small chunks a second;
        # redrawing on every one made the console look like it was
        # printing one character at a time. dirty is a plain flag;
        # TargetScreen's tick redraws instead, poll not push.
        self.dirty = False

    def _cast_for_test(self, test_name):
        if not test_name:
            return None
        # Fast path without lock: single-threaded setup phase.
        w = self._casts.get(test_name)
        if w is not None:
            return w
        # RunContext already created the header file; resolve its path
        # via the context helper, or fall back to a sanitized name next
        # to the global cast.
        path = None
        getter = getattr(self.app, "_cast_path_for", None)
        if callable(getter):
            try:
                path = getter(test_name)
            except Exception:
                path = None
        if path is None:
            import re as _re, os as _os
            safe = _re.sub(r'[^A-Za-z0-9._-]', '_', test_name)
            base = getattr(self.app, "console_cast_path", None)
            if base:
                path = _os.path.join(_os.path.dirname(base), "%s.cast" % safe)
        if not path:
            return None
        # Ensure RunContext's own bookkeeping knows about this cast.
        try:
            if hasattr(self.app, "console_casts") and test_name not in self.app.console_casts:
                self.app.console_casts[test_name] = path
        except Exception:
            pass
        w = _AsciinemaWriter(path)
        # Keep per-test writers ordered by test start -- close() will flush all.
        if self._casts_lock is not None:
            with self._casts_lock:
                # Re-check under lock.
                if test_name not in self._casts:
                    self._casts[test_name] = w
                else:
                    # Another thread won -- discard duplicate and reuse.
                    w.close()
                    w = self._casts[test_name]
        else:
            self._casts[test_name] = w
        return w

    def print(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.stream.feed(data)
        self.dirty = True
        if self._log is not None:
            self._log.write(data)
            self._log.flush()
        # Per-test cast (if a test is running) plus the global run cast:
        # the per-test file is scoped evidence for that test, the
        # global one covers the whole run. Read under the same lock
        # RunContext.start_test()/end_test() hold while switching
        # current_test, so a byte from mtda's background thread never
        # sees a torn transition.
        lock = getattr(self.app, "_console_lock", None)
        with lock if lock is not None else contextlib.nullcontext():
            test = getattr(self.app, "current_test", None)
            if test:
                try:
                    w = self._cast_for_test(test)
                    if w is not None:
                        w.write(data)
                except Exception:
                    pass
        if self._cast is not None:
            self._cast.write(data)

    def on_event(self, event):
        self.app.target_state.on_event(event)
        self.app.call_from_thread(self.app.refresh_indicators)

    def close(self):
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._cast is not None:
            self._cast.close()
            self._cast = None
        for w in list(getattr(self, "_casts", {}).values()):
            try:
                w.close()
            except Exception:
                pass
        if hasattr(self, "_casts"):
            self._casts.clear()

# pyte gives one Char per cell (fg/bg as an ANSI name or a bare hex
# triplet), mapped straight to a Rich Style per cell. Naive: no
# run-length merging of same-style neighbours, so a redraw is one
# Text.append() per cell; add merging if that shows up as slow.
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
# firmware's content ever needs, purely as scroll-drift margin, so
# rows beyond what the BIOS actually draws are never meant to be seen.
# Pass however many rows fit the available space, rendered from the top.
# tail: end at the last row holding output rather than the
# screen bottom -- a replay pane is shorter than the 40-row screen,
# and the newest output is what must stay visible. A short session
# (fewer rows used than the screen holds) still starts at the top
# instead of showing a wall of leading blank rows.
def render_console(screen, max_lines=None, tail=False):
    from rich.text import Text
    # no_wrap/overflow="crop": pyte already wrapped this at exactly
    # screen.columns. Rewrapping here would scramble the fixed
    # 80-column grid BIOS/serial-console output assumes.
    text = Text(no_wrap=True, overflow="crop")
    lines = screen.lines if max_lines is None else min(max_lines, screen.lines)
    first = 0
    if tail and max_lines is not None:
        last = 0
        for y in range(screen.lines):
            if any(char.data not in ("", " ")
                    for char in screen.buffer[y].values()):
                last = y
        first = max(0, last + 1 - lines)
    for y in range(first, first + lines):
        row = screen.buffer[y]
        for x in range(screen.columns):
            char = row[x]
            text.append(char.data, style=_pyte_style(char))
        if y < first + lines - 1:
            text.append("\n")
    return text
