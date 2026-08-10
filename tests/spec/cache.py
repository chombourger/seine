#!/usr/bin/env python3

import avocado
import io
import contextlib
import os
import sys
import tarfile

path_to_self   = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.cache import CacheCmd, CACHES, PORTABLE

# A cache with something in it, under the test's own directory rather than
# the user's -- said the way a user would say it, with the two environment
# variables, so what is tested is where seine really looks.
class Caches(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "build")

        self.paths = {}
        for name in CACHES:
            path = CACHES[name][1]()
            self.assertTrue(path.startswith(self.workdir),
                            "%s is not under the test's directory: %s"
                            % (name, path))
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "blob"), "wb") as f:
                f.write(b"x" * 2048)
            self.paths[name] = path

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def run_cmd(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                CacheCmd().main(argv)
            except SystemExit as e:
                self.assertEqual(e.code, 0)
        return out.getvalue()

class WhatIsCachedIsReported(Caches):
    def test(self):
        shown = self.run_cmd(["info"])
        for name in CACHES:
            self.assertIn(name, shown)
            self.assertIn(self.paths[name], shown)
        # Four caches of 2KiB each, so the total is 8KiB.
        self.assertIn("8.0 KiB", shown)

class OneCacheIsClearedWithoutTheOthers(Caches):
    def test(self):
        self.run_cmd(["clear", "chroots"])
        self.assertFalse(os.path.isdir(self.paths["chroots"]))
        self.assertTrue(os.path.isdir(self.paths["downloads"]))

class EveryCacheIsClearedWhenNoneIsNamed(Caches):
    def test(self):
        self.run_cmd(["clear"])
        for name in CACHES:
            self.assertFalse(os.path.isdir(self.paths[name]))

    def test_all_says_the_same_thing(self):
        self.run_cmd(["clear", "all"])
        for name in CACHES:
            self.assertFalse(os.path.isdir(self.paths[name]))

class ACacheIsCarriedToAnotherMachine(Caches):
    def test(self):
        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        # Scratch is not worth carrying and is left out of the tar.
        with tarfile.open(where) as tar:
            tops = set(name.split("/")[0] for name in tar.getnames())
        self.assertEqual(tops, set(PORTABLE))

        self.run_cmd(["clear"])
        self.run_cmd(["import", where])
        for name in PORTABLE:
            with open(os.path.join(self.paths[name], "blob"), "rb") as f:
                self.assertEqual(len(f.read()), 2048)
        self.assertFalse(os.path.isdir(self.paths["scratch"]))

    def test_one_cache_is_taken_from_the_tar(self):
        where = os.path.join(self.workdir, "caches.tar.gz")
        self.run_cmd(["export", where])
        self.run_cmd(["clear"])
        self.run_cmd(["import", where, "chroots"])
        self.assertTrue(os.path.isfile(os.path.join(self.paths["chroots"], "blob")))
        self.assertFalse(os.path.isdir(self.paths["downloads"]))

    # A cache holds links as well as files -- sbuild names the latest of a
    # package's build logs with one -- and a link that stays inside its
    # cache is carried like anything else.
    def test_a_link_inside_a_cache_survives(self):
        link = os.path.join(self.paths["packages"], "linux-latest.deb")
        os.symlink("blob", link)
        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        self.run_cmd(["clear"])
        self.run_cmd(["import", where])
        self.assertEqual(os.readlink(link), "blob")

    # What a real packages cache holds beyond the .debs. The index and the
    # stamps are carried -- a build only rewrites the index when it has
    # something to add, so an import that rebuilds nothing still leaves apt
    # something to read, and the stamps are what say the .debs are current.
    # The rest is either a lock, a hash cache a build rewrites, or a
    # 130MB build log belonging to a machine that is not this one.
    def test_only_what_another_machine_needs_is_carried(self):
        repository = self.paths["packages"]
        os.makedirs(os.path.join(repository, ".stamps"), exist_ok=True)
        for name in ["Packages", "Packages.gz", ".stamps/linux_0123456789abcdef",
                     "linux_6.1_amd64.changes", "linux_6.1_amd64.buildinfo",
                     ".packages.db", "blob.lock", "lock",
                     "linux_6.1_amd64-2026-08-10T00:00:00Z.build"]:
            open(os.path.join(repository, name), "w").close()
        # sbuild's symlink to the latest log goes with the logs.
        os.symlink("linux_6.1_amd64-2026-08-10T00:00:00Z.build",
                   os.path.join(repository, "linux_6.1_amd64.build"))

        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        with tarfile.open(where) as tar:
            carried = set(name.removeprefix("packages/").rstrip("/")
                          for name in tar.getnames()
                          if name.startswith("packages/"))
        self.assertEqual(carried - {""},
                                  {".stamps", ".stamps/linux_0123456789abcdef",
                                   "Packages", "Packages.gz", "blob",
                                   "linux_6.1_amd64.changes",
                                   "linux_6.1_amd64.buildinfo"})

    # apt makes its 'partial' directory as root inside the container and
    # 0700 with it, so an export that reads every directory it walks does
    # not get as far as writing a tar at all.
    def test_a_directory_that_cannot_be_read_is_not_walked(self):
        partial = os.path.join(self.paths["downloads"], "partial")
        os.makedirs(partial, exist_ok=True)
        open(os.path.join(partial, "half.deb"), "w").close()
        os.chmod(partial, 0o000)
        try:
            where = os.path.join(self.workdir, "caches.tar")
            self.run_cmd(["export", where])
            with tarfile.open(where) as tar:
                self.assertIn("downloads/blob", tar.getnames())
                self.assertNotIn("downloads/partial", tar.getnames())
        finally:
            os.chmod(partial, 0o700)

    def test_scratch_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["export", os.path.join(self.workdir, "x.tar"),
                                 "scratch"])
        self.assertNotEqual(caught.exception.code, 0)

