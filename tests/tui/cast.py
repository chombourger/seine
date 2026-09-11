#!/usr/bin/env python3

import avocado
import contextlib
import json
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)


@contextlib.contextmanager
def _pyte_required(test):
    try:
        import pyte  # noqa: F401
        yield
    except ImportError as e:
        test.cancel("pyte is not installed: %s" % e)


def _write_cast(path, events, header_only=False):
    header = {"version": 2, "width": 80, "height": 40, "timestamp": 0,
              "env": {"TERM": "xterm-256color"}}
    lines = [json.dumps(header)]
    if not header_only:
        lines += [json.dumps([at, kind, data]) for at, kind, data in events]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class CastPlayer(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _pyte_required(self):
            from seine.tui.cast import CastPlayer as Player, SPEEDS
            self.Player = Player
            self.SPEEDS = SPEEDS

    def test_events_feed_in_clock_order(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"),
                           [(0.1, "o", "hi\r\n"), (1.0, "o", "bye\r\n")])
        player = self.Player()
        player.load(path)
        self.assertEqual(player.duration, 1.0)
        self.assertFalse(player.tick(0.05))
        self.assertTrue(player.tick(0.1))
        text = player.render().plain
        self.assertIn("hi", text)
        self.assertNotIn("bye", text)

    def test_speed_scales_the_clock(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"),
                           [(1.0, "o", "x\r\n")])
        player = self.Player()
        player.load(path)
        player.faster()
        self.assertEqual(player.speed, 2.0)
        self.assertTrue(player.tick(0.5))
        self.assertTrue(player.finished)

    def test_speed_steps_stay_in_range(self):
        player = self.Player()
        for _ in range(10):
            player.faster()
        self.assertEqual(player.speed, self.SPEEDS[-1])
        for _ in range(10):
            player.slower()
        self.assertEqual(player.speed, self.SPEEDS[0])

    def test_pause_freezes_the_clock(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"),
                           [(1.0, "o", "x\r\n")])
        player = self.Player()
        player.load(path)
        player.toggle_pause()
        self.assertFalse(player.tick(5.0))
        player.toggle_pause()
        self.assertTrue(player.tick(1.0))

    def test_space_at_the_end_restarts(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"),
                           [(1.0, "o", "x\r\n")])
        player = self.Player()
        player.load(path)
        self.assertTrue(player.tick(1.0))
        self.assertTrue(player.finished)
        player.toggle_pause()
        self.assertFalse(player.finished)
        self.assertEqual(player.clock, 0.0)
        self.assertNotIn("x", player.render().plain)

    def test_header_only_cast_is_a_valid_empty_still(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"), [], header_only=True)
        player = self.Player()
        player.load(path)
        self.assertEqual(player.duration, 0.0)
        self.assertTrue(player.finished)
        self.assertFalse(player.tick(1.0))

    def test_input_events_are_skipped(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"),
                           [(0.1, "i", "typed\r\n"), (0.2, "o", "shown\r\n")])
        player = self.Player()
        player.load(path)
        player.tick(1.0)
        text = player.render().plain
        self.assertIn("shown", text)
        self.assertNotIn("typed", text)

    def test_missing_file_is_a_value_error(self):
        player = self.Player()
        with self.assertRaises(ValueError):
            player.load(os.path.join(self.workdir, "gone.cast"))

    # A short pane shows the newest rows, not the top ones -- the
    # full 40-row screen never fits, and the interesting output is
    # at the bottom.
    def test_render_is_tail_aligned(self):
        events = [(0.1 + i * 0.1, "o", "line-%02d\r\n" % i) for i in range(20)]
        path = _write_cast(os.path.join(self.workdir, "t.cast"), events)
        player = self.Player()
        player.load(path)
        player.tick(30.0)
        shown = player.render(max_lines=14).plain
        self.assertIn("line-19", shown)
        self.assertNotIn("line-00", shown)

    # A run directory: the cast plus the interactions.json beside
    # it, timestamps rebased through the header's own timestamp.
    def _write_run(self, name="run", casts=None, entries=(), header_ts=1000):
        import time as _time
        outdir = os.path.join(self.workdir, name)
        os.makedirs(outdir, exist_ok=True)
        header = {"version": 2, "width": 80, "height": 40,
                  "timestamp": header_ts, "env": {"TERM": "xterm-256color"}}
        for basename in (casts or {}).values():
            with open(os.path.join(outdir, basename), "w",
                      encoding="utf-8") as f:
                f.write(json.dumps(header) + "\n"
                        + json.dumps([0.1, "o", "x\r\n"]) + "\n")
        with open(os.path.join(outdir, "interactions.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"console_cast": "console.cast",
                       "console_casts": casts or {},
                       "interactions": list(entries)}, f)
        return outdir

    def _entry(self, test, keyword, at, status="PASS", artifact=None):
        entry = {"test": test, "keyword": keyword, "args": [],
                 "timestamp": 1000 + at, "status": status}
        if artifact is not None:
            entry["artifact_kind"], entry["artifact_path"] = artifact
        return entry

    def test_timeline_keeps_only_the_replayed_test(self):
        outdir = self._write_run(
            casts={"x.alpha": "x.alpha.cast"},
            entries=[self._entry("x.alpha", "Power Cycle", 0.0),
                     self._entry("x.beta", "Power Cycle", 0.0),
                     self._entry("x.alpha", "Console Run", 5.0)])
        player = self.Player()
        player.load(os.path.join(outdir, "x.alpha.cast"))
        self.assertEqual(player.test_name, "x.alpha")
        self.assertEqual([r["keyword"] for r in player.timeline],
                         ["Power Cycle", "Console Run"])
        self.assertEqual([r["at"] for r in player.timeline], [0.0, 5.0])

    def test_global_cast_keeps_every_test(self):
        outdir = self._write_run(
            casts={},
            entries=[self._entry("x.alpha", "Power Cycle", 0.0),
                     self._entry("x.beta", "Power Cycle", 1.0)])
        with open(os.path.join(outdir, "console.cast"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"version": 2, "width": 80, "height": 40,
                                "timestamp": 1000,
                                "env": {"TERM": "xterm-256color"}}) + "\n")
        player = self.Player()
        player.load(os.path.join(outdir, "console.cast"))
        self.assertIsNone(player.test_name)
        self.assertEqual(len(player.timeline), 2)

    def test_missing_interactions_leaves_an_empty_timeline(self):
        path = _write_cast(os.path.join(self.workdir, "t.cast"),
                           [(0.1, "o", "x\r\n")])
        player = self.Player()
        player.load(path)
        self.assertEqual(player.timeline, [])
        self.assertEqual(player.render_timeline().plain, "")

    def test_current_index_follows_the_clock(self):
        outdir = self._write_run(
            casts={"x.alpha": "x.alpha.cast"},
            entries=[self._entry("x.alpha", "Power Cycle", 1.0),
                     self._entry("x.alpha", "Console Run", 5.0,
                                 status="FAIL")])
        player = self.Player()
        player.load(os.path.join(outdir, "x.alpha.cast"))
        self.assertEqual(player.current_index(), -1)
        player.tick(1.0)
        self.assertEqual(player.current_index(), 0)
        player.tick(5.0)
        self.assertEqual(player.current_index(), 1)

    def test_render_timeline_flags_the_current_row(self):
        outdir = self._write_run(
            casts={"x.alpha": "x.alpha.cast"},
            entries=[self._entry("x.alpha", "Power Cycle", 1.0),
                     self._entry("x.alpha", "Console Run", 5.0,
                                 status="FAIL",
                                 artifact=("screen", "boot-1.txt"))])
        player = self.Player()
        player.load(os.path.join(outdir, "x.alpha.cast"))
        player.tick(1.0)
        shown = player.render_timeline().plain
        self.assertIn("alpha", shown.splitlines()[0])
        self.assertIn("\u25b6 Power Cycle", shown)
        self.assertIn("\u2718", shown)
        self.assertIn("[screen]", shown)


if __name__ == "__main__":
    avocado.main()
