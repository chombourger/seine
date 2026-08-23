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

    # No migration from an older format -- it just doesn't match the
    # {"line", "ts", "scope"} shape, so it reads as empty.
    def test_the_old_plain_string_array_format_is_empty_not_migrated(self):
        with open(self.path(), "w") as f:
            json.dump(["/old", "history"], f)
        self.assertEqual(history.History(self.path()).lines, [])

class AddingAndRecalling(AHistoryFile):
    def test_add_appends_a_line_with_its_scope(self):
        h = history.History(self.path())
        h.add("/foo")
        h.add("hi", scope="chat")
        self.assertEqual([(e["line"], e["scope"]) for e in h.lines],
                         [("/foo", "commands"), ("hi", "chat")])

    def test_add_persists_across_instances(self):
        history.History(self.path()).add("/foo")
        reloaded = history.History(self.path())
        self.assertEqual([e["line"] for e in reloaded.lines], ["/foo"])

    def test_entries_are_oldest_first_and_filtered_by_scope(self):
        h = history.History(self.path())
        h.add("/one")
        h.add("hi", scope="chat")
        h.add("/two")
        self.assertEqual([e["line"] for e in h.entries({"commands"})],
                         ["/one", "/two"])
        self.assertEqual([e["line"] for e in h.entries({"commands", "chat"})],
                         ["/one", "hi", "/two"])

    def test_last_is_the_most_recent_matching_entry(self):
        h = history.History(self.path())
        h.add("/one")
        h.add("hi", scope="chat")
        self.assertEqual(h.last({"commands", "chat"}), "hi")
        self.assertEqual(h.last({"commands"}), "/one")
        self.assertIsNone(h.last({"target"}))

class SideLoading(AHistoryFile):
    def test_add_side_never_reaches_the_file(self):
        h = history.History(self.path())
        h.side_load("target")
        h.add_side("target", "root")
        self.assertFalse(os.path.exists(self.path()))
        self.assertEqual(h.lines, [])

    def test_add_side_participates_in_recall(self):
        h = history.History(self.path())
        h.side_load("target")
        h.add_side("target", "root")
        self.assertEqual(h.last({"target"}), "root")

    # Persisted and side-loaded lines interleave by when they were
    # actually typed, not grouped by source.
    def test_persisted_and_side_loaded_interleave_by_time(self):
        h = history.History(self.path())
        h.add("/use foo.yaml")
        h.side_load("target")
        h.add_side("target", "dmesg")
        h.add("/build")
        self.assertEqual([e["line"] for e in h.entries({"commands", "target"})],
                         ["/use foo.yaml", "dmesg", "/build"])

    # A second side_load() of the same name is a clean slate, same as
    # target.connect() tearing down before a fresh dial.
    def test_side_load_again_drops_the_previous_group(self):
        h = history.History(self.path())
        h.side_load("target")
        h.add_side("target", "old-device-command")
        h.side_load("target")
        self.assertIsNone(h.last({"target"}))

    def test_side_unload_drops_the_group_from_recall(self):
        h = history.History(self.path())
        h.add("/build")
        h.side_load("target")
        h.add_side("target", "root")
        h.side_unload("target")
        self.assertEqual(h.last({"commands", "target"}), "/build")

    def test_side_unload_with_nothing_loaded_is_a_no_op(self):
        h = history.History(self.path())
        h.side_unload("target")  # must not raise
        self.assertIsNone(h.last({"target"}))

    # add_side() works even without a prior side_load() -- one less
    # precondition a caller can get wrong.
    def test_add_side_without_a_prior_side_load_still_works(self):
        h = history.History(self.path())
        h.add_side("target", "root")
        self.assertEqual(h.last({"target"}), "root")

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
        entries.append({"line": line, "ts": time.time() - age_seconds,
                        "scope": "commands"})
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