# A tar handed to seine may name anything at all; it is the one place seine
# writes files it did not make itself.
class ATarThatReachesOutOfItsCacheIsRefused(Caches):
    def craft(self, name, kind=tarfile.REGTYPE, target=None):
        where = os.path.join(self.workdir, "evil.tar")
        with tarfile.open(where, "w") as tar:
            member = tarfile.TarInfo(name)
            member.type = kind
            if target is not None:
                member.linkname = target
            member.size = 0
            tar.addfile(member, io.BytesIO(b""))
        return where

    def refuses(self, where):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    CacheCmd().main(["import", where])
        self.assertNotEqual(caught.exception.code, 0)

    def test_a_parent_directory(self):
        self.refuses(self.craft("downloads/../../evil"))

    def test_an_absolute_path(self):
        self.refuses(self.craft("/etc/evil"))

    def test_a_cache_it_does_not_know(self):
        self.refuses(self.craft("elsewhere/evil"))

    def test_a_symlink_out_of_the_cache(self):
        self.refuses(self.craft("downloads/evil", tarfile.SYMTYPE, "/etc/passwd"))
        self.assertFalse(os.path.lexists(os.path.join(self.paths["downloads"],
                                                      "evil")))

    def test_a_symlink_climbing_out_of_the_cache(self):
        self.refuses(self.craft("downloads/evil", tarfile.SYMTYPE, "../../../etc"))

    def test_a_hard_link_out_of_the_cache(self):
        self.refuses(self.craft("downloads/evil", tarfile.LNKTYPE, "/etc/passwd"))

    def test_a_device(self):
        self.refuses(self.craft("downloads/evil", tarfile.CHRTYPE))

class AnUnknownCacheIsRefused(Caches):
    def test(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["clear", "kitchen-sink"])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertTrue(os.path.isdir(self.paths["downloads"]))

if __name__ == "__main__":
    avocado.main()
