#!/usr/bin/env python3

import avocado
import contextlib
import io
import os
import sys
import unittest.mock

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.progress import Display, interactive, unicode_safe

# A terminal, as far as anything asking is concerned.
class Terminal(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True

class Ascii(Terminal):
    encoding = "ascii"

class Piped(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return False

def display(stream, environment=None, total=3):
    clock = [0.0]
    shown = Display(stream=stream, total=total, clock=lambda: clock[0],
                    environment=environment if environment is not None
                    else {"TERM": "xterm"})
    return shown, clock

class ATerminalIsDrawnOn(avocado.Test):
    def test(self):
        stream = Terminal()
        shown, clock = display(stream)
        shown.started("package:linux")
        clock[0] = 3671
        shown.finished("package:linux")
        written = stream.getvalue()

        # What is running, then what it took, in a form a person reads.
        self.assertIn("package:linux", written)
        self.assertIn("1h01m", written)
        self.assertIn("1/3 done", written)
        # And the live area is rewritten rather than appended to.
        self.assertIn("\x1b[", written)

class APipeIsNotDrawnOn(avocado.Test):
    def test(self):
        stream = Piped()
        shown, clock = display(stream)
        shown.started("package:linux")
        clock[0] = 61
        shown.finished("package:linux")
        written = stream.getvalue()

        # A log read afterwards wants one line per step and no cursor
        # games in the middle of it. Whether it is decorated is a question
        # about the encoding rather than about the terminal: a utf-8 log
        # holds a tick perfectly well.
        self.assertNotIn("\x1b[", written)
        self.assertEqual(len(written.strip().split("\n")), 1)
        self.assertIn("package:linux", written)
        self.assertIn("1m01s", written)

class WhatATerminalCannotPrintIsNotPrinted(avocado.Test):
    def test(self):
        # A build in a POSIX locale gets a spinner its terminal can show.
        self.assertEqual(unicode_safe(Terminal()), True)
        self.assertEqual(unicode_safe(Ascii()), False)

        stream = Ascii()
        shown, clock = display(stream)
        shown.started("rootfs")
        shown.finished("rootfs", failed=True)
        written = stream.getvalue()
        self.assertIn("!!", written)
        self.assertNotIn("✘", written)

class ADumbTerminalIsAPipe(avocado.Test):
    def test(self):
        self.assertEqual(interactive(Terminal(), {"TERM": "xterm"}), True)
        self.assertEqual(interactive(Terminal(), {"TERM": "dumb"}), False)
        self.assertEqual(interactive(Terminal(), {}), False)
        self.assertEqual(interactive(Piped(), {"TERM": "xterm"}), False)

class FailuresAreCounted(avocado.Test):
    def test(self):
        stream = Terminal()
        shown, clock = display(stream, total=2)
        shown.started("one")
        shown.finished("one")
        shown.started("two")
        shown.finished("two", failed=True)
        self.assertIn("2/2 done, 1 failed", stream.getvalue())

class ALongNameNeverWrapsTheLiveLine(avocado.Test):
    def test(self):
        # A wrapped line breaks the erase math above it. Nothing
        # written may be wider than the terminal.
        stream = Terminal()
        shown, clock = display(stream)
        with unittest.mock.patch.dict(os.environ, {"COLUMNS": "40", "LINES": "24"}):
            shown.started("uki:package:a-rather-long-multiconfig-task-name-amd64")
        written = stream.getvalue()
        for line in written.split("\n"):
            self.assertLessEqual(len(line), 40)

# pyte needs setup.py's 'tui' extra (bundled with textual/rich);
# cancel rather than error if it truly isn't there, same convention
# tests/tui/target.py's _pyte_required uses.
@contextlib.contextmanager
def _pyte_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("pyte is not installed: %s" % e)

class RedrawsNeverDriftOffColumnZero(avocado.Test):
    def test(self):
        # Some ptys don't reset the column after '\n'. Replay the real
        # bytes in a VT100 emulator and check the cursor stays put.
        with _pyte_required(self):
            import pyte
        screen = pyte.Screen(60, 10)
        vt = pyte.Stream(screen)

        class Recorder(Terminal):
            def write(self, s):
                vt.feed(s)
                return super().write(s)

        stream = Recorder()
        shown, clock = display(stream, total=3,
                                environment={"TERM": "xterm"})
        shown.started("uki:package:linux-uki-amd64")
        shown.started("image:rootfs")
        for _ in range(5):
            clock[0] += 1
            shown.frame += 1
            shown._redraw()

        self.assertEqual(screen.cursor.x, 0)
        for line in screen.display:
            self.assertNotIn("uki:package:linux-uki-amd64  1s", line)

class StepsRunningAtOnceAreAllShown(avocado.Test):
    def test(self):
        stream = Terminal()
        shown, clock = display(stream, total=4)
        shown.started("package:linux")
        clock[0] = 5
        shown.started("rootfs")
        written = stream.getvalue()
        # Both of them, oldest first, with the count beside them.
        self.assertIn("package:linux", written)
        self.assertIn("rootfs", written)
        self.assertIn("0/4 done, 2 running", written)
