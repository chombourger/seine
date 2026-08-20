#!/usr/bin/env python3

import avocado
import contextlib
import io
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import gists
from seine.gists import GistCmd

class DefaultDir(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ.pop("SEINE_GISTS_DIR", None)
        os.environ.pop("XDG_DATA_HOME", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_SEINE_GISTS_DIR_overrides_everything_else(self):
        os.environ["XDG_DATA_HOME"] = "/should/be/ignored"
        os.environ["SEINE_GISTS_DIR"] = "/exactly/this"
        self.assertEqual(gists.default_dir(), "/exactly/this")

    def test_falls_back_to_XDG_DATA_HOME(self):
        os.environ["XDG_DATA_HOME"] = self.workdir
        self.assertEqual(gists.default_dir(),
                         os.path.join(self.workdir, "seine", "gists"))

    def test_falls_back_to_the_home_directory_last(self):
        self.assertEqual(gists.default_dir(),
                         os.path.expanduser("~/.local/share/seine/gists"))

class NamesAreValidated(avocado.Test):
    def test_a_bad_name_is_refused_by_every_call_that_takes_one(self):
        for bad in ["Has-Capitals", "has spaces", "../escaping", "trailing-",
                   "-leading", "double--hyphen"]:
            self.assertRaises(ValueError, gists.create, bad, "d", "x:\n",
                              directory=self.workdir)

class ListGists(avocado.Test):
    def test_an_empty_or_missing_directory_gives_an_empty_list(self):
        missing = os.path.join(self.workdir, "does-not-exist")
        self.assertEqual(gists.list_gists(missing), [])

    def test_lists_name_and_description_sorted_by_name(self):
        gists.create("zebra", "the last one", "packages: []\n", directory=self.workdir)
        gists.create("apple", "the first one", "packages: []\n", directory=self.workdir)
        self.assertEqual(
            gists.list_gists(self.workdir),
            [("apple", "the first one"), ("zebra", "the last one")])

    # No sidecar index -- the description is read straight off the
    # file's own first line, so a gist dropped in by hand (no
    # description comment) still lists, just with an empty one.
    def test_a_file_with_no_description_comment_lists_with_an_empty_one(self):
        path = os.path.join(self.workdir, "no-description.yaml")
        with open(path, "w") as f:
            f.write("packages: []\n")
        self.assertEqual(gists.list_gists(self.workdir),
                         [("no-description", "")])

class Create(avocado.Test):
    def test_writes_the_description_as_the_first_line(self):
        gists.create("a-kernel", "page-ref debugging", "packages:\n- x\n",
                    directory=self.workdir)
        with open(os.path.join(self.workdir, "a-kernel.yaml")) as f:
            self.assertEqual(f.read(), "# page-ref debugging\npackages:\n- x\n")

    def test_a_missing_trailing_newline_is_added(self):
        gists.create("no-newline", "d", "packages: []", directory=self.workdir)
        self.assertEqual(gists.read("no-newline", directory=self.workdir),
                         "# d\npackages: []\n")

    def test_refuses_to_overwrite_an_existing_gist(self):
        gists.create("dup", "first", "a: 1\n", directory=self.workdir)
        self.assertRaises(ValueError, gists.create, "dup", "second", "a: 2\n",
                          directory=self.workdir)
        # The refusal didn't touch what was already there.
        self.assertIn("first", gists.read("dup", directory=self.workdir))

class ReadAndDelete(avocado.Test):
    def test_read_gives_back_exactly_what_create_wrote(self):
        gists.create("roundtrip", "d", "packages:\n- x\n", directory=self.workdir)
        self.assertEqual(gists.read("roundtrip", directory=self.workdir),
                         "# d\npackages:\n- x\n")

    def test_read_of_a_missing_gist_raises(self):
        self.assertRaises(OSError, gists.read, "nope", directory=self.workdir)

    def test_delete_removes_the_file(self):
        gists.create("throwaway", "d", "a: 1\n", directory=self.workdir)
        gists.delete("throwaway", directory=self.workdir)
        self.assertEqual(gists.list_gists(self.workdir), [])

    def test_delete_of_a_missing_gist_raises(self):
        self.assertRaises(ValueError, gists.delete, "nope", directory=self.workdir)

# 'GistCmd' reads/writes through 'default_dir()' like a real user would
# run it -- SEINE_GISTS_DIR points that at the test's own directory,
# same as 'tests/spec/cache.py's Caches class does for SEINE_CACHE_DIR.
class Cli(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_GISTS_DIR"] = self.workdir
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_ls_with_nothing_yet_says_so(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            GistCmd().main(["ls"])
        self.assertIn("no gists yet", out.getvalue())

    def test_ls_lists_name_and_description(self):
        gists.create("a-kernel", "page-ref debugging", "packages: []\n",
                    directory=self.workdir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            GistCmd().main(["ls"])
        self.assertIn("a-kernel", out.getvalue())
        self.assertIn("page-ref debugging", out.getvalue())

    def test_show_prints_the_raw_file(self):
        gists.create("a-kernel", "page-ref debugging", "packages: []\n",
                    directory=self.workdir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            GistCmd().main(["show", "a-kernel"])
        self.assertEqual(out.getvalue(), "# page-ref debugging\npackages: []\n")

    def test_show_of_a_missing_gist_fails_with_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                GistCmd().main(["show", "nope"])
        self.assertEqual(caught.exception.code, 1)

    def test_rm_removes_it(self):
        gists.create("throwaway", "d", "a: 1\n", directory=self.workdir)
        GistCmd().main(["rm", "throwaway"])
        self.assertEqual(gists.list_gists(self.workdir), [])

    def test_rm_of_a_missing_gist_fails_with_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                GistCmd().main(["rm", "nope"])
        self.assertEqual(caught.exception.code, 1)

    def test_no_action_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                GistCmd().main([])
        self.assertEqual(caught.exception.code, 1)

    def test_unknown_action_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                GistCmd().main(["frobnicate"])
        self.assertEqual(caught.exception.code, 1)

if __name__ == "__main__":
    avocado.main()
