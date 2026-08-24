#!/usr/bin/env python3

import avocado
import contextlib
import os
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

def seine(*args):
    return subprocess.run([sys.executable, "./seine.py"] + list(args),
                          cwd=path_to_sources, capture_output=True, text=True)

# Every setUp() below needs this: mirrors tests/spec/tui.py's own
# '_tui_required' for the 'test' extra (robotframework) instead.
@contextlib.contextmanager
def _test_extra_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'test' extra (robotframework) is not installed: %s" % e)

def _write(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(text)
    return path

# The smallest 'image:'/'distribution:' BuildCmd.parse() accepts --
# every fixture below needs one, the same as any real specification
# would: 'test:' is a section of an ordinary spec, not something that
# stands alone.
MINIMAL_IMAGE = """
distribution:
  source: debian
  release: bookworm
  architecture: amd64
image:
  filename: x.img
  partitions:
    - label: rootfs
      where: /
      size: 10MiB
"""

# Every keyword here is Robot's own BuiltIn -- no mtda, no real target,
# so these run anywhere the 'test' extra is installed.
BUILTIN_ONLY_SPEC = MINIMAL_IMAGE + """
test:
  - name: builtin only
    variables:
      GREETING: hello
    keywords:
      - name: Greet Loudly
        args: [who]
        steps:
          - log: {message: "${GREETING} ${who}"}
    tests:
      - name: sequential steps and variables
        steps:
          - greet_loudly: {who: world}
          - set: {name: X, value: "1"}
          - should_be_equal: ["${X}", "1"]
      - name: if else branches
        steps:
          - set: {name: GPU, value: NVIDIA}
          - if: "'${GPU}' == 'NVIDIA'"
            then:
              - log: {message: "nvidia path"}
            else:
              - fail: {msg: "wrong branch"}
      - name: for range with a break
        steps:
          - for_range: {as: I, start: 0, stop: 5}
            do:
              - if: "$I == 2"
                then:
                  - break: true
              - set: {name: LAST, value: "${I}"}
          - should_be_equal_as_integers: ["${LAST}", 1]
      - name: for each over a list
        steps:
          - for_each: {as: ITEM, in: [a, b, c]}
            do:
              - set: {name: LAST, value: "${ITEM}"}
          - should_be_equal: ["${LAST}", c]
      # '${{ ... }}' (double braces) evaluates as a real Python
      # expression rather than substituting text -- what makes 'N' a
      # native int instead of the string a plain '${N + 1}' would.
      - name: while with a limit
        steps:
          - set: {name: N, value: "${{0}}"}
          - while: "$N < 3"
            limit: 5s
            do:
              - set: {name: N, value: "${{$N + 1}}"}
          - should_be_equal_as_integers: ["${N}", 3]
      - name: try except catches a matching failure
        steps:
          - try:
              - fail: {msg: "boom not found"}
            except:
              - pattern: "*not found*"
                do:
                  - log: {message: "caught it"}
      - name: a step failing without except still fails the test
        steps:
          - fail: {msg: "deliberately fails"}
"""

class Loading(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import loader, context as ctx
            self.loader = loader
            self.ctx = ctx

    def test_a_step_must_be_a_string_or_one_key_mapping(self):
        with self.ctx.RunContext() as context:
            with self.assertRaises(self.loader.LoadError):
                self.loader.compile(
                    [{"name": "x", "tests": [{"name": "t", "steps": [42]}]}], context)

    def test_a_keyword_step_takes_exactly_one_key(self):
        with self.ctx.RunContext() as context:
            with self.assertRaises(self.loader.LoadError):
                self.loader.compile(
                    [{"name": "x", "tests": [{"name": "t",
                        "steps": [{"log": {"message": "a"}, "extra": 1}]}]}], context)

    def test_default_libraries_are_always_imported(self):
        with self.ctx.RunContext() as context:
            suite = self.loader.compile([{"name": "x", "tests": []}], context)
            imported = [imp.name for imp in suite.resource.imports]
            for library in self.loader.DEFAULT_LIBRARIES:
                self.assertIn(library, imported)

    # A keyword one entry defines is reachable from a test another,
    # unrelated entry contributes -- proves entries share one resource
    # rather than becoming separate suites that can't see each other
    # (see loader.compile()'s own comment on why).
    def test_a_keyword_from_one_entry_is_reachable_from_another(self):
        with self.ctx.RunContext() as context:
            entries = [
                {"name": "shared", "keywords": [
                    {"name": "Greet", "steps": [{"log": {"message": "hi"}}]}]},
                {"name": "user", "tests": [
                    {"name": "uses the shared keyword", "steps": [{"call": "Greet"}]}]},
            ]
            suite = self.loader.compile(entries, context)
            self.assertEqual(len(suite.tests), 1)

    def test_the_same_keyword_defined_differently_twice_is_an_error(self):
        with self.ctx.RunContext() as context:
            entries = [
                {"name": "a", "keywords": [{"name": "Dup", "steps": [{"log": {"message": "a"}}]}]},
                {"name": "b", "keywords": [{"name": "Dup", "steps": [{"log": {"message": "b"}}]}]},
            ]
            with self.assertRaises(self.loader.LoadError):
                self.loader.compile(entries, context)

    # The real shape of side-loading two boards' own top-level specs
    # together, each 'requires:'-ing a shared fragment (conf-accounts,
    # say) on its own: the fragment's own 'test:' entry is reached
    # twice, word for word identical both times -- seine already
    # tolerates a file reached twice everywhere else ('requires:'
    # itself, a 'packages:' entry merged twice), so an identical
    # keyword definition seen again is not a conflict either.
    def test_the_exact_same_keyword_definition_twice_is_tolerated(self):
        with self.ctx.RunContext() as context:
            same = {"name": "Log In", "steps": [{"log": {"message": "hi"}}]}
            entries = [
                {"name": "a", "keywords": [dict(same)],
                 "tests": [{"name": "t", "steps": [{"call": "Log In"}]}]},
                {"name": "b", "keywords": [dict(same)]},
            ]
            suite = self.loader.compile(entries, context)
            self.assertEqual(len(suite.tests), 1)

    # Each entry's own 'setup:' is the default for its own tests only --
    # two unrelated entries (a shared fragment's own suite and a
    # board's own) each declaring one is the ordinary case, not a
    # conflict: this is the exact shape examples/pc-image/test-boot.yaml
    # and examples/rebuild-busybox/busybox.yaml hit when combined on
    # one command line, both declaring their own 'connect_target: {}'.
    def test_each_entrys_own_setup_applies_only_to_its_own_tests(self):
        with self.ctx.RunContext() as context:
            entries = [
                {"name": "a", "setup": {"call": "A"},
                 "keywords": [{"name": "A", "steps": [{"log": {"message": "a"}}]}],
                 "tests": [{"name": "from a", "steps": [{"log": {"message": "x"}}]}]},
                {"name": "b", "setup": {"call": "B"},
                 "keywords": [{"name": "B", "steps": [{"log": {"message": "b"}}]}],
                 "tests": [{"name": "from b", "steps": [{"log": {"message": "y"}}]}]},
            ]
            suite = self.loader.compile(entries, context)
            by_name = {t.name: t for t in suite.tests}
            self.assertEqual(by_name["from a"].setup.name, "A")
            self.assertEqual(by_name["from b"].setup.name, "B")

    # A test's own 'setup:' still overrides its entry's default.
    def test_a_tests_own_setup_overrides_its_entrys_default(self):
        with self.ctx.RunContext() as context:
            entries = [{"name": "a", "setup": {"call": "A"},
                       "keywords": [{"name": "A", "steps": []}, {"name": "C", "steps": []}],
                       "tests": [{"name": "t", "setup": {"call": "C"}, "steps": []}]}]
            suite = self.loader.compile(entries, context)
            self.assertEqual(suite.tests[0].setup.name, "C")

# Real Robot Framework runs, but only against BuiltIn -- proves the
# whole load/compile/run/report path end to end without needing mtda.
class RunningASpec(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import runner
            self.runner = runner
        self.spec = _write(self.workdir, "spec.yaml", BUILTIN_ONLY_SPEC)

    def test_every_construct_behaves(self):
        result = self.runner.run_spec([self.spec], outdir=self.workdir)
        by_name = {t.name.rsplit(".", 1)[-1]: t for t in result.tests}
        self.assertEqual(by_name["sequential steps and variables"].status, "PASS")
        self.assertEqual(by_name["if else branches"].status, "PASS")
        self.assertEqual(by_name["for range with a break"].status, "PASS")
        self.assertEqual(by_name["for each over a list"].status, "PASS")
        self.assertEqual(by_name["while with a limit"].status, "PASS")
        self.assertEqual(by_name["try except catches a matching failure"].status, "PASS")
        self.assertEqual(by_name["a step failing without except still fails the test"].status,
                         "FAIL")

    # No 'image:'/'distribution:' at all -- a fragment contributing
    # only a 'test:' entry, run on its own, needs neither.
    def test_a_spec_with_only_a_test_section_runs(self):
        spec = _write(self.workdir, "test-only.yaml", """
test:
  - name: x
    tests:
      - name: t
        steps: [{log: {message: hi}}]
""")
        result = self.runner.run_spec([spec], outdir=self.workdir)
        self.assertEqual(result.tests[0].status, "PASS")

    def test_no_test_section_is_refused(self):
        spec = _write(self.workdir, "no-tests.yaml", MINIMAL_IMAGE)
        with self.assertRaises(self.runner.NoTests):
            self.runner.run_spec([spec], outdir=self.workdir)

    def test_result_ok_is_false_when_anything_failed(self):
        result = self.runner.run_spec([self.spec], outdir=self.workdir)
        self.assertFalse(result.ok)

    def test_summary_counts_pass_and_fail_apart(self):
        result = self.runner.run_spec([self.spec], outdir=self.workdir)
        summary = result.summary()
        self.assertIn("7 tests", summary)
        self.assertIn("6 passed", summary)
        self.assertIn("1 failed", summary)

    def test_output_xml_is_written(self):
        result = self.runner.run_spec([self.spec], outdir=self.workdir)
        self.assertTrue(os.path.isfile(result.output_xml))

    def test_tags_narrow_which_tests_run(self):
        spec = _write(self.workdir, "tagged.yaml", MINIMAL_IMAGE + """
test:
  - name: tagged
    tests:
      - name: smoke one
        tags: [smoke]
        steps: [{log: {message: hi}}]
      - name: slow one
        tags: [slow]
        steps: [{log: {message: hi}}]
""")
        result = self.runner.run_spec([spec], tags=["smoke"], outdir=self.workdir)
        self.assertEqual(len(result.tests), 1)
        self.assertIn("smoke one", result.tests[0].name)

    # A real, hardware-touching keyword's body must not run under
    # 'dryrun' -- this environment has mtda installed (doctor.py's own
    # check_mtda() would say 'ok'), so a non-dry Power Cycle here would
    # either dial real hardware or fail trying to; PASS with dryrun
    # proves Robot never called into it at all, only resolved it.
    def test_dryrun_never_calls_a_real_keyword_body(self):
        spec = _write(self.workdir, "power.yaml", MINIMAL_IMAGE + """
test:
  - name: power
    tests:
      - name: would touch real hardware if not dry-run
        steps:
          - power_cycle: {}
""")
        result = self.runner.run_spec([spec], outdir=self.workdir, dryrun=True)
        self.assertEqual(result.tests[0].status, "PASS")

    # 'requires:' composes 'test:' entries across files the same way it
    # already composes 'playbook:' ones -- the fragment BuildCmd._merge_
    # test() adds is what makes this possible at all.
    def test_test_sections_merge_across_requires(self):
        fragment = _write(self.workdir, "fragment.yaml", """
test:
  - name: from a fragment
    tests:
      - name: contributed by requires
        steps: [{log: {message: hi}}]
""")
        main = _write(self.workdir, "main.yaml", MINIMAL_IMAGE + """
requires:
  - fragment
test:
  - name: from main
    tests:
      - name: contributed directly
        steps: [{log: {message: hi}}]
""")
        result = self.runner.run_spec([main], outdir=self.workdir)
        names = {t.name for t in result.tests}
        self.assertEqual(len(result.tests), 2)
        self.assertTrue(any("contributed by requires" in n for n in names))
        self.assertTrue(any("contributed directly" in n for n in names))

    # Two files each 'requires:'-ing (or naming directly on the command
    # line) their own entry with its own 'setup:' -- the exact shape
    # combining examples/pc-image/main.yaml and
    # examples/rebuild-busybox/busybox.yaml hits, both independently
    # declaring 'connect_target: {}' -- must not be treated as a
    # conflict.
    def test_two_entries_each_with_their_own_setup_do_not_conflict(self):
        a = _write(self.workdir, "a.yaml", MINIMAL_IMAGE + """
test:
  - name: a
    setup: {log: {message: setup-a}}
    tests:
      - name: from a
        steps: [{log: {message: hi}}]
""")
        b = _write(self.workdir, "b.yaml", """
test:
  - name: b
    setup: {log: {message: setup-b}}
    tests:
      - name: from b
        steps: [{log: {message: hi}}]
""")
        result = self.runner.run_spec([a, b], outdir=self.workdir)
        self.assertEqual(len(result.tests), 2)
        self.assertTrue(all(t.status == "PASS" for t in result.tests))

    def test_a_reporter_sees_started_and_finished(self):
        events = []

        class Reporter:
            def started(self, name):
                events.append(("started", name))

            def finished(self, name, failed=False):
                events.append(("finished", name, failed))

            def say(self, text):
                pass

        self.runner.run_spec([self.spec], outdir=self.workdir, reporter=Reporter())
        self.assertIn(("started", "builtin only.sequential steps and variables"), events)
        self.assertIn(("finished", "builtin only.if else branches", False), events)

    # End to end, through a real run_spec() call: 'Get Spec Value' is
    # ImageLibrary's own, needs no mtda, and is 'interesting' enough to
    # land in interactions.json -- proves the listener is actually
    # wired into suite.run(), not just unit-tested against fake data.
    def test_interactions_json_is_written(self):
        spec = _write(self.workdir, "gsv.yaml", MINIMAL_IMAGE + """
test:
  - name: x
    tests:
      - name: t1
        steps: [{get_spec_value: {path: "distribution.architecture"}}]
""")
        import json
        self.runner.run_spec([spec], outdir=self.workdir)
        with open(os.path.join(self.workdir, "interactions.json")) as f:
            data = json.load(f)
        self.assertEqual(data["console_log"], None)  # no console ever connected
        names = [e["keyword"] for e in data["interactions"]]
        self.assertIn("Get Spec Value", names)

class TestCommandLine(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            import robot  # noqa: F401
        self.spec = _write(self.workdir, "spec.yaml", BUILTIN_ONLY_SPEC)

    def test_help(self):
        run = seine("test", "--help")
        self.assertEqual(run.returncode, 0)
        self.assertIn("Usage:", run.stdout)

    def test_no_files_is_refused(self):
        run = seine("test")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("expects one or more specification files", run.stderr)

    def test_exit_status_matches_pass_or_fail(self):
        outdir = os.path.join(self.workdir, "out")
        run = seine("test", "--outdir=%s" % outdir, self.spec)
        self.assertEqual(run.returncode, 1)
        self.assertIn("6 passed", run.stdout)
        self.assertIn("1 failed", run.stdout)

    def test_tags_are_accepted(self):
        outdir = os.path.join(self.workdir, "out")
        run = seine("test", "--outdir=%s" % outdir, "--tags=smoke", self.spec)
        # None of BUILTIN_ONLY_SPEC's tests carry 'smoke' -- an empty,
        # all-passed run, not a refusal.
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("0 tests", run.stdout)

    def test_a_spec_with_no_test_section_exits_2(self):
        spec = _write(self.workdir, "no-tests.yaml", MINIMAL_IMAGE)
        run = seine("test", spec)
        self.assertEqual(run.returncode, 2)
        self.assertIn("no 'test:' section", run.stderr)

    def test_dry_run_flag_touches_no_hardware(self):
        spec = _write(self.workdir, "power.yaml", MINIMAL_IMAGE + """
test:
  - name: power
    tests:
      - name: t
        steps: [{power_cycle: {}}]
""")
        outdir = os.path.join(self.workdir, "out")
        run = seine("test", "--outdir=%s" % outdir, "--dry-run", spec)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("1 passed", run.stdout)

# doctor's own group, tested here rather than tests/spec/doctor.py
# (which the 'test' extra doesn't otherwise touch) -- same shape as its
# neighbouring 'note, not an error' checks.
class Doctor(avocado.Test):
    def test_missing_is_a_note_not_an_error(self):
        import importlib.util
        from seine import doctor
        real = importlib.util.find_spec
        importlib.util.find_spec = lambda name: None if name == "robot" else real(name)
        try:
            check = doctor.check_robotframework()
        finally:
            importlib.util.find_spec = real
        self.assertEqual(check.status, "warn")

    def test_run_includes_it(self):
        from seine import doctor
        self.assertIn("robotframework", [c.name for c in doctor.run()])

# The headless stand-in for the Textual App -- no mtda needed, since
# these only exercise what doesn't touch a real client.
class RunContext(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import context as ctx
            self.ctx = ctx

    def test_enter_exit_with_nothing_connected_is_safe(self):
        with self.ctx.RunContext() as context:
            self.assertIsNotNone(context.target_state)

    def test_call_from_thread_runs_synchronously(self):
        seen = []
        with self.ctx.RunContext() as context:
            context.call_from_thread(seen.append, "x")
        self.assertEqual(seen, ["x"])

    # start_keyword()/end_keyword() are Robot's own listener API --
    # constructed here without a real Robot run, close enough to what
    # Robot actually hands a listener for this filter to be worth
    # testing on its own.
    class _FakeKeywordData:
        KEYWORD = "KEYWORD"
        def __init__(self, type_="KEYWORD", args=()):
            self.type = type_
            self.args = args

    class _FakeKeywordResult:
        def __init__(self, libname, kwname, status="PASS"):
            self.libname = libname
            self.kwname = kwname
            self.status = status

    def test_an_interesting_keyword_is_recorded(self):
        with self.ctx.RunContext(outdir=self.workdir) as context:
            data = self._FakeKeywordData(args=("x",))
            result = self._FakeKeywordResult(
                "seine.testing.library.target.TargetLibrary", "Power Cycle")
            context.start_keyword(data, result)
            context.end_keyword(data, result)
        self.assertEqual(len(context.interactions), 1)
        entry = context.interactions[0]
        self.assertEqual(entry["keyword"], "Power Cycle")
        self.assertEqual(entry["args"], ["x"])
        self.assertEqual(entry["status"], "PASS")

    def test_an_uninteresting_keyword_is_not_recorded(self):
        with self.ctx.RunContext(outdir=self.workdir) as context:
            data = self._FakeKeywordData()
            result = self._FakeKeywordResult("BuiltIn", "Log")
            context.start_keyword(data, result)
            context.end_keyword(data, result)
        self.assertEqual(context.interactions, [])

    # record_artifact() attaches to whichever keyword call is currently
    # open -- the exact "did we send a keypress before this snapshot"
    # question a flat file list can't answer on its own.
    def test_record_artifact_attaches_to_the_open_keyword(self):
        with self.ctx.RunContext(outdir=self.workdir) as context:
            data = self._FakeKeywordData()
            result = self._FakeKeywordResult(
                "seine.testing.library.observation.ObservationLibrary", "Capture Screen Image")
            context.start_keyword(data, result)
            context.record_artifact("video-snapshot", os.path.join(self.workdir, "f.jpg"))
            context.end_keyword(data, result)
        self.assertEqual(len(context.interactions), 1)
        entry = context.interactions[0]
        self.assertEqual(entry["keyword"], "Capture Screen Image")
        self.assertEqual(entry["artifact_kind"], "video-snapshot")
        self.assertEqual(entry["artifact_path"], "f.jpg")

    def test_record_artifact_with_nothing_open_stands_alone(self):
        with self.ctx.RunContext(outdir=self.workdir) as context:
            context.record_artifact("screen", os.path.join(self.workdir, "f.txt"))
        self.assertEqual(len(context.interactions), 1)
        self.assertIsNone(context.interactions[0]["keyword"])

class ImageLibraryTests(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import context as ctx
            from seine.testing.library.image import ImageLibrary
            self.ctx = ctx
            self.ImageLibrary = ImageLibrary

    def test_get_spec_value_reads_a_dotted_path(self):
        with self.ctx.RunContext(spec={"image": {"gpu": "NVIDIA"}}) as context:
            lib = self.ImageLibrary(context)
            self.assertEqual(lib.get_spec_value("image.gpu"), "NVIDIA")

    def test_get_spec_value_without_a_spec_is_refused(self):
        with self.ctx.RunContext() as context:
            lib = self.ImageLibrary(context)
            with self.assertRaises(RuntimeError):
                lib.get_spec_value("image.gpu")

    def test_get_spec_value_reports_the_missing_key(self):
        with self.ctx.RunContext(spec={"image": {}}) as context:
            lib = self.ImageLibrary(context)
            with self.assertRaises(KeyError):
                lib.get_spec_value("image.gpu")

class ObservationLibraryTests(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import context as ctx
            from seine.testing.library.observation import ObservationLibrary
            self.ctx = ctx
            self.ObservationLibrary = ObservationLibrary

    def test_capture_screen_without_a_console_is_refused(self):
        with self.ctx.RunContext() as context:
            lib = self.ObservationLibrary(context)
            with self.assertRaises(RuntimeError):
                lib.capture_screen()

    def test_classify_screen_is_not_implemented_yet(self):
        with self.ctx.RunContext() as context:
            lib = self.ObservationLibrary(context)
            with self.assertRaises(NotImplementedError):
                lib.classify_screen()

if __name__ == "__main__":
    avocado.main()
