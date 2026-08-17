#!/usr/bin/env python3

import avocado
import json
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.sbom_diff import diff, diff_files

OLD = {"packages": [{"name": "base-files", "versionInfo": "12.4"},
                    {"name": "telnet", "versionInfo": "0.17-46"},
                    {"name": "libc6", "versionInfo": "2.36-9"}]}
NEW = {"packages": [{"name": "base-files", "versionInfo": "12.5"},
                    {"name": "openssh-server", "versionInfo": "1:10.0p1-2"},
                    {"name": "libc6", "versionInfo": "2.36-9"}]}

class DiffingTwoSBOMs(avocado.Test):
    def test_an_added_package_is_marked(self):
        self.assertIn("+ openssh-server", diff(OLD, NEW))

    def test_a_removed_package_is_marked(self):
        self.assertIn("- telnet", diff(OLD, NEW))

    def test_a_changed_version_is_marked(self):
        text = diff(OLD, NEW)
        self.assertIn("~ base-files", text)
        self.assertIn("12.4 -> 12.5", text)

    def test_an_unchanged_package_says_nothing(self):
        self.assertNotIn("libc6", diff(OLD, NEW))

    def test_identical_sboms_have_nothing_to_say(self):
        self.assertEqual(diff(OLD, OLD), "no package differences")

    def test_a_package_with_no_name_is_skipped_not_a_crash(self):
        broken = {"packages": [{"versionInfo": "1.0"}]}
        self.assertEqual(diff(broken, broken), "no package differences")

    def test_diff_files_reads_real_files(self):
        old_path = os.path.join(self.workdir, "old.spdx.json")
        new_path = os.path.join(self.workdir, "new.spdx.json")
        with open(old_path, "w") as f:
            json.dump(OLD, f)
        with open(new_path, "w") as f:
            json.dump(NEW, f)
        text = diff_files(old_path, new_path)
        self.assertIn("+ openssh-server", text)

    def test_a_missing_file_is_an_oserror(self):
        self.assertRaises(OSError, diff_files, "/does/not/exist.json",
                          "/also/not.json")

    def test_malformed_json_is_a_valueerror(self):
        path = os.path.join(self.workdir, "broken.json")
        with open(path, "w") as f:
            f.write("{not json")
        self.assertRaises(ValueError, diff_files, path, path)

if __name__ == "__main__":
    avocado.main()
