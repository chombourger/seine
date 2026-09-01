#!/usr/bin/env python3

import avocado
import io
import os
import sys

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
