# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import glob
import hashlib
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# Shared scaffolding for the reproducible_disk*.py tests: build the same
# spec twice and check the two images match byte-for-byte. Not an
# avocado.Test itself, so avocado does not try to run it on its own.
class ReproducibleDiskImage:
    # A real timestamp on snapshot.debian.org, so a subclass's spec does
    # not depend on what bookworm's feeds currently serve.
    SNAPSHOT = "20260801T000000Z"

    # The byte offset of the first difference, read in chunks rather
    # than all at once -- these images are large enough that a plain
    # 'a == b' would hold two full copies in memory for no reason.
    CHUNK = 4 * 1024 * 1024

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds a disk image twice; "
                        "this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build a disk image")

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

    # A subclass names its own image via self.FILENAME.
    def image(self, space):
        found = glob.glob(os.path.join(
            space["SEINE_BUILD_DIR"], "deploy", "bookworm", self.FILENAME))
        self.assertEqual(len(found), 1, "no disk image in %s" % space["SEINE_BUILD_DIR"])
        return found[0]

    def firstDifference(self, one, two):
        with open(one, "rb") as f, open(two, "rb") as g:
            offset = 0
            while True:
                a, b = f.read(self.CHUNK), g.read(self.CHUNK)
                if a != b:
                    for i in range(min(len(a), len(b))):
                        if a[i] != b[i]:
                            return "byte offset %d differs (0x%02x vs 0x%02x)" % (
                                offset + i, a[i], b[i])
                    return "one image is longer than the other, at offset %d" % (
                        offset + min(len(a), len(b)))
                if not a:
                    return "no byte difference found, but the digests differ"
                offset += len(a)

    # A subclass provides specification(), writing its own spec file(s)
    # and returning their paths.
    def test(self):
        first = self.space("first")
        second = self.space("second")
        spec = self.specification()

        self.seine(first, ["build", "-v", "--jobs", "2"] + spec, "build-first")
        self.seine(second, ["build", "-v", "--jobs", "2"] + spec, "build-second")

        one, two = self.image(first), self.image(second)
        with open(one, "rb") as f:
            first_digest = hashlib.file_digest(f, "sha256").hexdigest()
        with open(two, "rb") as f:
            second_digest = hashlib.file_digest(f, "sha256").hexdigest()

        self.assertEqual(
            first_digest, second_digest,
            "two builds pinned to the same snapshot (%s) produced different "
            "disk images: %s" % (self.SNAPSHOT, self.firstDifference(one, two)))
