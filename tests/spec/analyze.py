#!/usr/bin/env python3

import atexit
import avocado
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

# Records are written under seine's cache, so the suite gets one of its own
# rather than leaving runs in the machine's.
os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)

from seine import analyze
from seine.build import BuildCmd
from seine.tasks import Task, run

SPEC = """
distribution:
    release: trixie
    architecture: amd64
image:
    filename: analyze-test.img
    partitions:
        - label: rootfs
          where: /
"""

# A record of whatever these steps did, filed under a digest of this test's
# own so that one test's runs are not another's.
def recorded(steps, spec, jobs=1, ok=True):
    path = analyze.record(steps, spec, jobs=jobs, ok=ok)
    with open(path) as f:
        return json.load(f)

def sleeper(name, seconds, needs=None):
    import time
    return Task(name, lambda: time.sleep(seconds), needs=needs)

# A record as a build would have left it, without having to run one.
def run_of(tasks, spec="fake", started=1770000000, jobs=1, ok=True):
    return {"spec": spec, "graph": "0" * 12, "started": started,
            "jobs": jobs, "ok": ok,
            "tasks": [{"name": name, "needs": needs, "start": start,
                       "end": end, "failed": False}
                      for name, needs, start, end in tasks]}

# And a record as a build would have left on disk, so that what reads them
# back is read too.
def leave(spec, started, tasks, ok=True, jobs=1):
    steps = []
    for name, needs, start, end in tasks:
        step = Task(name, lambda: None, needs=needs)
        step.started, step.ended = started + start, started + end
        steps.append(step)
    analyze.record(steps, spec, jobs=jobs, ok=ok)

def said(report, *args):
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        report(*args)
    return printed.getvalue()

def seine(*args):
    return subprocess.run([sys.executable, "./seine.py"] + list(args),
                          cwd=path_to_sources, capture_output=True, text=True)

class WhatEachStepCostIsKept(avocado.Test):
    def test(self):
        steps = [sleeper("first", 0.05), sleeper("second", 0.0, ["first"])]
        run(steps)
        recorded_run = recorded(steps, "whateachstepcost")

        self.assertEqual([t["name"] for t in recorded_run["tasks"]],
                         ["first", "second"])
        first, second = recorded_run["tasks"]
        # Relative to the start of the run, so the first step starts at
        # zero however long ago the build was.
        self.assertEqual(first["start"], 0.0)
        self.assertGreaterEqual(first["end"], 0.05)
        self.assertGreaterEqual(second["start"], first["end"])
        self.assertEqual(second["needs"], ["first"])
        self.assertEqual(recorded_run["ok"], True)

# The exported root file-system's own size (seine/image.py's own
# 'Image.build()' passes it), not the disk image's -- kept the same way
# every other field here already is, and left out (not zero) for a run
# that never reached the 'tarball' step.
class RootfsSizeIsKeptAlongsideEveryOtherField(avocado.Test):
    def test_kept_when_given(self):
        steps = [sleeper("only", 0.0)]
        run(steps)
        path = analyze.record(steps, "rootfssize", rootfs_size=612345)
        with open(path) as f:
            recorded_run = json.load(f)
        self.assertEqual(recorded_run["rootfs_size"], 612345)

    def test_left_out_when_not_given(self):
        steps = [sleeper("only", 0.0)]
        run(steps)
        recorded_run = recorded(steps, "rootfssize-none")
        self.assertNotIn("rootfs_size", recorded_run)

class AFailedBuildIsRecordedToo(avocado.Test):
    def test(self):
        def no():
            raise ValueError("no")

        # The build that is worth reading afterwards: what ran, ran, and
        # what never started is simply not there.
        steps = [Task("ran", lambda: None),
                 Task("failed", no, needs=["ran"]),
                 Task("never", lambda: None, needs=["failed"])]
        try:
            run(steps)
            self.fail("a failing task was not reported!")
        except ValueError:
            pass

        recorded_run = recorded(steps, "afailedbuild", ok=False)
        self.assertEqual(recorded_run["ok"], False)
        self.assertEqual([t["name"] for t in recorded_run["tasks"]],
                         ["ran", "failed"])
        self.assertEqual(recorded_run["tasks"][0]["failed"], False)
        self.assertEqual(recorded_run["tasks"][1]["failed"], True)

class ARunThatDidNothingIsNotRecorded(avocado.Test):
    def test(self):
        # Nothing ran, so there is nothing to say about where the time
        # went, and an empty record would only be a file to skip later.
        self.assertEqual(
            analyze.record([Task("never", lambda: None)], "didnothing"), None)

