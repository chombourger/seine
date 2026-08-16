#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import multiconfig
from seine.build import BuildCmd

# Nothing under here may write into the machine's own cache -- see the
# same header in tests/spec/tasks.py.
os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)

class SplitOnDashDash(avocado.Test):
    def test_one_group_is_everything(self):
        self.assertEqual(multiconfig.split(["a.yml", "b.yml"]),
                         [["a.yml", "b.yml"]])

    def test_two_groups(self):
        self.assertEqual(multiconfig.split(["a.yml", "--", "b.yml"]),
                         [["a.yml"], ["b.yml"]])

    def test_a_group_may_be_several_files(self):
        self.assertEqual(
            multiconfig.split(["a.yml", "common.yml", "--", "b.yml"]),
            [["a.yml", "common.yml"], ["b.yml"]])

    def test_a_trailing_dash_dash_is_an_empty_group(self):
        try:
            multiconfig.split(["a.yml", "--"])
            self.fail("a trailing '--' was accepted!")
        except ValueError:
            pass

    def test_adjacent_dash_dashes_are_an_empty_group(self):
        try:
            multiconfig.split(["a.yml", "--", "--", "b.yml"])
            self.fail("adjacent '--'s were accepted!")
        except ValueError:
            pass

# One group of each of: a plain image, and one naming a package -- enough
# to build a build.image with '.packages' and an output filename, without
# 'requires' or anything else a group here does not need.
def _group(filename, release="trixie", architecture="amd64", package=None,
          feeds=None):
    build = BuildCmd()
    build.loads("""
distribution:
    release: %s
    architecture: %s
    uri: http://example.com/debian
%s
%s
image:
    filename: %s
    partitions:
        - label: rootfs
          where: /
""" % (release, architecture,
      ("    feeds:\n" + "\n".join(
          "        - suite: %s%s" % (
              feed["suite"],
              ("\n          uri: %s" % feed["uri"]) if "uri" in feed else "")
          for feed in feeds)) if feeds else "",
      ("packages:\n    - source: apt://%s\n" % package) if package else "",
      filename))
    build.parse()
    return build

class GroupsShareOneHostBootstrap(avocado.Test):
    def test(self):
        pc = _group("pc.img", architecture="amd64")
        rpi4 = _group("rpi4.img", architecture="arm64", package="linux-rpi4")
        rpi5 = _group("rpi5.img", architecture="arm64", package="linux-rpi5")
        merged = multiconfig.merged_tasks([pc, rpi4, rpi5])
        names = [t.name for t in merged]

        # One host bootstrap for both architectures.
        self.assertEqual(names.count("bootstrap-host"), 1)

        # One shared package graph for the arm64 cohort, bare 'packages'
        # for the amd64 one -- it built nothing, but it is still the only
        # arch-cohort of its kind, so its barrier is not namespaced either.
        self.assertIn("trixie-arm64:fetch:linux-rpi4", names)
        self.assertIn("trixie-arm64:fetch:linux-rpi5", names)
        self.assertIn("trixie-amd64:packages", names)

        # Three separate target bootstraps, never merged.
        for label in ["pc", "rpi4", "rpi5"]:
            self.assertIn("%s:bootstrap-target" % label, names)

        # Each own_tasks() barrier points at its own cohort's packages.
        by_name = {t.name: t for t in merged}
        self.assertIn("trixie-amd64:packages", by_name["pc:rootfs"].needs)
        self.assertIn("trixie-arm64:packages", by_name["rpi4:rootfs"].needs)
        self.assertIn("trixie-arm64:packages", by_name["rpi5:rootfs"].needs)
        # imager.py's own hardcoded 'packages' need, threaded the same way.
        self.assertIn("trixie-amd64:packages", by_name["pc:appliance"].needs)

class OneArchCohortKeepsBareNames(avocado.Test):
    def test(self):
        one = _group("one.img")
        two = _group("two.img")
        merged = multiconfig.merged_tasks([one, two])
        names = [t.name for t in merged]
        self.assertIn("packages", names)
        self.assertNotIn("trixie-amd64:packages", names)
        # Still namespaced per group: two 'rootfs' would collide otherwise.
        self.assertIn("one:rootfs", names)
        self.assertIn("two:rootfs", names)

class PackagesOnlyStopsBeforeOwnTasks(avocado.Test):
    def test(self):
        pc = _group("pc.img")
        pc.options["packages_only"] = True
        rpi4 = _group("rpi4.img", architecture="arm64")
        rpi4.options["packages_only"] = True
        names = [t.name for t in multiconfig.merged_tasks([pc, rpi4])]
        self.assertIn("bootstrap-host", names)
        for step in ["bootstrap-target", "rootfs", "tarball", "disk", "image"]:
            self.assertFalse(any(n.endswith(":%s" % step) or n == step
                                 for n in names),
                             "%s should not be in a --packages-only graph" % step)

