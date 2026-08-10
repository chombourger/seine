#!/usr/bin/env python3

import avocado
import io
import contextlib
import os
import sys

path_to_self   = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.cache import CacheCmd, CACHES

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

class AnUnknownCacheIsRefused(Caches):
    def test(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["clear", "kitchen-sink"])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertTrue(os.path.isdir(self.paths["downloads"]))

if __name__ == "__main__":
    avocado.main()