class TheSameSpecificationIsFiledUnderTheSameDigest(avocado.Test):
    def test(self):
        digests = []
        for _ in range(2):
            build = BuildCmd()
            build.loads(SPEC)
            digests.append(analyze.spec_digest(build.parse()))
        self.assertEqual(digests[0], digests[1])

    def test_a_different_specification_is_filed_elsewhere(self):
        build = BuildCmd()
        build.loads(SPEC)
        one = analyze.spec_digest(build.parse())

        other = BuildCmd()
        other.loads(SPEC.replace("amd64", "arm64"))
        self.assertNotEqual(one, analyze.spec_digest(other.parse()))

class TheStepsAreDigestedApartFromTheSpecification(avocado.Test):
    def test(self):
        # A resumed build runs fewer steps than the one it resumes -- the
        # packages that were built are no longer waiting to be -- so the
        # graph says something the specification cannot.
        one = [Task("a", lambda: None), Task("b", lambda: None, needs=["a"])]
        self.assertEqual(analyze.graph_digest(one),
                         analyze.graph_digest(list(reversed(one))))
        self.assertNotEqual(analyze.graph_digest(one),
                            analyze.graph_digest(one[:1]))

class OnlySoManyRunsOfOnePlanAreKept(avocado.Test):
    def test(self):
        where = os.path.join(self.workdir, "runs")
        os.makedirs(where)
        for second in range(1770000000, 1770000000 + 25):
            open(os.path.join(where, "%d.json" % second), "w").close()
        analyze._prune(where, keep=20)

        kept = sorted(os.listdir(where))
        self.assertEqual(len(kept), 20)
        # The newest twenty: an old run is of no use once twenty newer
        # ones say what the build costs now.
        self.assertEqual(kept[0], "1770000005.json")
        self.assertEqual(kept[-1], "1770000024.json")

class TheNewestRunIsReadFirst(avocado.Test):
    def test(self):
        for started in [1770000100, 1770000300, 1770000200]:
            steps = [Task("step", lambda: None)]
            steps[0].started, steps[0].ended = started, started + 1
            analyze.record(steps, "newestfirst")

        recorded = analyze.runs("newestfirst")
        self.assertEqual([run["started"] for run in recorded],
                         [1770000300, 1770000200, 1770000100])

class WhereTheTimeWent(avocado.Test):
    def test(self):
        # Two steps running beside each other and a long one after them,
        # which is the shape of every build worth asking about.
        run = run_of([("bootstrap-host", [], 0, 120),
                      ("packages", [], 0, 1200),
                      ("rootfs", ["packages"], 1200, 1500)], jobs=2)
        report = said(analyze.blame, run).splitlines()

        self.assertIn("plan fake", report[0])
        self.assertIn("2 steps at a time", report[0])
        self.assertEqual(
            [line.split()[-1] for line in report if line.startswith("  ")
             and "step time" not in line],
            ["packages", "rootfs", "bootstrap-host"])
        # The pair worth reading together: what the build did, and how
        # long it took to do it.
        self.assertIn("27m00s of step time in 25m00s of build", report[-1])

    def test_a_failed_build_says_which_step_failed(self):
        run = run_of([("packages", [], 0, 60)], ok=False)
        run["tasks"][0]["failed"] = True
        report = said(analyze.blame, run)
        self.assertIn("failed", report.splitlines()[0])
        self.assertIn("(failed)", report)

