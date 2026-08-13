# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import sys
import threading
import time

# What a build is doing, while it does it.
#
# A build spends most of its time in one or two long steps and says
# nothing about them: the kernel prints thousands of lines nobody reads,
# and what is worth knowing -- what is running, how long it has been
# running, how much is left -- is buried in them. So every step's output
# goes to a file of its own and this takes the terminal.
#
# '--verbose' turns it off and puts the raw output back: when a build is
# going wrong that is what is wanted, and no summary replaces it.
SPINNER = {
    True:  "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
    False: "|/-\\",
}
DONE   = {True: "✔", False: "ok"}
FAILED = {True: "✘", False: "!!"}

# Where a terminal can be talked to as a terminal. A pipe, a file, a CI
# log or a dumb TERM gets one line per step instead -- the same
# information, in the form that survives being read later.
def interactive(stream, environment):
    if stream is None or not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return environment.get("TERM", "") not in ["", "dumb"]

# Whether the characters above will survive the encoding on the way out.
# A build in a POSIX locale is a build whose terminal cannot print a
# spinner, and a mojibake spinner is worse than a plain one.
def unicode_safe(stream):
    encoding = getattr(stream, "encoding", None)
    if encoding is None:
        return False
    try:
        (SPINNER[True] + DONE[True] + FAILED[True]).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True

# How long something took, in the units someone reading it thinks in.
# Out here rather than on the display because a build's steps are timed in
# two places now: while they run, and afterwards by 'seine analyze'.
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
        # Reentrant: say() is reached from the SIGINT handler, which runs
        # on the main thread wherever it happened to be -- including
        # inside started() or finished(), holding this.
        self.lock = threading.RLock()
        self.ticker = None
        self.stop = threading.Event()

    # Started and finished are called by whatever is running the steps;
    # everything else here is presentation.
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
            # A finished step leaves a line behind, above whatever is
            # still running: what a build did is worth having in the
            # scrollback, and only what it is doing needs redrawing.
            self._erase()
            self._line("%s %-28s %s" % (
                (FAILED if failed else DONE)[self.fancy], name,
                "" if started is None
                else elapsed(self.clock() - started)))
            self.lines = 0
            self._redraw()

    # Something worth saying while steps are running. Erase and redraw the
    # live block around it, as a finished step does: printing past the block
    # instead leaves the message inside what the next redraw clears.
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

    # A spinner has to move on its own, or a long step looks like a step
    # that has hung.
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
