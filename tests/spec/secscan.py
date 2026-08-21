#!/usr/bin/env python3

import avocado
import contextlib
import io
import json
import os
import stat
import sys
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine            import secscan as secscan_module
from seine.sbom        import output_path
from seine.secscan    import (Finding, IssuesCmd, cache_path,
                              filter_findings, parse_lines, read_cache, scan, stats)

PC_IMAGE = os.path.join(path_to_sources, "examples", "pc-image", "main.yaml")

# A well-formed line as debsbom's own '-f json' actually writes one
# (shape confirmed against a real scan of examples/pc-image's own SBOM
# while secscan.py was written), plus the optional fields it sometimes
# omits.
CVE_LINE = json.dumps({
    "package": "libtasn1-6@4.19.0-2+deb12u1",
    "purl": "pkg:deb/debian/libtasn1-6@4.19.0-2%2Bdeb12u1?arch=source",
    "vulnerability": {"id": "CVE-2025-13151", "status": "open",
                      "urgency": "not-yet-assigned",
                      "tracker": "https://security-tracker.debian.org/tracker/CVE-2025-13151",
                      "debianbug": 1125063, "bugreport": "https://bugs.debian.org/1125063",
                      "nodsa": "Minor issue"}})

MINIMAL_LINE = json.dumps({
    "package": "linux@6.12.95-1",
    "vulnerability": {"id": "CVE-2004-0230", "status": "open", "urgency": "unimportant"}})

class JSONLinesAreParsedIntoFindings(avocado.Test):
    def test_a_well_formed_line_parses(self):
        findings = parse_lines(CVE_LINE)
        self.assertEqual(findings, [Finding(
            cve="CVE-2025-13151", package="libtasn1-6", version="4.19.0-2+deb12u1",
            urgency="not-yet-assigned", status="open",
            tracker="https://security-tracker.debian.org/tracker/CVE-2025-13151")])

    # 'tracker' (and every other optional field) is missing more often
    # than not -- most of a real scan's findings carry no 'nodsa'/
    # 'debianbug'/'bugreport' at all.
    def test_missing_optional_fields_default_to_empty(self):
        findings = parse_lines(MINIMAL_LINE)
        self.assertEqual(findings[0].tracker, "")

    # A truncated write (the scan killed mid-stream, say) must not take
    # the rest of an otherwise-good scan down with it.
    def test_a_malformed_line_is_skipped_not_raised(self):
        text = "\n".join([CVE_LINE, "{not valid json", MINIMAL_LINE, ""])
        findings = parse_lines(text)
        self.assertEqual([f.cve for f in findings], ["CVE-2025-13151", "CVE-2004-0230"])

    # Structurally valid JSON that just isn't a finding -- no
    # 'vulnerability' object, or one with no 'id' -- is the same kind of
    # "not usable" as bad JSON, not a crash.
    def test_a_line_with_no_vulnerability_id_is_skipped(self):
        text = "\n".join([json.dumps({"package": "foo@1"}),
                          json.dumps({"package": "bar@1", "vulnerability": {"status": "open"}})])
        self.assertEqual(parse_lines(text), [])

class StatsAggregateFindings(avocado.Test):
    def test(self):
        # The same CVE against two packages (a shared library) --
        # 'unique_cves' must not double-count it the way 'total' does.
        findings = [
            Finding("CVE-1", "libssl", "1.0", "high", "open", ""),
            Finding("CVE-1", "libssl-dev", "1.0", "high", "open", ""),
            Finding("CVE-2", "libssl", "1.0", "low", "open", ""),
            Finding("CVE-3", "vim", "9.0", "unimportant", "undetermined", ""),
        ]
        result = stats(findings)
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["unique_cves"], 3)
        self.assertEqual(result["packages"], 3)
        self.assertEqual(result["by_urgency"], {"high": 2, "low": 1, "unimportant": 1})
        self.assertEqual(result["by_status"], {"open": 3, "undetermined": 1})
        self.assertEqual(result["by_package"], {"libssl": 2, "libssl-dev": 1, "vim": 1})

# Stands in for the container engine so the tests can see what debsbom
# would have been run with, or that it was not run at all, without
# pulling its image -- same shape as tests/spec/sbom.py's own 'Engine'.
class Engine:
    def __init__(self, testcase, output=b""):
        self.testcase = testcase
        self.output = output
        self.commands = []

    def check_output(self, cmd):
        self.commands.append(cmd)
        return self.output

    def cache(self, *names):
        return os.path.join(self.testcase.workdir, "engine-cache", *names)

    # Reached through a module-global, so it is swapped in place and put
    # back rather than injected.
    def __enter__(self):
        self.saved = secscan_module.ContainerEngine
        secscan_module.ContainerEngine = self
        return self

    def __exit__(self, *args):
        secscan_module.ContainerEngine = self.saved

