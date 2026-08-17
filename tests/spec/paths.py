#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tui.paths import complete

class Completing(avocado.Test):
    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, self._cwd)

        os.mkdir("etc")
        os.mkdir("etc/nested")
        with open("etc/hostname", "w"):
            pass
        with open("etc/hosts", "w"):
            pass
        with open(".hidden", "w"):
            pass

    def test_a_bare_prefix_matches_relative_to_cwd(self):
        self.assertEqual(complete("et"), ["etc/"])

    def test_a_directory_gets_a_trailing_slash(self):
        self.assertIn("etc/", complete("etc"))

    def test_drilling_into_a_directory(self):
        self.assertEqual(sorted(complete("etc/host")),
                         ["etc/hostname", "etc/hosts"])

    def test_an_exact_file_is_its_own_only_match(self):
        self.assertEqual(complete("etc/hostname"), ["etc/hostname"])

    def test_an_empty_fragment_lists_the_current_directory(self):
        self.assertIn("etc/", complete(""))

    def test_dotfiles_are_hidden_unless_asked_for(self):
        self.assertNotIn(".hidden", complete(""))
        self.assertIn(".hidden", complete("."))

    def test_no_matches_is_an_empty_list_not_an_error(self):
        self.assertEqual(complete("nope-nothing-here"), [])

    def test_an_unreadable_directory_is_an_empty_list_not_an_error(self):
        self.assertEqual(complete("no/such/dir/"), [])

    def test_an_absolute_fragment_completes_from_the_root(self):
        self.assertIn("/etc/", complete("/et"))

if __name__ == "__main__":
    avocado.main()
