#!/usr/bin/env python3

import avocado
import os
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from tests.native_image import native_image

def seine(*args):
    return subprocess.run([sys.executable, "./seine.py"] + list(args),
                          cwd=path_to_sources, capture_output=True, text=True)

# Someone who has just installed seine asks it what it does, and the answer
# has to be the commands it has rather than a complaint about one it does not.
class WhatSeineCanBeAskedToDo(avocado.Test):
    def test(self):
        for flag in ["-h", "--help"]:
            run = seine(flag)
            self.assertEqual(run.returncode, 0, "'%s' failed: %s" % (flag, run.stderr))
            for command in ["build", "plan", "cache", "analyze", "issues", "validate",
                           "inspect", "doctor", "diff", "tui"]:
                self.assertIn(command, run.stdout)

    # Told nothing, or told something it has no command for, it says so on
    # stderr and lists what it does have.
    def test_no_command(self):
        run = seine()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("no command given", run.stderr)
        self.assertIn("build", run.stderr)

    def test_a_command_it_does_not_have(self):
        run = seine("bootstrap")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not a seine command", run.stderr)
        self.assertIn("cache", run.stderr)

    # And each command still answers for its own flags.
    def test_a_command_says_the_rest_itself(self):
        for command in ["build", "plan", "cache", "analyze", "issues", "validate",
                       "inspect", "doctor", "diff", "tui"]:
            run = seine(command, "--help")
            self.assertEqual(run.returncode, 0)
            self.assertIn("Usage:", run.stdout)

NATIVE_IMAGE = native_image()

# 'seine validate': everything 'seine build' does before touching a
# container, and nothing else.
class ValidatingASpecification(avocado.Test):
    def test_a_real_spec_is_valid(self):
        run = seine("validate", NATIVE_IMAGE)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("valid", run.stdout)

    def test_a_missing_file_is_not(self):
        run = seine("validate", "/does/not/exist.yaml")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("couldn't open", run.stderr)

    def test_a_malformed_spec_is_not(self):
        spec = os.path.join(self.workdir, "no-filename.yaml")
        with open(spec, "w") as f:
            f.write("""
                image:
                    partitions:
                        - label: rootfs
                          where: /
            """)
        run = seine("validate", spec)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("filename", run.stderr)

    def test_no_files_is_refused(self):
        run = seine("validate")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("expects one or more specification files", run.stderr)

    # Nothing is written: no baseline recorded for 'seine plan' to diff
    # against, unlike an actual build.
    def test_validating_does_not_touch_the_cache(self):
        cache = os.path.join(self.workdir, "cache")
        env = dict(os.environ, SEINE_CACHE_DIR=cache)
        subprocess.run([sys.executable, "./seine.py", "validate", NATIVE_IMAGE],
                       cwd=path_to_sources, capture_output=True, text=True, env=env)
        self.assertFalse(os.path.isdir(os.path.join(cache, "plans")))

# 'seine doctor': exit status follows whether anything is actually
# missing, not whether it printed anything -- a machine with only notes
# still exits 0.
class TheDoctor(avocado.Test):
    def test_says_something_about_every_group(self):
        run = seine("doctor")
        for group in ["Container engine", "Imaging", "Ansible", "Signing", "Storage"]:
            self.assertIn(group, run.stdout)

    def test_the_tally_matches_the_exit_status(self):
        run = seine("doctor")
        if "0 errors" in run.stdout:
            self.assertEqual(run.returncode, 0)
        else:
            self.assertNotEqual(run.returncode, 0)

    def test_pull_adds_the_debsbom_check(self):
        run = seine("doctor", "--pull")
        self.assertIn("debsbom", run.stdout)
        without = seine("doctor")
        self.assertNotIn("debsbom", without.stdout)

# 'seine diff': the specification form (already 'seine plan''s own diff,
# exposed standalone) and the '--sbom' form (two SPDX files, package by
# package) do not combine.
class Diffing(avocado.Test):
    def test_a_specification_with_nothing_built_yet_still_prints(self):
        env = dict(os.environ, SEINE_CACHE_DIR=os.path.join(self.workdir, "cache"))
        run = subprocess.run([sys.executable, "./seine.py", "diff", NATIVE_IMAGE],
                             cwd=path_to_sources, capture_output=True, text=True, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("distribution:", run.stdout)

    def test_sbom_needs_exactly_two(self):
        run = seine("diff", "--sbom=one.json")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("exactly two", run.stderr)

    def test_sbom_diff_of_two_real_files(self):
        old = os.path.join(self.workdir, "old.spdx.json")
        new = os.path.join(self.workdir, "new.spdx.json")
        with open(old, "w") as f:
            f.write('{"packages": [{"name": "telnet", "versionInfo": "1"}]}')
        with open(new, "w") as f:
            f.write('{"packages": [{"name": "openssh-server", "versionInfo": "2"}]}')
        run = seine("diff", "--sbom=%s" % old, "--sbom=%s" % new)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("+ openssh-server", run.stdout)
        self.assertIn("- telnet", run.stdout)

    def test_no_arguments_at_all_is_refused(self):
        run = seine("diff")
        self.assertNotEqual(run.returncode, 0)

if __name__ == "__main__":
    avocado.main()
