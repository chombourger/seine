#!/usr/bin/env python3

import avocado
import os
import sys
import tempfile
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

# BuildCmd() now reads settings.py's jobs default -- pointed at an
# empty, per-run directory so a developer's real settings.json can never
# change how many of these run in parallel.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-build-tests-config-")

from seine import analyze
from seine import settings
from seine import tasks
from seine.build import BuildCmd
from seine.utils import ContainerEngine

# BuildCmd's jobs default: 1 unless a persisted setting overrides it;
# an explicit -j/--jobs still wins either way.
class DefaultJobCount(avocado.Test):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_one_with_no_settings_file(self):
        self.assertEqual(BuildCmd().options["jobs"], 1)

    def test_the_persisted_value_otherwise(self):
        current = settings.load()
        current["jobs"] = 3
        settings.save(current)
        self.assertEqual(BuildCmd().options["jobs"], 3)

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

IMAGELESS = """
distribution:
    release: trixie
    architecture: amd64
    uri: http://example.com/debian
packages:
    - source: apt://busybox
"""

VENDOR_ONLY = """
distribution:
    release: bookworm
    architecture: amd64
    uri: http://example.com/debian
vendor:
    - name: openssl
"""

# A real bug, found live: a spec with no 'image:' section crashed with
# a bare TypeError, since 'BuildCmd.parse()' skipped 'Image.parse()'
# entirely -- hit by a 'multiconfig:' group, which owns no disk of its
# own. Such a spec now parses fully and deploys its root tarball as
# real output.
class AnImageLessSpecificationWithSomethingToBuild(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.spec = os.path.join(self.workdir, "main.yaml")
        with open(self.spec, "w") as f:
            f.write(IMAGELESS)

    def parsed(self):
        build = BuildCmd()
        build.options["files"] = [self.spec]
        build.load_all([self.spec])
        build.parse()
        return build

    def test_it_parses_instead_of_staying_unparsed(self):
        build = self.parsed()
        self.assertIsNotNone(build.image.spec)
        self.assertEqual(len(build.image.packages), 1)

    # Named from the spec file's own basename, scoped under the
    # release, the same as an image-bearing specification's own
    # 'filename:' -- see Image._rootfs_output().
    def test_the_tarball_is_named_after_the_spec_file(self):
        build = self.parsed()
        self.assertEqual(
            build.image._output,
            os.path.join(ContainerEngine.deploy_root(), "trixie", "main.tar"))

    def test_own_tasks_deploy_the_tarball_instead_of_writing_a_disk(self):
        names = {t.name for t in self.parsed().image.tasks()}
        self.assertIn("deploy-rootfs", names)
        self.assertNotIn("disk", names)
        self.assertNotIn("appliance", names)

    # 'tasks.run()' is stubbed the way this file's own
    # 'RecordedDigestSurvivesTaskMutation' stubs it: what is under test
    # is that 'Image.build()' reaches it at all instead of crashing
    # first, not a real container build.
    def test_a_real_build_no_longer_crashes(self):
        build = self.parsed()

        def fake_run(steps, jobs=1, verbose=False, logs=None, display=None):
            for step in steps:
                step.started = step.ended = time.time()
                step.failed = False
        real_run, tasks.run = tasks.run, fake_run
        try:
            build.build()
        finally:
            tasks.run = real_run

# 'build_tarball()' leaves the exported root file-system a scratch file
# under 'ContainerEngine.scratch()' -- '_deploy_tarball()' is what turns
# that into the build's real, persisted output.
class DeployTarballMovesTheScratchFileToItsDeployPath(avocado.Test):
    def test(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        spec = os.path.join(self.workdir, "main.yaml")
        with open(spec, "w") as f:
            f.write(IMAGELESS)
        build = BuildCmd()
        build.options["files"] = [spec]
        build.load_all([spec])
        build.parse()

        scratch = os.path.join(self.workdir, "scratch.tar")
        with open(scratch, "w") as f:
            f.write("stands in for a real exported root file-system")
        build.image._tarball = scratch

        build.image._deploy_tarball()

        self.assertTrue(os.path.isfile(build.image._output))
        self.assertFalse(os.path.isfile(scratch))
        self.assertIsNone(build.image._tarball)

# The other side of the same fix: a specification with neither 'image:'
# nor anything to build ('packages:'/'playbook:') is still refused --
# with a clean message now, instead of the same crash.
class AVendorOnlySpecificationStillRefusesToBuild(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = self.workdir

    def test_parse_still_leaves_it_unparsed(self):
        build = BuildCmd()
        build.loads(VENDOR_ONLY)
        build.parse()
        self.assertIsNone(build.image.spec)

    def test_build_refuses_cleanly_instead_of_crashing(self):
        build = BuildCmd()
        build.loads(VENDOR_ONLY)
        build.parse()
        with self.assertRaises(ValueError) as raised:
            build.build()
        self.assertIn("no 'image:' section", str(raised.exception))

if __name__ == "__main__":
    avocado.main()
