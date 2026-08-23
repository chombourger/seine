#!/usr/bin/env python3

import avocado
import json
import os
import sys
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tui import history

class ParsingPruneAfter(avocado.Test):
    def test_unset_is_the_default_thirty_days(self):
        self.assertEqual(history.parse_prune_after(None), history.DEFAULT_PRUNE_AFTER)
        self.assertEqual(history.DEFAULT_PRUNE_AFTER, 30 * 86400)

    def test_zero_means_never_prune(self):
        self.assertIsNone(history.parse_prune_after("0"))
        self.assertIsNone(history.parse_prune_after("0d"))

    def test_a_bare_number_and_a_d_suffix_both_mean_days(self):
        self.assertEqual(history.parse_prune_after("15"), 15 * 86400)
        self.assertEqual(history.parse_prune_after("15d"), 15 * 86400)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            history.parse_prune_after("bogus")

    def test_zero_or_negative_days_raises(self):
        with self.assertRaises(ValueError):
            history.parse_prune_after("-5d")
        with self.assertRaises(ValueError):
            history.parse_prune_after("-5")

# History._prune_after() reads seine.settings, which is XDG-located --
# every test here points XDG_CONFIG_HOME at its own workdir so it never
# touches a real user's settings.json.
class AHistoryFile(avocado.Test):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def path(self):
        return os.path.join(self.workdir, "history.json")

    def set_pruning(self, value):
        from seine import settings
        current = settings.load()
        current["history_pruning"] = value
        settings.save(current)

class LoadingIsTolerant(AHistoryFile):
    def test_missing_file_is_empty(self):
        self.assertEqual(history.History(self.path()).lines, [])

    def test_not_json_is_empty(self):
        with open(self.path(), "w") as f:
            f.write("this is not json")
        self.assertEqual(history.History(self.path()).lines, [])

    # No migration from the older plain-string-array format -- it just
    # doesn't match the {"line", "ts"} shape, so it reads as empty.
    def test_the_old_plain_string_array_format_is_empty_not_migrated(self):
        with open(self.path(), "w") as f:
            json.dump(["/old", "history"], f)
        self.assertEqual(history.History(self.path()).lines, [])

class AddingAndRecalling(AHistoryFile):
    def test_add_appends_a_line_and_session_advances(self):
        h = history.History(self.path())
        h.add("/foo")
        h.add("/bar")
        self.assertEqual([e["line"] for e in h.lines], ["/foo", "/bar"])
        self.assertEqual(h.at, 2)

    def test_add_persists_across_instances(self):
        history.History(self.path()).add("/foo")
        reloaded = history.History(self.path())
        self.assertEqual([e["line"] for e in reloaded.lines], ["/foo"])

    def test_prev_and_next_walk_oldest_first(self):
        h = history.History(self.path())
        h.add("/one")
        h.add("/two")
        self.assertEqual(h.prev(), "/two")
        self.assertEqual(h.prev(), "/one")
        self.assertEqual(h.prev(), "/one")  # already at the oldest, stays put
        self.assertEqual(h.next(), "/two")
        self.assertEqual(h.next(), "")  # past the newest

class Pruning(AHistoryFile):
    # Appends to whatever is already on disk -- a test writing more than
    # one entry (differently aged) needs them to coexist, not overwrite
    # each other.
    def _write_entry(self, line, age_seconds):
        try:
            with open(self.path()) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = []
        entries.append({"line": line, "ts": time.time() - age_seconds})
        with open(self.path(), "w") as f:
            json.dump(entries, f)

    def test_an_entry_older_than_the_default_is_dropped_on_load(self):
        self._write_entry("/stale", 31 * 86400)
        self.assertEqual(history.History(self.path()).lines, [])

    def test_an_entry_within_the_default_survives(self):
        self._write_entry("/fresh", 1 * 86400)
        lines = history.History(self.path()).lines
        self.assertEqual([e["line"] for e in lines], ["/fresh"])

    def test_history_pruning_zero_keeps_everything(self):
        self.set_pruning("0")
        self._write_entry("/ancient", 365 * 86400)
        lines = history.History(self.path()).lines
        self.assertEqual([e["line"] for e in lines], ["/ancient"])

    def test_history_pruning_is_configurable(self):
        self.set_pruning("1d")
        self._write_entry("/two_days_old", 2 * 86400)
        self.assertEqual(history.History(self.path()).lines, [])

    # A hand-edited or foreign settings.json shouldn't take history down
    # with it -- falls back to the default age instead of raising.
    def test_a_bad_stored_setting_falls_back_to_the_default(self):
        self.set_pruning("not-a-duration")
        self._write_entry("/fresh", 1 * 86400)
        self._write_entry("/stale", 31 * 86400)
        lines = history.History(self.path()).lines
        self.assertEqual([e["line"] for e in lines], ["/fresh"])

    def test_pruning_also_rewrites_the_file_on_load(self):
        self._write_entry("/stale", 31 * 86400)
        history.History(self.path())
        with open(self.path()) as f:
            self.assertEqual(json.load(f), [])

    def test_add_prunes_before_persisting(self):
        self._write_entry("/stale", 31 * 86400)
        h = history.History(self.path())
        h.add("/fresh")
        self.assertEqual([e["line"] for e in h.lines], ["/fresh"])
