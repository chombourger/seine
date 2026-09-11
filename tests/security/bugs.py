#!/usr/bin/env python3

import avocado
import contextlib
import io
import json
import os
import sys
import time
import urllib.parse

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine            import bugs as bugs_module
from seine.bugs        import (Bug, cache_path, fetch, filter_bugs,
                               read_cache, scan, sources_from_sbom, stats)
from seine.secscan    import IssuesCmd

# One UDD answer entry, the fields seine actually reads (the live
# answer carries more -- affects_*, autormdate, ...).
ENTRY = {"id": 1234567, "package": "bash", "source": "bash",
         "severity": "important", "title": "bash breaks on Tuesdays",
         "status": "pending", "last_modified": "2026-01-02"}

WISHLIST_ENTRY = dict(ENTRY, id=1234568, severity="wishlist",
                      title="bash should make coffee")

OTHER_ENTRY = dict(ENTRY, id=1234569, source="openssl", package="openssl",
                   title="openssl is also broken")

def answer(*entries):
    return json.dumps(list(entries)).encode()

# Stands in for the network so the tests can see what URL UDD would
# have been asked for, without reaching it -- same shape as
# tests/security/secscan.py's own 'Engine'.
class Network:
    def __init__(self, testcase, payloads=None):
        # One payload per chunk request, in order; a missing one is an
        # empty answer rather than an error.
        self.testcase = testcase
        self.payloads = list(payloads or [])
        self.urls = []

    def fetch(self, url, timeout=30):
        self.urls.append(url)
        if self.payloads:
            return self.payloads.pop(0).decode()
        return "[]"

    # Reached through a module-global, so it is swapped in place and
    # put back rather than injected.
    def __enter__(self):
        self.saved = bugs_module._fetch_url
        bugs_module._fetch_url = self.fetch
        return self

    def __exit__(self, *args):
        bugs_module._fetch_url = self.saved

def sbom(workdir, names=("bash", "openssl"), name="pc-image-sbom.spdx.json"):
    path = os.path.join(workdir, name)
    with open(path, "w") as f:
        json.dump({"packages": [{"name": n, "versionInfo": "1.0"}
                                for n in names]}, f)
    return path

class QueryShape(avocado.Test):
    def test_release_scopes_affects_and_done_and_merged_are_dropped(self):
        with Network(self) as net:
            fetch(["bash"], distro="trixie")
        self.assertEqual(len(net.urls), 1)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(net.urls[0]).query))
        self.assertEqual(query["release"], "trixie")
        self.assertEqual(query["packages"], "bash")
        self.assertEqual(query["done"], "ign")
        self.assertEqual(query["merged"], "ign")
        self.assertEqual(query["format"], "json")
        # 'rc'/'allbugs' select site-wide sets unioned with the
        # packages -- they must never appear here.
        self.assertNotIn("rc", query)
        self.assertNotIn("allbugs", query)

    def test_an_unknown_distro_queries_any_release(self):
        with Network(self) as net:
            fetch(["bash"], distro="some-local-fork")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(net.urls[0]).query))
        self.assertEqual(query["release"], "any")

    def test_many_sources_go_out_in_chunks(self):
        saved, bugs_module.CHUNK_SIZE = bugs_module.CHUNK_SIZE, 2
        self.addCleanup(setattr, bugs_module, "CHUNK_SIZE", saved)
        with Network(self) as net:
            fetch(["a", "b", "c", "d", "e"], distro="trixie")
        self.assertEqual(len(net.urls), 3)

    def test_a_broken_chunk_is_skipped_not_raised(self):
        saved, bugs_module.CHUNK_SIZE = bugs_module.CHUNK_SIZE, 1
        self.addCleanup(setattr, bugs_module, "CHUNK_SIZE", saved)
        with Network(self, payloads=[b"{not json", answer(ENTRY)]) as net:
            found = fetch(["a", "b"], distro="trixie")
        self.assertEqual(len(net.urls), 2)
        self.assertEqual([b.id for b in found], [1234567])

    def test_the_same_bug_twice_is_one_bug(self):
        with Network(self, payloads=[answer(ENTRY, ENTRY)]):
            found = fetch(["bash"], distro="trixie")
        self.assertEqual(len(found), 1)

    def test_entries_without_an_id_or_source_are_skipped(self):
        bad_id = dict(ENTRY, id="not-a-number")
        bad_source = dict(ENTRY, id=999, source="")
        with Network(self, payloads=[answer(bad_id, bad_source, ENTRY)]):
            found = fetch(["bash"], distro="trixie")
        self.assertEqual([b.id for b in found], [1234567])

class SourcesFromSbom(avocado.Test):
    def test_names_are_sorted_and_deduplicated(self):
        path = sbom(self.workdir, names=("openssl", "bash", "bash"))
        self.assertEqual(sources_from_sbom(path), ["bash", "openssl"])