class DifferentFilenamesAreRequired(avocado.Test):
    def test(self):
        one = _group("same.img")
        two = _group("same.img", architecture="arm64")
        try:
            multiconfig._check_filenames([one, two])
            self.fail("two groups writing the same file were accepted!")
        except ValueError:
            pass

class ConflictingPackagesAreRejected(avocado.Test):
    def test_the_same_package_twice_is_fine(self):
        one = _group("one.img", architecture="arm64", package="busybox")
        two = _group("two.img", architecture="arm64", package="busybox")
        merged = multiconfig.merged_tasks([one, two])
        # One fetch, not two: same package, same settings, one task. Bare
        # 'fetch:busybox', not namespaced -- one and two are one
        # arch-cohort, the same as OneArchCohortKeepsBareNames above.
        self.assertEqual(
            len([t for t in merged if t.name == "fetch:busybox"]), 1)

    def test_two_different_packages_of_one_name_are_rejected(self):
        # Same name, different setting -- the rpi4/rpi5 case if they both
        # named their kernel package plain 'linux' instead of naming it
        # for the board, as the existing examples already do.
        one = BuildCmd()
        one.loads("""
distribution:
    release: trixie
    architecture: arm64
    uri: http://example.com/debian
packages:
    - source: apt://linux
      cross: false
image:
    filename: one.img
    partitions:
        - label: rootfs
          where: /
""")
        one.parse()
        two = BuildCmd()
        two.loads("""
distribution:
    release: trixie
    architecture: arm64
    uri: http://example.com/debian
packages:
    - source: apt://linux
image:
    filename: two.img
    partitions:
        - label: rootfs
          where: /
""")
        two.parse()
        try:
            multiconfig.merged_tasks([one, two])
            self.fail("two conflicting packages of one name were accepted!")
        except ValueError as e:
            self.assertIn("linux", str(e))

class BuilderImageCollisionIsCaught(avocado.Test):
    def test_same_feeds_is_fine(self):
        pc = _group("pc.img", architecture="amd64")
        rpi4 = _group("rpi4.img", architecture="arm64")
        multiconfig._check_builder_image_collisions([pc, rpi4])

    def test_different_feeds_on_one_release_are_rejected(self):
        pc = _group("pc.img", architecture="amd64")
        rpi4 = _group("rpi4.img", architecture="arm64", feeds=[
            {"suite": "trixie"},
            {"suite": "trixie-firmware", "uri": "http://vendor.example.com"},
        ])
        try:
            multiconfig._check_builder_image_collisions([pc, rpi4])
            self.fail("a builder image collision was accepted!")
        except ValueError as e:
            self.assertIn("builder/debian/trixie", str(e))

# End to end, through the CLI: nothing here needs podman, since
# '--dry-run'/'plan' promise nothing is fetched, built or written.
class DryRunThroughTheCLI(avocado.Test):
    def specs(self):
        base = self.workdir
        written = []
        for name, architecture, package in [
                ("pc", "amd64", None), ("rpi4", "arm64", "linux-rpi4"),
                ("rpi5", "arm64", "linux-rpi5")]:
            path = os.path.join(base, "%s.yml" % name)
            with open(path, "w") as f:
                f.write("""
distribution:
    release: trixie
    architecture: %s
    uri: http://example.com/debian
%s
image:
    filename: %s.img
    partitions:
        - label: rootfs
          where: /
""" % (architecture,
      "packages:\n    - source: apt://%s\n" % package if package else "",
      os.path.join(base, name)))
            written.append(path)
        return written

    def seine(self, args):
        environment = dict(os.environ)
        environment["SEINE_CACHE_DIR"] = tempfile.mkdtemp(
            dir=self.workdir, prefix="cache-")
        return subprocess.run(
            [sys.executable, "./seine.py"] + args, cwd=path_to_sources,
            env=environment, capture_output=True, text=True)

    def test(self):
        pc, rpi4, rpi5 = self.specs()
        result = self.seine(["plan", "--tasks-only", pc, "--", rpi4, "--", rpi5])
        self.assertEqual(result.returncode, 0, result.stderr)
        # As a task, not as a substring: every task needing it prints
        # 'after bootstrap-host' too.
        self.assertEqual(
            len([line for line in result.stdout.splitlines()
                if line.strip() == "bootstrap-host"]), 1)
        self.assertIn("trixie-arm64:fetch:linux-rpi4", result.stdout)
        self.assertIn("trixie-arm64:fetch:linux-rpi5", result.stdout)
        self.assertIn("pc:rootfs", result.stdout)
        self.assertIn("rpi4:rootfs", result.stdout)
        self.assertIn("rpi5:rootfs", result.stdout)

    def test_a_single_group_is_unaffected(self):
        pc, _rpi4, _rpi5 = self.specs()
        result = self.seine(["plan", "--tasks-only", pc])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("  packages", result.stdout)
        self.assertNotIn("pc:", result.stdout)
