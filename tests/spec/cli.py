#!/usr/bin/env python3

import avocado
import os
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

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
            for command in ["build", "plan", "cache"]:
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
        for command in ["build", "plan", "cache"]:
            run = seine(command, "--help")
            self.assertEqual(run.returncode, 0)
            self.assertIn("Usage:", run.stdout)

if __name__ == "__main__":
    avocado.main()
