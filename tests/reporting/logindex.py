#!/usr/bin/env python3

import atexit
import avocado
import os
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)

from seine import logindex

# 'logs' is never read from by record()/entries() (it's only stored as
# a relative 'dir', and only the writer -- Image.build()/multiconfig.py
# -- actually put task logs there), so a bare path that doesn't exist is
# fine for every test here.
def _task(name, failed=False, cached=False, log="x.log"):
    return {"name": name, "failed": failed, "cached": cached, "log": log}

class LogIndexRecording(avocado.Test):
    """
    :avocado: tags=reporting
    """
    # Own SEINE_LOG_DIR per test: record()/entries() share one file at
    # a fixed path under it, so two tests sharing a directory would see
    # each other's entries.
    def setUp(self):
        os.environ["SEINE_LOG_DIR"] = self.workdir

    def test_a_recorded_run_comes_back_from_entries(self):
        logindex.record(["a.yaml"], "trixie", "amd64",
                        os.path.join(self.workdir, "abc123", "20260101-000000"),
                        [_task("packages")], True)
        [entry] = logindex.entries()
        self.assertEqual(entry["release"], "trixie")
        self.assertEqual(entry["arch"], "amd64")
        self.assertEqual(entry["dir"], os.path.join("abc123", "20260101-000000"))
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["tasks"], [_task("packages")])

    def test_digest_matches_utils_digest_of_the_same_files(self):
        from seine.utils import digest
        files = ["a.yaml", "b.yaml"]
        logindex.record(files, "trixie", "amd64",
                        os.path.join(self.workdir, "d", "t"), [], True)
        [entry] = logindex.entries()
        self.assertEqual(entry["digest"], digest(files, 8))

    # No log directory -- a verbose, single-job, no-reporter run -- has
    # nothing on disk to catalog, so record() is a no-op rather than
    # writing an entry that points nowhere.
    def test_no_logs_directory_is_a_no_op(self):
        logindex.record(["a.yaml"], "trixie", "amd64", None, [_task("packages")], True)
        self.assertEqual(logindex.entries(), [])
        self.assertFalse(os.path.exists(os.path.join(self.workdir, logindex.INDEX_FILE)))

    def test_entries_are_newest_first(self):
        for i in range(3):
            logindex.record(["a.yaml"], "trixie", "amd64",
                            os.path.join(self.workdir, "d", str(i)), [], True)
        dirs = [e["dir"] for e in logindex.entries()]
        self.assertEqual(dirs, [os.path.join("d", "2"), os.path.join("d", "1"),
                                os.path.join("d", "0")])

    def test_old_entries_are_pruned_past_keep(self):
        for i in range(logindex.KEEP + 5):
            logindex.record(["a.yaml"], "trixie", "amd64",
                            os.path.join(self.workdir, "d", str(i)), [], True)
        entries = logindex.entries()
        self.assertEqual(len(entries), logindex.KEEP)
        # The newest KEEP survived, not an arbitrary subset.
        newest = [os.path.join("d", str(i))
                 for i in range(logindex.KEEP + 5 - 1, 4, -1)]
        self.assertEqual([e["dir"] for e in entries], newest)

    def test_no_index_file_yet_is_an_empty_list(self):
        self.assertEqual(logindex.entries(), [])