def sbom(workdir, name="pc-image-sbom.spdx.json"):
    path = os.path.join(workdir, name)
    with open(path, "w") as f:
        f.write("{}")
    return path

# scan() always reads settings.sbom2cve_program, so any test calling it
# needs its own settings.json -- otherwise it silently picks up whatever
# is at the real ~/.config/seine/settings.json on the machine running
# the test. Isolated the same way tests/spec/doctor.py's own LLM check
# isolates XDG_CONFIG_HOME.
class Isolated(avocado.Test):
    def setUp(self):
        self._real_xdg = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def tearDown(self):
        if self._real_xdg is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self._real_xdg

class TheBuiltInScannerIsRunWithTheRightArguments(Isolated):
    def test_the_sbom_and_a_persistent_db_cache_are_mounted(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()) as engine:
            scan(path, distro="bookworm")
        self.assertEqual(len(engine.commands), 1)
        cmd = engine.commands[0]
        self.assertIn("%s:/sbom.json:ro,z" % path, cmd)
        self.assertIn("%s:/root/.cache/debsbom:z" % engine.cache("debsbom"), cmd)
        self.assertEqual(cmd[cmd.index(secscan_module.DEBSBOM_IMAGE) + 1:],
                         ["debsbom", "sec-scan", "--update-db", "-t", "spdx", "-f", "json",
                          "--distro", "bookworm", "/sbom.json"])

    # No 'distro' given leaves debsbom's own default (trixie) alone
    # rather than seine silently picking one.
    def test_no_distro_omits_the_flag(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()) as engine:
            scan(path)
        self.assertNotIn("--distro", engine.commands[0])

