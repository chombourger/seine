#!/usr/bin/env python3

import avocado
import contextlib
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.testing import testindex


# Every run_spec() test below needs this: mirrors
# tests/cli/testing.py's own '_test_extra_required'.
@contextlib.contextmanager
def _test_extra_required(test):
    try:
        import robot  # noqa: F401
        yield
    except ImportError as e:
        test.cancel("the 'test' extra (robotframework) is not installed: %s" % e)


def _outcome(name, status="PASS", elapsed=1.0):
    return {"name": name, "status": status, "elapsed": elapsed}


def _write(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(text)
    return path


MINIMAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<robot generator="Robot 7.4.2" generated="2026-09-11T07:53:15.043955">
<suite name="seine test">
<test name="boots to a login prompt and identifies as Linux">
<status status="PASS" start="2026-09-11T07:53:15.067676" elapsed="25.153776"/>
</test>
<test name="a failed install is reported, not fatal to the whole run">
<status status="FAIL" start="2026-09-11T07:54:00.000000" elapsed="25.047288">boom</status>
</test>
</suite>
</robot>
"""


class TestIndexRecording(avocado.Test):
    """
    :avocado: tags=reporting
    """
    # Own SEINE_LOG_DIR per test: record()/entries() share one file at
    # a fixed path under it, so two tests sharing a directory would see
    # each other's entries.
    def setUp(self):
        os.environ["SEINE_LOG_DIR"] = self.workdir

    def test_a_recorded_run_comes_back_from_entries(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        os.makedirs(outdir)
        testindex.record(["a.yaml"], outdir,
                         [_outcome("seine test.t one")], started=1000.0)
        [entry] = testindex.entries()
        self.assertEqual(entry["dir"], os.path.join("tests", "20260911-000000"))
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["started"], 1000.0)
        self.assertEqual(entry["tests"], [_outcome("seine test.t one")])

    def test_digest_matches_utils_digest_of_the_same_files(self):
        from seine.utils import digest
        outdir = os.path.join(self.workdir, "tests", "d")
        os.makedirs(outdir)
        files = ["a.yaml", "b.yaml"]
        testindex.record(files, outdir, [_outcome("s.t")])
        [entry] = testindex.entries()
        self.assertEqual(entry["digest"], digest(files, 8))

    # No outdir -- nothing on disk to catalog, so record() is a no-op
    # rather than writing an entry that points nowhere (same rule as
    # logindex with no logs directory).
    def test_no_outdir_is_a_no_op(self):
        testindex.record(["a.yaml"], None, [_outcome("s.t")])
        self.assertEqual(testindex.entries(), [])
        self.assertFalse(os.path.exists(
            os.path.join(self.workdir, "tests", "index.json")))

    def test_entries_are_newest_first(self):
        for i in range(3):
            outdir = os.path.join(self.workdir, "tests", str(i))
            os.makedirs(outdir)
            testindex.record(["a.yaml"], outdir, [_outcome("s.t")],
                             started=float(i))
        dirs = [e["dir"] for e in testindex.entries()]
        self.assertEqual(dirs, [os.path.join("tests", "2"),
                                os.path.join("tests", "1"),
                                os.path.join("tests", "0")])

    def test_old_entries_are_pruned_past_keep(self):
        for i in range(testindex.KEEP + 5):
            outdir = os.path.join(self.workdir, "tests", str(i))
            os.makedirs(outdir)
            testindex.record(["a.yaml"], outdir, [_outcome("s.t")],
                             started=float(i))
        entries = testindex.entries()
        self.assertEqual(len(entries), testindex.KEEP)
        newest = [os.path.join("tests", str(i))
                  for i in range(testindex.KEEP + 5 - 1, 4, -1)]
        self.assertEqual([e["dir"] for e in entries], newest)

    # The run directory lands in index.json relative to logs_root(),
    # never absolute -- an absolute path bakes in a machine-specific
    # $SEINE_LOG_DIR.
    def test_outdir_under_logs_root_is_stored_relative(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        os.makedirs(outdir)
        testindex.record(["a.yaml"], outdir, [_outcome("s.t")])
        [entry] = testindex.entries()
        self.assertFalse(os.path.isabs(entry["dir"]))

    # A custom --outdir outside the logs root (avocado's workdir, a
    # unit test's temp dir) stays out of the index -- recording those
    # polluted the real catalog with every suite run.
    def test_outdir_outside_logs_root_is_not_indexed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as elsewhere:
            testindex.record(["a.yaml"], elsewhere, [_outcome("s.t")])
            self.assertEqual(testindex.entries(), [])
            self.assertFalse(os.path.exists(
                os.path.join(self.workdir, "tests", "index.json")))

    def test_no_index_file_yet_is_an_empty_list(self):
        self.assertEqual(testindex.entries(), [])

    def test_ok_is_false_when_any_test_failed(self):
        outdir = os.path.join(self.workdir, "tests", "d")
        os.makedirs(outdir)
        testindex.record(["a.yaml"], outdir,
                         [_outcome("s.good"), _outcome("s.bad", status="FAIL")])
        [entry] = testindex.entries()
        self.assertFalse(entry["ok"])

    def test_runs_for_matches_exact_and_short_names(self):
        outdir = os.path.join(self.workdir, "tests", "d")
        os.makedirs(outdir)
        testindex.record(["a.yaml"], outdir,
                         [_outcome("seine test.boots to a login prompt")])
        [entry] = testindex.entries()
        self.assertEqual(len(testindex.runs_for(
            "seine test.boots to a login prompt")), 1)
        # A suite rename changes the qualified name but not the case
        # name -- the short form still finds the run.
        self.assertEqual(len(testindex.runs_for(
            "other suite.boots to a login prompt")), 1)
        self.assertEqual(testindex.runs_for("s.unrelated"), [])

    def test_status_of_finds_the_matching_row(self):
        outdir = os.path.join(self.workdir, "tests", "d")
        os.makedirs(outdir)
        testindex.record(["a.yaml"], outdir,
                         [_outcome("seine test.t", status="FAIL")])
        [entry] = testindex.entries()
        row = testindex.status_of(entry, "seine test.t")
        self.assertEqual(row["status"], "FAIL")
        self.assertIsNone(testindex.status_of(entry, "s.unrelated"))

    # Runs that predate the index have output.xml on disk but no entry
    # -- scan() reads them back so old logs stay visible.
    def test_scan_finds_runs_missing_from_the_index(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        os.makedirs(outdir)
        _write(outdir, "output.xml", MINIMAL_XML)
        self.assertEqual(testindex.entries(), [])
        [entry] = testindex.scan()
        by_name = {t["name"]: t for t in entry["tests"]}
        self.assertIn("seine test.boots to a login prompt and identifies as Linux",
                      by_name)
        self.assertEqual(by_name[
            "seine test.a failed install is reported, not fatal to the whole run"
        ]["status"], "FAIL")
        self.assertFalse(entry["ok"])

    # render_test_node() (seine/tui/render.py) calls scan() on every
    # repaint, including several times a second while a run is live --
    # a second call right after the first must not re-walk the tests/
    # directory (or re-parse XML) within the cache's TTL.
    def test_scan_is_cached_within_its_ttl(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        os.makedirs(outdir)
        _write(outdir, "output.xml", MINIMAL_XML)
        first = testindex.scan()

        second_dir = os.path.join(self.workdir, "tests", "20260911-000001")
        os.makedirs(second_dir)
        _write(second_dir, "output.xml", MINIMAL_XML)
        # A fresh run dir landed, but the cache doesn't know that yet --
        # still returns what the first call saw, not a re-walked result.
        self.assertEqual(testindex.scan(), first)

    # A different SEINE_LOG_DIR (a separate test, or a real rescan
    # elsewhere) must never read another root's cached result back --
    # cache lookups key on logs_root() too, not just elapsed time.
    def test_scan_cache_does_not_leak_across_log_dirs(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        os.makedirs(outdir)
        _write(outdir, "output.xml", MINIMAL_XML)
        testindex.scan()

        other_root = os.path.join(self.workdir, "elsewhere")
        os.environ["SEINE_LOG_DIR"] = other_root
        try:
            self.assertEqual(testindex.scan(), [])
        finally:
            os.environ["SEINE_LOG_DIR"] = self.workdir


class RunSpecHook(avocado.Test):
    """
    :avocado: tags=reporting
    """
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import runner
            self.runner = runner
        os.environ["SEINE_LOG_DIR"] = self.workdir

    def _spec(self, name="spec.yaml"):
        return _write(self.workdir, name, """
test:
  - name: x
    tests:
      - name: t
        steps: [{log: {message: hi}}]
""")

    def test_run_spec_records_an_index_entry(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        self.runner.run_spec([self._spec()], outdir=outdir)
        [entry] = testindex.entries()
        self.assertEqual(entry["dir"], os.path.join("tests", "20260911-000000"))
        self.assertTrue(entry["ok"])
        self.assertEqual([t["name"] for t in entry["tests"]], ["x.t"])

    # A dry run resolves keywords without running them -- PASS there
    # says nothing about the target, so it stays out of the index.
    def test_dryrun_is_not_indexed(self):
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        self.runner.run_spec([self._spec()], outdir=outdir, dryrun=True)
        self.assertEqual(testindex.entries(), [])

    def test_a_failing_run_is_indexed_as_not_ok(self):
        spec = _write(self.workdir, "failing.yaml", """
test:
  - name: x
    tests:
      - name: boom
        steps: [{fail: {msg: deliberately fails}}]
""")
        outdir = os.path.join(self.workdir, "tests", "20260911-000000")
        self.runner.run_spec([spec], outdir=outdir)
        [entry] = testindex.entries()
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["tests"][0]["status"], "FAIL")


class CastFor(avocado.Test):
    """
    :avocado: tags=reporting
    """
    def setUp(self):
        os.environ["SEINE_LOG_DIR"] = self.workdir

    def _run_dir(self, name="20260911-000000", casts=None, global_cast=True):
        import json
        outdir = os.path.join(self.workdir, "tests", name)
        os.makedirs(outdir)
        mapping = {}
        for qualified, basename in (casts or {}).items():
            _write(outdir, basename, "fake cast")
            mapping[qualified] = basename
        if global_cast:
            _write(outdir, "console.cast", "fake global cast")
        with open(os.path.join(outdir, "interactions.json"), "w") as f:
            json.dump({"console_cast": "console.cast" if global_cast else None,
                       "console_casts": mapping}, f)
        return {"digest": "x", "started": 1.0, "dir": os.path.join("tests", name),
                "ok": True, "tests": []}

    def test_prefers_the_tests_own_cast(self):
        entry = self._run_dir(casts={"seine test.boot": "seine_test.boot.cast"})
        self.assertTrue(testindex.cast_for(entry, "seine test.boot").endswith(
            os.path.join("20260911-000000", "seine_test.boot.cast")))

    def test_falls_back_to_the_global_cast(self):
        entry = self._run_dir()
        self.assertTrue(testindex.cast_for(entry, "seine test.boot").endswith(
            os.path.join("20260911-000000", "console.cast")))

    def test_short_name_survives_a_suite_rename(self):
        entry = self._run_dir(casts={"seine test.boot": "seine_test.boot.cast"})
        self.assertTrue(testindex.cast_for(entry, "other suite.boot").endswith(
            "seine_test.boot.cast"))

    def test_no_recording_at_all_is_none(self):
        entry = self._run_dir(global_cast=False)
        self.assertIsNone(testindex.cast_for(entry, "seine test.boot"))

    def test_a_missing_run_dir_is_none(self):
        entry = {"digest": "x", "started": 1.0, "dir": os.path.join("tests", "gone"),
                 "ok": True, "tests": []}
        self.assertIsNone(testindex.cast_for(entry, "seine test.boot"))


if __name__ == "__main__":
    avocado.main()
