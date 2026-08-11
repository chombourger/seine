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
