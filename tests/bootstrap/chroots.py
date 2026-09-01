#!/usr/bin/env python3

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

# As tests/bootstrap/feeds.py: asking a chroot where it lives makes the
# directory, so keep this out of the machine's own cache.
os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)

from seine.sbuild import SbuildChroot

DISTRO = {"source": "debian", "release": "bookworm", "architecture": "amd64",
          "uri": "http://example.com/debian"}

# mmdebstrap, as far as making a file where it was told to goes. What it is
# told is the path inside the builder container, which is the chroots
# directory bind-mounted at /root/.cache/sbuild.
class Builder:
    def __init__(self, where, content=b"a whole tarball", watching=None):
        self.where = where
        self.content = content
        self.watching = watching
        self.wrote = None

    # Written in two halves, with whoever is watching looking in between:
    # what a build reading a chroot as it is made would see.
    def exec(self, args, architecture=None, volumes=None):
        target = [a for a in args if a.startswith("/root/.cache/sbuild/")][0]
        self.wrote = os.path.join(self.where, os.path.basename(target))
        with open(self.wrote, "wb") as f:
            f.write(self.content[:4])
            f.flush()
            if self.watching is not None:
                self.watching()
            f.write(self.content[4:])

class Failing(Builder):
    def exec(self, args, architecture=None, volumes=None):
        super().exec(args, architecture, volumes)
        raise subprocess.CalledProcessError(1, "mmdebstrap")

class TheChrootIsPublishedWhole(avocado.Test):
    def setUp(self):
        self.chroot = SbuildChroot(DISTRO, {}, "amd64")
        self.where = os.path.dirname(self.chroot.path)

    def existing(self, content=b"the chroot that was there"):
        with open(self.chroot.path, "wb") as f:
            f.write(content)
        with open(self.chroot.inputs, "w") as f:
            f.write("0000000000000000\n")

    def test_a_chroot_is_made_where_sbuild_looks_for_it(self):
        self.chroot.create(Builder(self.where))
        with open(self.chroot.path, "rb") as f:
            self.assertEqual(f.read(), b"a whole tarball")
        self.assertFalse(os.path.exists(self.chroot.temporary),
                         "the temporary name was left behind")

    # While mmdebstrap writes, the tarball sbuild would unpack is the one
    # that was already there rather than a half-written one.
    def test_the_tarball_in_place_is_whole_while_another_is_written(self):
        self.existing()
        seen = []

        def look():
            with open(self.chroot.path, "rb") as f:
                seen.append(f.read())

        self.chroot.create(Builder(self.where, watching=look))
        self.assertEqual(seen, [b"the chroot that was there"])
        with open(self.chroot.path, "rb") as f:
            self.assertEqual(f.read(), b"a whole tarball")

    # sbuild takes every '<dist>-<arch>.t<anything>' for a chroot.
    def test_what_is_being_written_is_not_taken_for_a_chroot(self):
        self.assertNotIn("bookworm", os.path.basename(self.chroot.temporary))
        self.assertNotIn("amd64", os.path.basename(self.chroot.temporary))
        self.assertTrue(os.path.basename(self.chroot.temporary).startswith("."))

    def test_a_failed_run_leaves_the_chroot_that_was_there(self):
        self.existing()
        with self.assertRaises(subprocess.CalledProcessError):
            self.chroot.create(Failing(self.where))
        with open(self.chroot.path, "rb") as f:
            self.assertEqual(f.read(), b"the chroot that was there")
        self.assertTrue(os.path.isfile(self.chroot.inputs))
        self.assertFalse(os.path.exists(self.chroot.temporary),
                         "what the failed run wrote was left behind")

if __name__ == "__main__":
    avocado.main()
