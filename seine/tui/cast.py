# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# In-pane screencast replay: plays a run's asciinema v2 .cast back
# through the same pyte screen + render_console() the live target
# console uses, so a replay frame looks exactly like the live console
# did. A pure model, no Textual -- the OverviewScreen pane owns the
# timer and the keys; this owns the clock, the speed and the screen.

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
        # The replayed test's keyword timeline, from the run's
        # interactions.json next to the cast: (seconds since the
        # cast's own start, keyword, status, artifact marker or None).
        # Empty when the run predates interactions.json -- replay
        # then works exactly as before, with no timeline.
        self.test_name = None
        self.timeline = []

    @property
    def speed(self):
        return SPEEDS[self.speed_index]

    @property
    def finished(self):
        return self._cursor >= len(self.events)

    # Parses an asciinema v2 file: one JSON header, then [elapsed,
    # "o", data] event lines. Only output ("o") events are kept --
    # input ("i") ones never touched the console. A malformed line is
    # skipped, not fatal; a header-only cast (no console traffic ever)
    # is a valid zero-length still, not an error.
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
        self._load_timeline(path)

    # The run's interactions.json lives next to the cast and holds
    # wall-clock timestamps, while cast events count from the cast's
    # own start -- aligned through the header's own timestamp. Kept
    # to the replayed test (reverse-mapped through 'console_casts');
    # the run-global console.cast has no single test, so it keeps
    # every entry. Anything unreadable or missing leaves an empty
    # timeline rather than failing the replay.
    def _load_timeline(self, path):
        import json
        import os
        self.test_name = None
        self.timeline = []
        outdir = os.path.dirname(os.path.abspath(path))
        try:
            with open(os.path.join(outdir, "interactions.json"),
                      encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        try:
            with open(path, encoding="utf-8") as f:
                header = json.loads(f.readline() or "{}")
            started = float(header.get("timestamp"))
        except (OSError, ValueError, TypeError):
            return
        mine = None
        for name, basename in (data.get("console_casts") or {}).items():
            if basename == os.path.basename(path):
                mine = name
                break
        rows = []
        for entry in data.get("interactions") or []:
            if mine is not None and entry.get("test") != mine:
                continue
            try:
                at = float(entry.get("timestamp", 0)) - started
            except (TypeError, ValueError):
                continue
            artifact = None
            if entry.get("artifact_kind"):
                artifact = "[%s]" % entry["artifact_kind"]
            rows.append({"at": max(0.0, at),
                         "test": entry.get("test"),
                         "keyword": entry.get("keyword") or "?",
                         "status": entry.get("status"),
                         "args": list(entry.get("args") or []),
                         "artifact": artifact})
        rows.sort(key=lambda row: row["at"])
        self.test_name = mine
        self.timeline = rows

    # Index of the entry playing at the clock, or -1 before the
    # first one. The pane repaints its timeline only when this
    # changes, not on every tick.
    def current_index(self):
        index = -1
        for i, row in enumerate(self.timeline):
            if row["at"] <= self.clock:
                index = i
            else:
                break
        return index

    # The entry playing now, or None before the first one -- what
    # the arguments pane shows.
    def current(self):
        index = self.current_index()
        if 0 <= index < len(self.timeline):
            return self.timeline[index]
        return None

    # The timeline for the right pane: one row per keyword with its
    # offset into the replay and its outcome mark, the row playing
    # now flagged. Same green ✔ / red ✘ as the Test screen, so a
    # failure reads the same here as where it ran.
    def render_timeline(self):
        from rich.text import Text
        text = Text()
        if not self.timeline:
            return text
        if self.test_name is not None:
            text.append("%s\n" % self.test_name.rsplit(".", 1)[-1])
        current = self.current_index()
        for i, row in enumerate(self.timeline):
            failed = row["status"] not in (None, "PASS")
            mark, style = (("✘", "red") if failed else ("✔", "green"))
            text.append("▶ " if i == current else "  ")
            text.append("%-18s +%ds " % (row["keyword"][:18], row["at"]))
            text.append(mark, style=style)
            if row["artifact"] is not None:
                from rich.style import Style
                text.append(" %s" % row["artifact"], style=Style(dim=True))
            text.append("\n")
        return text

    # The playing row's call arguments for the lower pane: the
    # keyword up top, one argument per line below. Empty before the
    # first row, and a dimmed note when the run predates argument
    # capture rather than a blank that reads as broken.
    def render_args(self):
        from rich.style import Style
        from rich.text import Text
        text = Text()
        row = self.current()
        if row is None:
            return text
        text.append("%s\n" % row["keyword"])
        args = row.get("args") or []
        if not args:
            text.append("(no recorded arguments)", style=Style(dim=True))
        for arg in args:
            text.append("  %s\n" % arg)
        return text

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
