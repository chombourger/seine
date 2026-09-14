# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Replays a run's asciinema v2 .cast through the same pyte screen
# render_console() uses live, so a replay frame looks the same. A
# pure model -- OverviewScreen owns the timer and keys.

# Speed steps the pane's left/right keys walk, 1x in the middle.
SPEEDS = (0.5, 1.0, 2.0, 4.0)

TICK = 1 / 30  # same rate as the target screen's console poll


class Unavailable(Exception):
    pass


class CastPlayer:
    def __init__(self):
        self.path = None
        self.events = []
        self.duration = 0.0
        self.clock = 0.0
        self.speed_index = 1
        self.paused = False
        self.screen = None
        self._stream = None
        self._cursor = 0

    @property
    def speed(self):
        return SPEEDS[self.speed_index]

    @property
    def finished(self):
        return self._cursor >= len(self.events)

    # One JSON header, then [elapsed, "o"/"i", data] event lines.
    # Only output ("o") events touched the console, so input ones
    # are dropped; a malformed line is skipped, not fatal.
    def load(self, path):
        from seine.tui.console import (CONSOLE_COLUMNS, CONSOLE_LINES,
                                       _get_byte_stream_class, render_console)
        try:
            import pyte
            stream_class = _get_byte_stream_class()
        except ImportError:
            raise Unavailable(
                "pyte is not installed -- replay is disabled "
                "(pip install seine[target], or the seine-target package)")
        import json
        events = []
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError as e:
            raise ValueError("could not read %s: %s" % (path, e))
        for line in lines[1:]:
            try:
                moment, kind, data = json.loads(line)
            except (ValueError, TypeError):
                continue
            if kind != "o":
                continue
            try:
                events.append((float(moment), str(data).encode("utf-8")))
            except (TypeError, ValueError):
                continue
        self.path = path
        self.events = sorted(events, key=lambda e: e[0])
        self.duration = self.events[-1][0] if self.events else 0.0
        self.screen = pyte.Screen(CONSOLE_COLUMNS, CONSOLE_LINES)
        self._stream = stream_class(self.screen)
        self.clock = 0.0
        self._cursor = 0
        self.paused = False
        self._render_console = render_console

    # Advances the clock by dt seconds of wall time (times the current
    # speed) and feeds whatever events came due. Returns True when the
    # screen changed -- False while paused or once the cast is over.
    def tick(self, dt):
        if self.paused or self.screen is None:
            return False
        self.clock += dt * self.speed
        fed = False
        while self._cursor < len(self.events) and \
                self.events[self._cursor][0] <= self.clock:
            _, data = self.events[self._cursor]
            self._stream.feed(data)
            self._cursor += 1
            fed = True
        return fed

    def toggle_pause(self):
        # Space at the end starts over rather than sitting on the
        # last frame with nothing left to pause.
        if self.finished:
            self.restart()
        else:
            self.paused = not self.paused

    def faster(self):
        self.speed_index = min(len(SPEEDS) - 1, self.speed_index + 1)

    def slower(self):
        self.speed_index = max(0, self.speed_index - 1)

    def restart(self):
        from seine.tui.console import (CONSOLE_COLUMNS, CONSOLE_LINES,
                                       _get_byte_stream_class)
        import pyte
        self.screen = pyte.Screen(CONSOLE_COLUMNS, CONSOLE_LINES)
        self._stream = _get_byte_stream_class()(self.screen)
        self.clock = 0.0
        self._cursor = 0
        self.paused = False

    def render(self, max_lines=None):
        # Tail-aligned: the pane is shorter than the 40-row screen,
        # and replay must show the newest output, not the top rows.
        return self._render_console(self.screen, max_lines, tail=True)

    # Border subtitle, same role as the target screen's uptime one.
    def status(self):
        moment = "ended" if self.finished else "%ds/%ds" % (self.clock, self.duration)
        return "%s %s %gx" % ("II" if self.paused else ">", moment, self.speed)
