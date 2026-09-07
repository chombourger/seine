# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import sys
import threading
import time

# Live build status shown on the terminal while step output goes to files.
# '--verbose' disables this and prints raw output instead.
SPINNER = {
    True:  "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    False: "|/-\\",
}
DONE   = {True: "✔", False: "ok"}
FAILED = {True: "✘", False: "!!"}

# True only for a real terminal; a pipe/file/CI log/dumb TERM gets
# one line per step instead.
def interactive(stream, environment):
    if stream is None or not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return environment.get("TERM", "") not in ["", "dumb"]

# Checks the stream's encoding can print the spinner/done/failed glyphs.
def unicode_safe(stream):
    encoding = getattr(stream, "encoding", None)
    if encoding is None:
        return False
    try:
        (SPINNER[True] + DONE[True] + FAILED[True]).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True

# Shared with 'seine analyze', which also times build steps.
def elapsed(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)

class Display:
    def __init__(self, stream=None, total=0, environment=None, clock=time.time):
        self.stream = stream if stream is not None else sys.stdout
        self.total = total
        self.clock = clock
        self.interactive = interactive(self.stream, environment or {})
        self.fancy = unicode_safe(self.stream)
        self.running = {}
        self.done = 0
        self.failed = 0
        self.lines = 0
        self.frame = 0
        # Reentrant: say() can run from the SIGINT handler while the
        # main thread already holds this lock (e.g. inside started()).
        self.lock = threading.RLock()
        self.ticker = None
        self.stop = threading.Event()

    def started(self, name):
        with self.lock:
            self.running[name] = self.clock()
            self._redraw()

    def finished(self, name, failed=False):
        with self.lock:
            started = self.running.pop(name, None)
            self.done += 1
            if failed:
                self.failed += 1
            # Leave this step's line in the scrollback; only redraw
            # what's still running.
            self._erase()
            self._line("%s %-28s %s" % (
                (FAILED if failed else DONE)[self.fancy], name,
                "" if started is None
                else elapsed(self.clock() - started)))
            self.lines = 0
            self._redraw()

    # Print a message without breaking the live redraw block below it.
    def say(self, text):
        with self.lock:
            self._erase()
            self._line(text)
            self.lines = 0
            self._redraw()

    def __enter__(self):
        if self.interactive:
            self.ticker = threading.Thread(target=self._tick, daemon=True)
            self.ticker.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.ticker is not None:
            self.ticker.join(timeout=1)
        with self.lock:
            self._erase()
        return False

    def _tick(self):
        while not self.stop.wait(0.1):
            with self.lock:
                self.frame += 1
                self._redraw()

    def _redraw(self):
        if not self.interactive:
            return
        self._erase()
        spinner = SPINNER[self.fancy]
        for name, started in sorted(self.running.items(),
                                    key=lambda item: item[1]):
            self._line("  %s %-28s %s" % (
                spinner[self.frame % len(spinner)], name,
                elapsed(self.clock() - started)))
        self._line(self._summary())
        self.stream.flush()

    def _summary(self):
        of = "/%d" % self.total if self.total else ""
        summary = "  %d%s done" % (self.done, of)
        if len(self.running) > 0:
            summary += ", %d running" % len(self.running)
        if self.failed > 0:
            summary += ", %d failed" % self.failed
        return summary

    def _erase(self):
        if self.interactive and self.lines > 0:
            self.stream.write("\x1b[%dA\x1b[J" % self.lines)
            self.lines = 0

    def _line(self, text):
        self.stream.write(text + "\n")
        if self.interactive:
            self.lines += 1
