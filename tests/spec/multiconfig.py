#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import multiconfig
from seine.build import BuildCmd
from seine.utils import HOST_ARCH

EXAMPLES = os.path.join(path_to_sources, "examples")

# As tests/spec/images.py's own: these do not run unless asked for, since
# a kernel build takes real time even slimmed down.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

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

# Against a real archive and a real sbuild: two groups sharing an
# architecture, each naming its own kernel besides a plain 'busybox' both
# of them ask for identically. What is under test is the sharing itself
# -- busybox built once for the two of them, each kernel built once for
# its own group and not folded into the other's -- so '--packages-only'
# is enough, and this asks nothing of the kernels beyond that they were
# built: unlike tests/spec/images.py's own, nothing here boots one.
class MultiGroupSharesPackagesWithinAnArchCohort(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 21600

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds packages; this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to rebuild a package")

    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space], check=False)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_CACHE_DIR"] = os.path.join(path, "cache")
        environment["SEINE_BUILD_DIR"] = os.path.join(path, "build")
        self.spaces.append(path)
        return environment

    def seine(self, space, args, log):
        where = os.path.join(self.outputdir, "%s.log" % log)
        with open(where, "w") as f:
            run = subprocess.run(
                [sys.executable, "-u", "./seine.py"] + args,
                cwd=path_to_sources, env=space, stdout=f,
                stderr=subprocess.STDOUT)
        with open(where, "r", errors="replace") as f:
            said = f.read()
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))
        return said

    # 'busybox' plain, and a kernel this group alone owns: a real content
    # difference ('revision'), not only a different 'name' -- so the two
    # groups' kernels are genuinely two different builds, not the same
    # stamp under two labels, however this is checked.
    def specification(self, label):
        names = ["common/trixie.yaml", "common/%s.yaml" % HOST_ARCH]
        specs = [os.path.join(EXAMPLES, name) for name in names]
        for spec in specs:
            self.assertTrue(os.path.isfile(spec), "no such specification: %s" % spec)

        where = os.path.join(self.workdir, "%s.yml" % label)
        with open(where, "w") as f:
            f.write(
                "packages:\n"
                "    - source: apt://busybox\n"
                "    - source: apt://linux\n"
                "      name: linux-%(label)s\n"
                "      revision: %(label)s1\n"
                "      extends:\n"
                "          kernel:\n"
                "              config:\n"
                "                  - %(common)s\n"
                "                  - %(arch_fragment)s\n"
                "      profiles:\n"
                "          - pkg.linux.nokerneldbginfo\n"
                "          - pkg.linux.notools\n"
                "          - pkg.linux.nosource\n"
                "          - nodoc\n"
                "          - noudeb\n"
                "image:\n"
                "    filename: %(label)s.img\n"
                "    partitions:\n"
                "        - label: rootfs\n"
                "          where: /\n"
                % {"label": label,
                   "common": os.path.join(EXAMPLES, "configs", "slim-common.fragment"),
                   "arch_fragment": os.path.join(
                       EXAMPLES, "configs", "slim-%s.fragment" % HOST_ARCH)})
        return specs + [where]

    def repository(self, space):
        return os.path.join(space["SEINE_CACHE_DIR"], "packages", "trixie")

    def debs(self, space, pattern):
        return glob.glob(os.path.join(self.repository(space), pattern))

    def test(self):
        space = self.space("shared")
        one = self.specification("one")
        two = self.specification("two")

        # The graph, first: exactly one busybox build, shared by both
        # groups' arch-cohort, before anything is actually built. And
        # linux-one/linux-two, sharing 'apt://linux', get one 'fetch:linux'
        # task between them -- the fetch/prepare split makes this the
        # ordinary same-task-name-same-node case, not a special one.
        planned = self.seine(space, ["build", "--dry-run", "--tasks-only",
                                     "--packages-only"] + one + ["--"] + two,
                             "plan")
        for step in ["fetch:busybox", "fetch:linux"]:
            self.assertEqual(
                len([line for line in planned.splitlines()
                    if line.split()[:1] == [step]]),
                1, "%s was not one shared task" % step)
        self.assertIn("prepare:linux-one", planned)
        self.assertIn("prepare:linux-two", planned)

        # Then the real thing.
        built = self.seine(space, ["build", "-v", "--jobs", "4", "--packages-only"]
                           + one + ["--"] + two, "build")

        self.assertNotEqual(self.debs(space, "busybox_*_%s.deb" % HOST_ARCH), [],
                            "busybox was not built")
        for label in ["one", "two"]:
            self.assertNotEqual(self.debs(space, "*+%s1*" % label), [],
                                "linux-%s's own revision was not built" % label)

        # And the point of this test: 'fetch:linux' is the only thing
        # that should ever talk to apt for the download -- both
        # 'prepare:linux-one'/'prepare:linux-two' should build straight
        # from the copy it left them, neither refetching. 'Get:' is apt's
        # own progress-line prefix while downloading.
        match = re.search(r"output under (\S+)", built)
        self.assertIsNotNone(match, "no log directory reported")
        logs = match.group(1)
        with open(os.path.join(logs, "fetch:linux.log")) as f:
            self.assertIn("Get:", f.read(), "fetch:linux never talked to apt")
        for label in ["one", "two"]:
            with open(os.path.join(logs, "prepare:linux-%s.log" % label)) as f:
                self.assertNotIn("Get:", f.read(),
                                 "prepare:linux-%s fetched from apt itself" % label)