class Caching(avocado.Test):
    def test_a_fresh_cache_is_reused_not_refetched(self):
        path = sbom(self.workdir)
        with Network(self, payloads=[answer(ENTRY)]) as net:
            first = scan(path, distro="trixie")
            second = scan(path, distro="trixie")
        self.assertEqual(first, second)
        self.assertEqual(len(net.urls), 1)
        self.assertTrue(os.path.exists(cache_path(path)))

    def test_rescan_forces_a_fresh_fetch(self):
        path = sbom(self.workdir)
        with Network(self, payloads=[answer(ENTRY), answer(ENTRY)]) as net:
            scan(path, distro="trixie")
            scan(path, distro="trixie", rescan=True)
        self.assertEqual(len(net.urls), 2)

    def test_an_expired_cache_is_refetched(self):
        path = sbom(self.workdir)
        with Network(self, payloads=[answer(ENTRY), answer(ENTRY)]) as net:
            scan(path, distro="trixie")
            recorded = json.load(open(cache_path(path)))
            recorded["fetched_at"] -= bugs_module.CACHE_TTL + 10
            with open(cache_path(path), "w") as f:
                json.dump(recorded, f)
            scan(path, distro="trixie")
        self.assertEqual(len(net.urls), 2)

    def test_no_cache_yet_is_none(self):
        self.assertIsNone(read_cache(sbom(self.workdir)))

class StatsAndFilter(avocado.Test):
    BUGS = [
        Bug(1, "bash", "bash", "important", "t1", "pending", "2026-01-01"),
        Bug(2, "bash", "bash", "normal", "t2", "pending", "2026-01-01"),
        Bug(3, "openssl", "openssl", "grave", "t3", "forwarded", "2026-01-01"),
        Bug(4, "openssl", "openssl", "wishlist", "t4", "pending", "2026-01-01"),
    ]

    def test_stats(self):
        result = stats(self.BUGS)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["sources"], 2)
        self.assertEqual(result["by_severity"], {"important": 1, "normal": 1,
                                                 "grave": 1, "wishlist": 1})
        self.assertEqual(result["by_source"], {"bash": 2, "openssl": 2})

    # The default cut keeps critical/grave/serious/important and drops
    # normal/minor/wishlist -- the "likely matters" rule.
    def test_default_filter_keeps_only_what_likely_matters(self):
        self.assertEqual([b.id for b in filter_bugs(self.BUGS)], [1, 3])

    def test_a_lower_cut_keeps_more(self):
        self.assertEqual([b.id for b in filter_bugs(self.BUGS, min_severity="normal")],
                         [1, 2, 3])

    def test_no_cut_keeps_everything(self):
        self.assertEqual(len(filter_bugs(self.BUGS, min_severity=None)), 4)

    def test_source_narrows_case_insensitively(self):
        self.assertEqual([b.id for b in filter_bugs(self.BUGS, source="OPENSSL",
                                                    min_severity=None)],
                         [3, 4])

    def test_an_unknown_severity_raises_value_error(self):
        with self.assertRaises(ValueError):
            filter_bugs(self.BUGS, min_severity="critical-ish")

class Cli(avocado.Test):
    def _engine(self):
        # Reuses secscan's own container stand-in so --defects tests
        # never reach podman either.
        from seine import secscan as secscan_module
        from tests.security.secscan import Engine
        return secscan_module, Engine

    def test_defects_are_listed_with_severity_and_status(self):
        secscan_module, Engine = self._engine()
        path = sbom(self.workdir, names=("bash",))
        out = io.StringIO()
        with Engine(self, output=b'{"package": "x@1", "vulnerability": {"id": "CVE-1"}}'), \
                Network(self, payloads=[answer(ENTRY, WISHLIST_ENTRY)]), \
                contextlib.redirect_stdout(out):
            IssuesCmd().main(["--sbom", path, "--defects"])
        printed = out.getvalue()
        self.assertIn("#1234567", printed)
        self.assertIn("important", printed)
        self.assertNotIn("should make coffee", printed)

    def test_filter_narrows_defects_by_source(self):
        secscan_module, Engine = self._engine()
        path = sbom(self.workdir)
        out = io.StringIO()
        with Engine(self, output=b""), \
                Network(self, payloads=[answer(ENTRY, OTHER_ENTRY)]), \
                contextlib.redirect_stdout(out):
            IssuesCmd().main(["--sbom", path, "--defects", "--filter", "openssl"])
        printed = out.getvalue()
        self.assertIn("openssl is also broken", printed)
        self.assertNotIn("Tuesdays", printed)

    def test_no_defects_says_so(self):
        secscan_module, Engine = self._engine()
        path = sbom(self.workdir)
        out = io.StringIO()
        with Engine(self, output=b""), \
                Network(self, payloads=[answer()]), \
                contextlib.redirect_stdout(out):
            IssuesCmd().main(["--sbom", path, "--defects"])
        self.assertIn("no defects", out.getvalue())

    def test_a_dead_udd_is_exit_5(self):
        secscan_module, Engine = self._engine()
        path = sbom(self.workdir)
        def dead(url, timeout=30):
            raise OSError("network is unreachable")
        saved = bugs_module._fetch_url
        bugs_module._fetch_url = dead
        self.addCleanup(setattr, bugs_module, "_fetch_url", saved)
        with Engine(self, output=b""), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                IssuesCmd().main(["--sbom", path, "--defects"])
        self.assertEqual(caught.exception.code, 5)

    def test_a_bad_min_severity_is_an_error(self):
        secscan_module, Engine = self._engine()
        path = sbom(self.workdir)
        with Engine(self, output=b""), \
                Network(self, payloads=[answer(ENTRY)]), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                IssuesCmd().main(["--sbom", path, "--defects",
                                  "--min-severity", "critical-ish"])
        self.assertEqual(caught.exception.code, 1)

if __name__ == "__main__":
    avocado.main()
