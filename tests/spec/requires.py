#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class RequiresLoops(avocado.Test):
    def write(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    # Two files reaching for each other used to recurse until Python gave
    # up, and the traceback named neither of them.
    def test_two_files_requiring_each_other_are_reported(self):
        self.write("second.yaml", "requires:\n    - first\n")
        first = self.write("first.yaml", "requires:\n    - second\n")

        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.load(first)
        self.assertIn("loops", str(caught.exception))
        self.assertIn("second.yaml", str(caught.exception))

    def test_a_file_requiring_itself_is_reported(self):
        first = self.write("first.yaml", "requires:\n    - first\n")

        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.load(first)
        self.assertIn("loops", str(caught.exception))

    # A fragment two files both list is not a loop: it is composition, and
    # it went on working.
    def test_a_fragment_listed_twice_still_loads(self):
        self.write("shared.yaml", "distribution:\n    release: trixie\n")
        self.write("left.yaml", "requires:\n    - shared\n")
        self.write("right.yaml", "requires:\n    - shared\n")
        main = self.write("main.yaml", "requires:\n    - left\n    - right\n")

        build = BuildCmd()
        spec = build.load(main)
        self.assertEqual(spec["distribution"]["release"], "trixie")

if __name__ == "__main__":
    avocado.main()
