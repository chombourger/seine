#!/usr/bin/env python3

import avocado
import os
import sys
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import analyze
from seine import tasks
from seine.build import BuildCmd
from seine.progress import Display

MINIMAL = """
image:
    filename: simple-test.img
    partitions:
        - label: rootfs
          where: /
"""

# 'progress.Display' implements the required 'Reporter' methods
# unchanged, duck-typed -- no subclassing, no ABC to satisfy.
class DisplayIsAlreadyAReporter(avocado.Test):
    def test_the_required_methods_are_there(self):
        display = Display()
        self.assertTrue(callable(display.started))
        self.assertTrue(callable(display.finished))
        self.assertTrue(callable(display.say))

    # output/sampled are optional -- Display has neither.
    def test_the_optional_methods_are_not_required(self):
        display = Display()
        self.assertIsNone(getattr(display, "output", None))
        self.assertIsNone(getattr(display, "sampled", None))

class FakeReporter:
    def __init__(self):
        self.started_names = []
        self.finished_names = []

    def started(self, name):
        self.started_names.append(name)

    def finished(self, name, failed=False):
        self.finished_names.append(name)

    def say(self, text):
        pass

# No podman here: tasks.run is stood in for, same as tests/spec/plan.py
# does for Image.build -- under test is Image.build()'s new plumbing.
class ImageBuildTakesAReporter(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        # BuildCmd.__init__ reads settings.py's jobs default -- isolated
        # so a real settings.json (a persisted 'jobs' != 1) can't turn
        # "single job" into "several" underneath these tests.
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        self.real_run = tasks.run
        self.addCleanup(setattr, tasks, "run", self.real_run)
        self.calls = []

        def fake_run(steps, jobs=1, verbose=False, logs=None, display=None):
            for step in steps:
                if display is not None:
                    display.started(step.name)
                step.started = step.ended = time.time()
                step.failed = False
                if display is not None:
                    display.finished(step.name, failed=False)
            self.calls.append({"jobs": jobs, "verbose": verbose,
                               "logs": logs, "display": display})
        tasks.run = fake_run

        build = BuildCmd()
        build.loads(MINIMAL)
        build.parse()
        self.build = build

    # Unchanged from before 'reporter' existed: no argument, a
    # progress.Display, log directory forced by --verbose/--jobs as always.
    def test_no_reporter_is_the_old_behaviour(self):
        self.build.build()
        call = self.calls[-1]
        self.assertIsInstance(call["display"], Display)
        self.assertIsNotNone(call["logs"])

    # '-v', one job, no reporter: the one combination that has never
    # written a log directory, and still does not.
    def test_verbose_single_job_still_gets_no_logs(self):
        self.build.options["verbose"] = True
        self.build.build()
        self.assertIsNone(self.calls[-1]["logs"])

    # A given reporter is what 'tasks.run()' is handed, and it always
    # gets a log directory -- a reporter has no terminal of its own to
    # fall back on, unlike 'verbose=False' at a real terminal.
    def test_a_given_reporter_is_used_and_always_gets_logs(self):
        reporter = FakeReporter()
        self.build.image.build(reporter=reporter)
        call = self.calls[-1]
        self.assertIs(call["display"], reporter)
        self.assertIsNotNone(call["logs"])
        self.assertTrue(os.path.isdir(call["logs"]))
        self.assertGreater(len(reporter.started_names), 0)
        self.assertEqual(reporter.started_names, reporter.finished_names)

    # The same through 'BuildCmd.build()', the CLI's own entry point --
    # a caller does not have to reach past it into 'Image' directly.
    def test_buildcmd_passes_the_reporter_through_too(self):
        reporter = FakeReporter()
        self.build.build(reporter=reporter)
        self.assertIs(self.calls[-1]["display"], reporter)

    # 'sampled' is optional on a Reporter -- wired to 'analyze.watching()'
    # only when the reporter actually has one.
    def test_sampled_is_wired_when_the_reporter_has_one(self):
        class Sampling(FakeReporter):
            def sampled(self, sample):
                pass

        captured = {}
        real_watching = analyze.watching
        def fake_watching(*a, **kw):
            captured["callback"] = kw.get("callback")
            return real_watching(*a, **kw)
        self.addCleanup(setattr, analyze, "watching", real_watching)
        analyze.watching = fake_watching

        reporter = Sampling()
        self.build.image.build(reporter=reporter)
        self.assertEqual(captured["callback"], reporter.sampled)

    # And nothing breaks for one that does not -- the common case, since
    # neither 'Display' nor a minimal Reporter has to implement it.
    def test_no_sampled_on_the_reporter_is_fine(self):
        self.build.image.build(reporter=FakeReporter())

# The digest-timing regression test that used to live here moved to
# tests/spec/build.py, alongside the fix itself (fix(build): take a
# build's digest before tasks can mutate the spec) -- it needed nothing
# from this file that HEAD did not already have.

if __name__ == "__main__":
    avocado.main()