class WhatTheMachineWasDoing(avocado.Test):
    # /proc/stat as the kernel writes it: user, nice, system, idle,
    # iowait, and the interrupt counters after them.
    def stat(self, ticks):
        where = os.path.join(self.workdir, "stat")
        with open(where, "w") as f:
            f.write("cpu  %s\n" % " ".join(str(t) for t in ticks))
            f.write("cpu0 1 2 3 4 5 6 7 0 0 0\n")
        return where

    def test(self):
        before = analyze._busy(self.stat([100, 0, 50, 800, 50, 0, 0, 0, 0, 0]))
        # 150 ticks of work out of 1000 had, and the idle and the iowait
        # are both time the machine was not working.
        self.assertEqual(before, (150, 1000))

        after = analyze._busy(self.stat([400, 0, 50, 1500, 50, 0, 0, 0, 0, 0]))
        self.assertEqual(analyze._cpu(before, after), 0.3)

    def test_a_machine_that_does_not_answer_is_not_sampled(self):
        # No /proc/stat is not a build that fails: it is a build recorded
        # without any of this.
        self.assertEqual(analyze._busy(os.path.join(self.workdir, "nope")),
                         None)
        with analyze.watching(stat=os.path.join(self.workdir, "nope")) as m:
            self.assertEqual(m.watcher, None)
        self.assertEqual(m.samples, [])

    def test_it_is_watched_while_the_build_runs(self):
        import time as clock
        with analyze.watching(every=0.05, stat="/proc/stat") as machine:
            clock.sleep(0.2)
        self.assertGreater(len(machine.samples), 0)
        for sample in machine.samples:
            self.assertGreaterEqual(sample["cpu"], 0.0)
            self.assertLessEqual(sample["cpu"], 1.0)

    # A caller (the TUI) wanting the load while the build runs, not only
    # once it is over -- the same sample, pushed as it is taken.
    def test_a_sample_is_pushed_live_when_asked(self):
        import time as clock
        pushed = []
        with analyze.watching(every=0.05, stat="/proc/stat",
                              callback=pushed.append) as machine:
            clock.sleep(0.2)
        self.assertGreater(len(pushed), 0)
        self.assertEqual(pushed, machine.samples)

    def test_no_callback_is_the_same_as_before(self):
        import time as clock
        with analyze.watching(every=0.05, stat="/proc/stat") as machine:
            clock.sleep(0.1)
        self.assertGreater(len(machine.samples), 0)

    def test_the_report_says_how_hard_it_was_working(self):
        run = run_of([("rootfs", [], 0, 600)])
        run["cpus"] = 8
        run["samples"] = [{"t": 10, "load": 5.0, "cpu": 0.6},
                          {"t": 20, "load": 5.2, "cpu": 0.64}]
        self.assertIn("the machine was 62% busy, load 5.1 of 8 cpus",
                      said(analyze.blame, run))

class WhatTheBuildWaitedOn(avocado.Test):
    # A kernel and a root file-system built beside each other, both waited
    # for by the image: the kernel is the reason the build took as long as
    # it did, and the root file-system finished inside it.
    BUILD = [("bootstrap", [], 0, 120),
             ("kernel", ["bootstrap"], 120, 1920),
             ("rootfs", ["bootstrap"], 120, 1320),
             ("image", ["kernel", "rootfs"], 1920, 2100)]

    def test(self):
        took, path = analyze.critical(run_of(self.BUILD, jobs=4))
        self.assertEqual(path, ["bootstrap", "kernel", "image"])
        self.assertEqual(took, 120 + 1800 + 180)

    def test_it_is_printed_end_first(self):
        report = said(analyze.critical_chain, run_of(self.BUILD, jobs=4))
        lines = [line for line in report.splitlines() if "└─" in line
                 or line.startswith("image")]
        self.assertEqual(lines[0], "image +3m00s")
        self.assertEqual(lines[1], "  └─kernel +30m00s")
        self.assertEqual(lines[2], "    └─bootstrap +2m00s")
        self.assertIn("35m00s of the 35m00s the build took", report)
        self.assertNotIn("rootfs", report)

    def test_one_step_at_a_time_makes_the_question_moot(self):
        # Every step waited for the one before it, so the chain is the
        # build and a tree of it says nothing.
        report = said(analyze.critical_chain, run_of(self.BUILD, jobs=1))
        self.assertIn("one step at a time", report)
        self.assertNotIn("└─", report)

HOUR = 3600

class ABuildIsTheRunsItWasResumedFrom(avocado.Test):
    def setUp(self):
        # A build that failed on the kernel, a second that failed on the
        # root file-system, and a third that finished -- the shape of a
        # Friday afternoon.
        leave("resumed", 1770000000, [("packages", [], 0, 600),
                                      ("kernel", ["packages"], 600, 900)],
              ok=False)
        leave("resumed", 1770000000 + HOUR, [("kernel", [], 0, 300),
                                             ("rootfs", ["kernel"], 300, 400)],
              ok=False)
        leave("resumed", 1770000000 + 2 * HOUR, [("rootfs", [], 0, 200)])

    def test(self):
        taken = analyze.chain(analyze.runs("resumed"))
        self.assertEqual([run["started"] for run in taken],
                         [1770000000 + 2 * HOUR, 1770000000 + HOUR, 1770000000])

    def test_a_step_that_ran_twice_is_counted_once(self):
        build = analyze.merged(analyze.chain(analyze.runs("resumed")))
        self.assertEqual([task["name"] for task in build["tasks"]],
                         ["packages", "kernel", "rootfs"])
        # What it cost the last time it ran, and not the two attempts
        # added together: nobody waited 500 seconds for that kernel.
        kernel = build["tasks"][1]
        self.assertEqual(kernel["end"] - kernel["start"], 300)
        self.assertEqual(build["runs"], 3)

    def test_the_hours_between_the_runs_are_nobodys_build_time(self):
        build = analyze.merged(analyze.chain(analyze.runs("resumed")))
        # 900 + 400 + 200, and not the two hours somebody spent at lunch
        # and on the fix.
        self.assertEqual(analyze.spent(build), 1500)

    def test_it_says_the_runs_were_joined(self):
        build = analyze.merged(analyze.chain(analyze.runs("resumed")))
        report = said(analyze.blame, build)
        self.assertIn("3 runs joined", report)
        self.assertIn("different steps", report)

