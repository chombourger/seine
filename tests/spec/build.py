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

MINIMAL = """
image:
    filename: simple-test.img
    partitions:
        - label: rootfs
          where: /
"""

# A real bug, found live: 'PartitionHandler.compute_sizes()' (called by
# the 'disk' task, seine/image.py's '_prepare_disk()') writes
# '_size'/'_start_mib'/'_end_mib' straight onto the same partition dicts
# the specification holds -- the same mistake already found and fixed
# once for playbooks (ansible_runner.py's '_run_playbooks()', commit
# 27b0267). 'Image.build()' used to take its digest *after* 'tasks.run()'
# had already run every task including 'disk', so a real build's record
# was filed under a digest 'seine plan'/'seine analyze' on freshly
# reloaded files -- which never runs 'disk' -- could never compute again.
class RecordedDigestSurvivesTaskMutation(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        self.real_run = tasks.run
        self.addCleanup(setattr, tasks, "run", self.real_run)

        build = BuildCmd()
        build.loads(MINIMAL)
        build.parse()
        self.build = build

        # Stands in for what the real 'disk' task does mid-build, without
        # a real disk or a real partition table.
        def mutating_run(steps, jobs=1, verbose=False, logs=None, display=None):
            self.build.image.partitionHandler.compute_sizes()
            for step in steps:
                step.started = step.ended = time.time()
                step.failed = False
        tasks.run = mutating_run

    def test_the_recorded_digest_matches_a_fresh_reload(self):
        fresh = BuildCmd()
        fresh.loads(MINIMAL)
        fresh.parse()
        expected = analyze.spec_digest(fresh.spec)

        self.build.build()

        [run] = analyze.runs(expected)
        self.assertTrue(run["ok"])

if __name__ == "__main__":
    avocado.main()