class ACustomProgramReplacesTheContainer(Isolated):
    # A real (tiny) external program, not another stand-in -- exercising
    # the actual subprocess call rather than mocking it out too, the way
    # tests/spec/sources.py's 'ASourceIsActuallyPulled' does for a real fetch.
    def _write_fake_scanner(self):
        path = os.path.join(self.workdir, "fake-scanner")
        with open(path, "w") as f:
            # Echoes the SBOM path it was given back as the finding's
            # own package name -- proof the real argument reached it,
            # not just that some hard-coded output came back.
            f.write("#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "print(json.dumps({'package': sys.argv[1] + '@1',\n"
                    "                  'vulnerability': {'id': 'CVE-9999-0001',\n"
                    "                                    'status': 'open',\n"
                    "                                    'urgency': 'low'}}))\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def test(self):
        from seine import settings
        current = settings.load()
        current["sbom2cve_program"] = self._write_fake_scanner()
        settings.save(current)

        path = sbom(self.workdir)
        with Engine(self) as engine:
            findings = scan(path)
        self.assertEqual(engine.commands, [])
        self.assertEqual(findings, [Finding("CVE-9999-0001", path, "1", "low", "open", "")])

# A caller that must never trigger a real scan itself (seine/tui/ai.py's
# own read-only 'issues' tool) reads read_cache() directly instead of
# scan() -- 'test_no_cache_yet_is_none' below is exercised with no
# Engine swapped in at all, since a call that reached the container/
# program would be exactly the bug this guards, not just a failed one.
class ReadCacheNeverScans(Isolated):
    def test_no_cache_yet_is_none(self):
        self.assertIsNone(read_cache(sbom(self.workdir)))

    def test_a_fresh_cache_is_returned(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()):
            scan(path)  # populates the cache read_cache() reads back
        self.assertEqual(read_cache(path)[0].cve, "CVE-2025-13151")

    def test_a_stale_cache_is_none(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()):
            scan(path)
        future = time.time() + 10
        os.utime(path, (future, future))
        self.assertIsNone(read_cache(path))

class CachingAvoidsRescanning(Isolated):
    def test_a_fresh_cache_is_reused_not_rerun(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()) as engine:
            first = scan(path)
            second = scan(path)
        self.assertEqual(first, second)
        self.assertEqual(len(engine.commands), 1)
        self.assertTrue(os.path.exists(cache_path(path)))

    # A newer SBOM (a later build wrote over the same path) makes the
    # cache stale even though nothing asked for a rescan.
    def test_a_newer_sbom_invalidates_the_cache(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()) as engine:
            scan(path)
            future = time.time() + 10
            os.utime(path, (future, future))
            scan(path)
        self.assertEqual(len(engine.commands), 2)

    def test_rescan_forces_a_fresh_run_even_with_a_fresh_cache(self):
        path = sbom(self.workdir)
        with Engine(self, output=CVE_LINE.encode()) as engine:
            scan(path)
            scan(path, rescan=True)
        self.assertEqual(len(engine.commands), 2)

FINDINGS = [
    Finding("CVE-1", "libssl", "1.0", "high", "open", ""),
    Finding("CVE-2", "libssl", "1.0", "low", "open", ""),
    Finding("CVE-3", "vim", "9.0", "unimportant", "open", ""),
    Finding("CVE-4", "vim", "9.0", "not-yet-assigned", "undetermined", ""),
]

class FilterFindings(avocado.Test):
    def test_no_filters_returns_everything(self):
        self.assertEqual(filter_findings(FINDINGS), FINDINGS)

    def test_package_narrows_by_name_case_insensitively(self):
        self.assertEqual([f.cve for f in filter_findings(FINDINGS, package="VIM")],
                         ["CVE-3", "CVE-4"])

    def test_a_bad_pattern_raises_value_error(self):
        with self.assertRaises(ValueError):
            filter_findings(FINDINGS, package="(unclosed")

    # Cutting at 'low' keeps 'high' and 'low' (at or above), drops
    # 'unimportant' and 'not-yet-assigned' (below it).
    def test_min_urgency_keeps_at_or_above_the_cutoff(self):
        self.assertEqual([f.cve for f in filter_findings(FINDINGS, min_urgency="low")],
                         ["CVE-1", "CVE-2"])

    def test_an_unknown_urgency_level_raises_value_error(self):
        with self.assertRaises(ValueError):
            filter_findings(FINDINGS, min_urgency="critical")

    def test_package_and_min_urgency_combine(self):
        result = filter_findings(FINDINGS, package="vim", min_urgency="unimportant")
        self.assertEqual([f.cve for f in result], ["CVE-3"])

# 'IssuesCmd' reads settings.sbom2cve_program through scan(), so every
# test here needs its own settings.json -- same isolation secscan's own
# Isolated base class already gives the tests above.
class Cli(Isolated):
    def test_help_prints_usage_and_does_not_exit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            IssuesCmd().main(["--help"])
        self.assertIn("Usage:", out.getvalue())

    def test_no_args_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                IssuesCmd().main([])
        self.assertEqual(caught.exception.code, 1)

    def test_sbom_and_a_spec_file_together_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                IssuesCmd().main(["--sbom", "x.spdx.json", PC_IMAGE])
        self.assertEqual(caught.exception.code, 1)

    def test_sbom_form_scans_the_given_file_directly(self):
        path = sbom(self.workdir)
        out = io.StringIO()
        with Engine(self, output=CVE_LINE.encode()), contextlib.redirect_stdout(out):
            IssuesCmd().main(["--sbom", path])
        self.assertIn("CVE-2025-13151", out.getvalue())

    # SEINE_DEPLOY_DIR redirected the same way the next test does -- this
    # checkout may well have a real SBOM sitting under its own real
    # deploy directory already (from an actual 'seine build --sbom' run
    # against this same example), which would otherwise make this "no
    # SBOM yet" case fail to reproduce.
    def test_spec_form_needs_a_prior_sbom(self):
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "deploy")
        try:
            with self.assertRaises(SystemExit) as caught:
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    IssuesCmd().main([PC_IMAGE])
        finally:
            del os.environ["SEINE_DEPLOY_DIR"]
        self.assertEqual(caught.exception.code, 4)
        self.assertIn("seine build --sbom", err.getvalue())

    # SEINE_DEPLOY_DIR redirected into the test's own workdir -- without
    # it, the dummy SBOM this writes at the build's real output path
    # would land under whatever deploy directory this machine actually
    # uses, the same isolation every other env-var-scoped test here
    # already gives itself.
    def test_spec_form_scans_the_builds_own_sbom_against_its_release(self):
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "deploy")
        try:
            from seine.build import BuildCmd
            build = BuildCmd()
            build.options = dict(build.options, ansible_library=[])
            build.load_all([PC_IMAGE])
            build.parse()
            path = output_path(build.image._output)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("{}")

            out = io.StringIO()
            with Engine(self, output=CVE_LINE.encode()) as engine, \
                    contextlib.redirect_stdout(out):
                IssuesCmd().main([PC_IMAGE])
        finally:
            del os.environ["SEINE_DEPLOY_DIR"]
        self.assertIn("CVE-2025-13151", out.getvalue())
        self.assertIn("%s:/sbom.json:ro,z" % path, engine.commands[0])
        self.assertIn("bookworm", engine.commands[0])

    def test_filter_and_min_urgency_are_applied_to_the_scan(self):
        path = sbom(self.workdir)
        combined = "\n".join([CVE_LINE, MINIMAL_LINE])  # not-yet-assigned, unimportant
        out = io.StringIO()
        with Engine(self, output=combined.encode()), contextlib.redirect_stdout(out):
            IssuesCmd().main(["--sbom", path, "--min-urgency", "unimportant"])
        printed = out.getvalue()
        self.assertIn("CVE-2004-0230", printed)
        self.assertNotIn("CVE-2025-13151", printed)

    def test_no_findings_says_so(self):
        path = sbom(self.workdir)
        out = io.StringIO()
        with Engine(self, output=b""), contextlib.redirect_stdout(out):
            IssuesCmd().main(["--sbom", path])
        self.assertIn("no known CVEs found", out.getvalue())

if __name__ == "__main__":
    avocado.main()