class AnEarlierSuccessIsABuildOfItsOwn(avocado.Test):
    def test(self):
        leave("earlier", 1770000000, [("rootfs", [], 0, 100)])
        leave("earlier", 1770000000 + HOUR, [("rootfs", [], 0, 200)], ok=False)
        leave("earlier", 1770000000 + 2 * HOUR, [("rootfs", [], 0, 300)])

        # The failure and the run that fixed it, and not the build that
        # had already finished before either of them.
        taken = analyze.chain(analyze.runs("earlier"))
        self.assertEqual([run["started"] for run in taken],
                         [1770000000 + 2 * HOUR, 1770000000 + HOUR])

    def test_two_builds_in_a_row_stay_apart(self):
        leave("inarow", 1770000000, [("rootfs", [], 0, 100)])
        leave("inarow", 1770000000 + HOUR, [("rootfs", [], 0, 200)])
        self.assertEqual(len(analyze.chain(analyze.runs("inarow"))), 1)

class TheBuildAsAChart(avocado.Test):
    def chart(self, run):
        import xml.etree.ElementTree as elements
        # It has to parse: a chart that a browser refuses is not a chart.
        return elements.fromstring(said(analyze.plot, run))

    def test(self):
        run = run_of([("bootstrap", [], 0, 120),
                      ("kernel", ["bootstrap"], 120, 1920),
                      ("rootfs", ["bootstrap"], 120, 1320)], jobs=2)
        chart = self.chart(run)

        drawn = [rect for rect in chart.iter("{http://www.w3.org/2000/svg}rect")
                 if rect.get("class") == "step"]
        self.assertEqual(len(drawn), 3)
        # The one that took fifteen times as long is fifteen times as
        # wide, which is the whole point of drawing it.
        widths = [float(rect.get("width")) for rect in drawn]
        self.assertAlmostEqual(widths[1] / widths[0], 15.0, places=1)
        self.assertIn("kernel", said(analyze.plot, run))

    def test_a_failed_step_is_drawn_apart(self):
        run = run_of([("kernel", [], 0, 120)], ok=False)
        run["tasks"][0]["failed"] = True
        drawn = [rect for rect in self.chart(run).iter(
            "{http://www.w3.org/2000/svg}rect") if rect.get("class") == "step"]
        self.assertEqual(len(drawn), 1)
        self.assertNotEqual(drawn[0].get("fill"), "#4c78a8")

    def test_the_machine_is_drawn_under_the_steps(self):
        run = run_of([("kernel", [], 0, 120)])
        run["cpus"] = 4
        run["samples"] = [{"t": 10, "load": 3.0, "cpu": 0.5},
                          {"t": 60, "load": 4.0, "cpu": 0.9}]
        chart = self.chart(run)
        lines = list(chart.iter("{http://www.w3.org/2000/svg}polyline"))
        # One for the cores it was burning and one for the load, on the
        # same axis so that the gap between them is readable.
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(len(line.get("points").split()), 2)

class APlanNobodyBuiltHasNothingToReport(avocado.Test):
    def test(self):
        where = os.path.join(self.workdir, "never-built.yml")
        with open(where, "w") as f:
            f.write(SPEC.replace("analyze-test.img", "never-built.img"))

        run = seine("analyze", "blame", where)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("nothing recorded", run.stderr)
        # And it says which plan it looked for, so someone who named the
        # wrong specifications can see that they did.
        self.assertIn("plan", run.stderr)

    def test_a_report_it_does_not_have(self):
        run = seine("analyze", "flamegraph")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not something seine analyzes", run.stderr)
        self.assertIn("blame", run.stderr)

if __name__ == "__main__":
    avocado.main()
