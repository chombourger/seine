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

class ConsoleCastPaths(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import context as ctx
            self.ctx = ctx

    def test_runcontext_has_console_cast_next_to_console_log(self):
        with self.ctx.RunContext(outdir=self.workdir) as context:
            self.assertEqual(context.console_cast_path,
                             os.path.join(self.workdir, "console.cast"))
            self.assertEqual(context.console_log_path,
                             os.path.join(self.workdir, "console.log"))

    def test_runcontext_without_outdir_has_no_cast(self):
        with self.ctx.RunContext() as context:
            self.assertIsNone(context.console_cast_path)
            self.assertIsNone(context.console_log_path)

    def test_cast_header_only_when_no_console_traffic(self):
        spec = _write(self.workdir, "spec.yaml", MINIMAL_IMAGE + """
test:
  - name: x
    tests:
      - name: t
        steps: [{log: {message: hi}}]
""")
        from seine.testing import runner
        runner.run_spec([spec], outdir=self.workdir)
        cast = os.path.join(self.workdir, "console.cast")
        self.assertTrue(os.path.isfile(cast))
        with open(cast, encoding="utf-8") as f:
            lines = f.read().splitlines()
        import json as _json
        header = _json.loads(lines[0])
        self.assertEqual(header["version"], 2)
        self.assertEqual(header["width"], 80)
        self.assertEqual(header["height"], 40)
        self.assertIn("timestamp", header)
        self.assertEqual(header["env"]["TERM"], "xterm-256color")
        # no events, only header
        self.assertEqual(len(lines), 1)
        # console.log was never written
        self.assertFalse(os.path.isfile(os.path.join(self.workdir, "console.log")))

class ConsoleCastWiring(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import context as ctx
            from seine.testing import runner as _runner
            from seine.tui import target as _target
            self.ctx = ctx
            self.runner = _runner
            self.target = _target

    def test_run_spec_creates_cast_and_references_it_in_interactions(self):
        spec = _write(self.workdir, "spec.yaml", MINIMAL_IMAGE + """
test:
  - name: x
    tests:
      - name: t
        steps: [{get_spec_value: {path: "distribution.architecture"}}]
""")
        import json
        self.runner.run_spec([spec], outdir=self.workdir)
        with open(os.path.join(self.workdir, "interactions.json")) as f:
            data = json.load(f)
        # header-only cast is still present, referenced by basename
        self.assertEqual(data["console_cast"], "console.cast")
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "console.cast")))
        # no bytes ever arrived, so console.log stays absent
        self.assertIsNone(data["console_log"])

    def test_run_spec_lists_existing_console_log_and_cast(self):
        spec = _write(self.workdir, "spec.yaml", MINIMAL_IMAGE + """
test:
  - name: x
    tests:
      - name: t
        steps: [{log: {message: hi}}]
""")
        # pre-seed a console.log to prove interactions.json lists it only
        # when it actually exists on disk, same gate console_cast uses
        open(os.path.join(self.workdir, "console.log"), "wb").write(b"hello\n")
        import json
        self.runner.run_spec([spec], outdir=self.workdir)
        with open(os.path.join(self.workdir, "interactions.json")) as f:
            data = json.load(f)
        self.assertEqual(data["console_log"], "console.log")
        self.assertEqual(data["console_cast"], "console.cast")

    def _read_cast(self, path):
        import json as _json
        with open(path, encoding="utf-8") as f:
            raw = f.read().splitlines()
        header = _json.loads(raw[0])
        events = [_json.loads(l) for l in raw[1:]]
        return header, events

    def test_console_adapter_writes_bytes_and_str(self):
        import time as _time
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            adapter = self.target.ConsoleAdapter(ctx)
            adapter.print(b"hello\r\n")
            _time.sleep(0.01)
            adapter.print("world\r\n")
            adapter.close()
        header, events = self._read_cast(os.path.join(self.workdir, "console.cast"))
        self.assertEqual(header["width"], self.target.CONSOLE_COLUMNS)
        self.assertEqual(header["height"], self.target.CONSOLE_LINES)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][1], "o")
        self.assertEqual(events[0][2], "hello\r\n")
        self.assertEqual(events[1][2], "world\r\n")
        # elapsed is monotonic
        self.assertGreaterEqual(events[1][0], events[0][0])
        self.assertGreaterEqual(events[0][0], 0)

    def test_console_adapter_preserves_ansi_escapes(self):
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            adapter = self.target.ConsoleAdapter(ctx)
            adapter.print(b"\x1b[31mRed\x1b[m\r\n")
            adapter.close()
        _, events = self._read_cast(os.path.join(self.workdir, "console.cast"))
        self.assertEqual(events[0][2], "\x1b[31mRed\x1b[m\r\n")

    def test_console_adapter_appends_across_reconnects_without_duplicate_header(self):
        import time as _time
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            a1 = self.target.ConsoleAdapter(ctx)
            a1.print(b"first\r\n")
            a1.close()
            # second connect in same run
            a2 = self.target.ConsoleAdapter(ctx)
            _time.sleep(0.02)
            a2.print(b"second\r\n")
            a2.close()
        import json as _json
        with open(os.path.join(self.workdir, "console.cast"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        header = _json.loads(lines[0])
        events = [_json.loads(l) for l in lines[1:]]
        # exactly one header even after two adapters
        self.assertEqual(len([l for l in lines
                              if isinstance(_json.loads(l), dict)
                              and _json.loads(l).get("version") == 2]), 1)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0][2], "first\r\n")
        self.assertEqual(events[1][2], "second\r\n")
        # second elapsed is relative to first header's timestamp, not zero
        self.assertGreater(events[1][0], events[0][0])
        # raw console.log contains both chunks contiguously
        self.assertEqual(open(os.path.join(self.workdir, "console.log"), "rb").read(),
                         b"first\r\nsecond\r\n")

    def test_ensure_is_idempotent(self):
        import json as _json
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            adapter = self.target.ConsoleAdapter(ctx)
            adapter.print(b"hi\r\n")
            adapter.close()
        # runner's helper must not clobber existing file
        self.runner._ensure_console_cast(ctx)
        self.runner._ensure_console_cast(ctx)
        header, events = self._read_cast(os.path.join(self.workdir, "console.cast"))
        self.assertEqual(len(events), 1)
        # ensure on a fresh context with no prior file creates header-only
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "out")
            os.makedirs(out)
            with self.ctx.RunContext(outdir=out) as ctx2:
                self.runner._ensure_console_cast(ctx2)
            with open(os.path.join(out, "console.cast"), encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(_json.loads(lines[0])["version"], 2)

class ConsoleCastCLI(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            import robot  # noqa: F401

    def test_cli_creates_cast_next_to_logs(self):
        spec = _write(self.workdir, "spec.yaml", BUILTIN_ONLY_SPEC)
        outdir = os.path.join(self.workdir, "out")
        run = seine("test", "--outdir=%s" % outdir, spec)
        # BUILTIN_ONLY_SPEC has one failing test, so exit 1 is expected
        self.assertEqual(run.returncode, 1)
        self.assertTrue(os.path.isfile(os.path.join(outdir, "console.cast")))
        self.assertTrue(os.path.isfile(os.path.join(outdir, "interactions.json")))
        import json
        with open(os.path.join(outdir, "interactions.json")) as f:
            data = json.load(f)
        self.assertEqual(data["console_cast"], "console.cast")
        with open(os.path.join(outdir, "console.cast"), encoding="utf-8") as f:
            header = json.loads(f.readline())
        self.assertEqual(header["version"], 2)
        self.assertEqual(header["width"], 80)
        self.assertEqual(header["height"], 40)

    def test_cli_dry_run_still_creates_cast(self):
        spec = _write(self.workdir, "power.yaml", MINIMAL_IMAGE + """
test:
  - name: power
    tests:
      - name: t
        steps: [{power_cycle: {}}]
""")
        outdir = os.path.join(self.workdir, "out")
        run = seine("test", "--outdir=%s" % outdir, "--dry-run", spec)
        self.assertEqual(run.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(outdir, "console.cast")))

class ConsoleCastPerTest(avocado.Test):
    def setUp(self):
        with _test_extra_required(self):
            from seine.testing import context as ctx
            from seine.testing import runner as _runner
            from seine.tui import target as _target
            self.ctx = ctx
            self.runner = _runner
            self.target = _target

    def _read_cast(self, path):
        import json as _json
        with open(path, encoding="utf-8") as f:
            raw = f.read().splitlines()
        header = _json.loads(raw[0])
        events = [_json.loads(l) for l in raw[1:]]
        return header, events

    def test_each_test_gets_its_own_cast_file(self):
        spec = _write(self.workdir, "spec.yaml", MINIMAL_IMAGE + """
test:
  - name: suiteA
    tests:
      - name: alpha
        steps: [{log: {message: hi}}]
      - name: beta
        steps: [{log: {message: hi}}]
""")
        import json
        self.runner.run_spec([spec], outdir=self.workdir)
        # one global plus one per test
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "console.cast")))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "suiteA.alpha.cast")))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, "suiteA.beta.cast")))
        with open(os.path.join(self.workdir, "interactions.json")) as f:
            data = json.load(f)
        self.assertEqual(data["console_cast"], "console.cast")
        self.assertEqual(data["console_casts"]["suiteA.alpha"], "suiteA.alpha.cast")
        self.assertEqual(data["console_casts"]["suiteA.beta"], "suiteA.beta.cast")
        # header-only per-test casts when no console bytes arrived
        for name in ["suiteA.alpha", "suiteA.beta"]:
            with open(os.path.join(self.workdir, "%s.cast" % name), encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["version"], 2)

    def test_per_test_cast_is_isolated(self):
        import time as _time
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            # simulate two tests sequentially, each with its own console output
            ctx.start_test(
                type("D", (), {"name": "alpha", "type": "TEST"})(),
                type("R", (), {"parent": type("P", (), {"name": "suiteA"})()})())
            a = self.target.ConsoleAdapter(ctx)
            a.print(b"for alpha\r\n")
            _time.sleep(0.01)
            a.close()
            ctx.end_test(
                type("D", (), {"name": "alpha"})(),
                type("R", (), {"parent": type("P", (), {"name": "suiteA"})()})())
            ctx.start_test(
                type("D", (), {"name": "beta", "type": "TEST"})(),
                type("R", (), {"parent": type("P", (), {"name": "suiteA"})()})())
            b = self.target.ConsoleAdapter(ctx)
            b.print(b"for beta\r\n")
            b.close()
            ctx.end_test(
                type("D", (), {"name": "beta"})(),
                type("R", (), {"parent": type("P", (), {"name": "suiteA"})()})())
        _, ev_alpha = self._read_cast(os.path.join(self.workdir, "suiteA.alpha.cast"))
        _, ev_beta = self._read_cast(os.path.join(self.workdir, "suiteA.beta.cast"))
        _, ev_global = self._read_cast(os.path.join(self.workdir, "console.cast"))
        self.assertEqual(len(ev_alpha), 1)
        self.assertEqual(ev_alpha[0][2], "for alpha\r\n")
        self.assertEqual(len(ev_beta), 1)
        self.assertEqual(ev_beta[0][2], "for beta\r\n")
        # global contains both
        self.assertEqual(len(ev_global), 2)
        self.assertEqual([e[2] for e in ev_global], ["for alpha\r\n", "for beta\r\n"])

    def test_adapter_routes_to_current_test_and_global(self):
        import time as _time
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            ctx.start_test(
                type("D", (), {"name": "t", "type": "TEST"})(),
                type("R", (), {"parent": type("P", (), {"name": "x"})()})())
            adapter = self.target.ConsoleAdapter(ctx)
            adapter.print(b"hello from t\r\n")
            adapter.close()
            ctx.end_test(type("D", (), {"name": "t"})(),
                         type("R", (), {"parent": type("P", (), {"name": "x"})()})())
            # output outside any test goes only to global
            adapter2 = self.target.ConsoleAdapter(ctx)
            adapter2.print(b"outside\r\n")
            adapter2.close()
        _, ev_test = self._read_cast(os.path.join(self.workdir, "x.t.cast"))
        _, ev_global = self._read_cast(os.path.join(self.workdir, "console.cast"))
        self.assertEqual(ev_test[0][2], "hello from t\r\n")
        self.assertEqual(len(ev_test), 1)
        self.assertEqual([e[2] for e in ev_global],
                         ["hello from t\r\n", "outside\r\n"])

    # ConsoleAdapter.print() runs on mtda's own background thread while
    # start_test()/end_test() run on Robot's -- a byte routed by reading
    # current_test outside a shared lock could land against a test that
    # is mid-transition. Proven here by holding RunContext's own lock
    # across a print() call from another thread: the write must not
    # happen until the lock is released.
    def test_print_is_serialized_against_a_test_transition(self):
        import threading
        with self.ctx.RunContext(outdir=self.workdir) as ctx:
            ctx.start_test(
                type("D", (), {"name": "t", "type": "TEST"})(),
                type("R", (), {"parent": type("P", (), {"name": "x"})()})())
            adapter = self.target.ConsoleAdapter(ctx)
            ctx._console_lock.acquire()
            wrote = threading.Event()
            def do_print():
                adapter.print(b"late\r\n")
                wrote.set()
            th = threading.Thread(target=do_print)
            th.start()
            try:
                # print() must be blocked on the lock -- give it a
                # generous window to prove it does NOT write meanwhile.
                self.assertFalse(wrote.wait(timeout=0.3))
            finally:
                ctx._console_lock.release()
            self.assertTrue(wrote.wait(timeout=5))
            th.join()
            adapter.close()
            ctx.end_test(type("D", (), {"name": "t"})(),
                         type("R", (), {"parent": type("P", (), {"name": "x"})()})())
        _, events = self._read_cast(os.path.join(self.workdir, "x.t.cast"))
        self.assertEqual(events[0][2], "late\r\n")

    def test_cli_creates_one_cast_per_test(self):
        spec = _write(self.workdir, "spec.yaml", MINIMAL_IMAGE + """
test:
  - name: s
    tests:
      - name: one
        steps: [{log: {message: hi}}]
      - name: two
        steps: [{log: {message: hi}}]
""")
        outdir = os.path.join(self.workdir, "out")
        run = seine("test", "--outdir=%s" % outdir, spec)
        self.assertEqual(run.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(outdir, "console.cast")))
        self.assertTrue(os.path.isfile(os.path.join(outdir, "s.one.cast")))
        self.assertTrue(os.path.isfile(os.path.join(outdir, "s.two.cast")))
        import json
        with open(os.path.join(outdir, "interactions.json")) as f:
            data = json.load(f)
        self.assertEqual(data["console_casts"]["s.one"], "s.one.cast")
        self.assertEqual(data["console_casts"]["s.two"], "s.two.cast")

if __name__ == "__main__":
    avocado.main()
