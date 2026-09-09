#!/usr/bin/env python3

import avocado
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import HOST_ARCH

EXAMPLES = os.path.join(path_to_sources, "examples")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# A real timestamp on snapshot.debian.org, so this test does not depend
# on what bookworm's feeds currently serve.
SNAPSHOT = "20260801T000000Z"

# Builds the same snapshot-pinned spec in two separate build spaces and
# checks the two rootfs tarballs are byte-for-byte identical.
class RootfsIsByteIdenticalAcrossTwoBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds a root file-system twice; "
                        "this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build a root file-system")

    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space], check=False)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        # ansible-playbook lives beside the python running the tests when
        # they are run from a virtual environment, and the build needs it.
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
                cwd=path_to_sources, env=space, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))

    # Written once, in this test's own workdir: both builds must read
    # the exact same file, since its mtime is used as the build epoch.
    def specification(self):
        where = os.path.join(self.workdir, "reproducible.yml")
        with open(where, "w") as f:
            f.write(
                "distribution:\n"
                "    release: bookworm\n"
                "    architecture: %(arch)s\n"
                "    architectures: [%(arch)s]\n"
                "    uri: https://snapshot.debian.org/archive/debian/%(ts)s\n"
                "    feeds:\n"
                "        - suite: bookworm\n"
                "          valid-until: false\n"
                "        - suite: bookworm-updates\n"
                "          valid-until: false\n"
                "        - suite: bookworm-security\n"
                "          uri: https://snapshot.debian.org/archive/debian-security/%(ts)s\n"
                "          valid-until: false\n"
                "packages:\n"
                "    - source: apt://busybox\n"
                "      profiles: [nocheck]\n"
                % {"arch": HOST_ARCH, "ts": SNAPSHOT})
        return [where]

    # The spec file's stem names the tarball (see image.py's own
    # '_rootfs_output()').
    def tarball(self, space):
        found = glob.glob(os.path.join(
            space["SEINE_BUILD_DIR"], "deploy", "bookworm", "reproducible.tar"))
        self.assertEqual(len(found), 1, "no rootfs tarball in %s" % space["SEINE_BUILD_DIR"])
        return found[0]

    # Every member's mode, mtime, type, owner and content hash, by name.
    def manifest(self, tarball):
        found = {}
        with tarfile.open(tarball) as tar:
            for member in tar.getmembers():
                content = tar.extractfile(member).read() if member.isfile() else None
                found[member.name] = (
                    member.mode, member.mtime, member.type,
                    member.linkname, member.uid, member.gid,
                    hashlib.sha256(content).hexdigest() if content is not None else None)
        return found

    # Says which member differs and how, instead of a bare 'not equal'.
    def explainDifference(self, first, second):
        one, two = self.manifest(first), self.manifest(second)
        only_first = sorted(set(one) - set(two))
        only_second = sorted(set(two) - set(one))
        if only_first or only_second:
            return ("member lists differ: only in the first build: %s; "
                    "only in the second: %s" % (only_first[:5], only_second[:5]))
        for name in sorted(one):
            if one[name] != two[name]:
                fields = ["mode", "mtime", "type", "linkname", "uid", "gid", "sha256"]
                changed = [f for f, a, b in zip(fields, one[name], two[name]) if a != b]
                return "'%s' differs: %s" % (name, ", ".join(changed))
        return ("every member matches (name, mode, mtime, type, owner and "
                "content) but the tarballs' own bytes still differ -- header "
                "padding or member order is not pinned")

    def test(self):
        first = self.space("first")
        second = self.space("second")
        spec = self.specification()

        self.seine(first, ["build", "-v", "--jobs", "2"] + spec, "build-first")
        self.seine(second, ["build", "-v", "--jobs", "2"] + spec, "build-second")

        one, two = self.tarball(first), self.tarball(second)
        with open(one, "rb") as f:
            first_digest = hashlib.sha256(f.read()).hexdigest()
        with open(two, "rb") as f:
            second_digest = hashlib.sha256(f.read()).hexdigest()

        self.assertEqual(
            first_digest, second_digest,
            "two builds pinned to the same snapshot (%s) produced different "
            "root file-systems: %s" % (SNAPSHOT, self.explainDifference(one, two)))
