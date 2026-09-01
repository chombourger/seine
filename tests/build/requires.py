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

# loaded_files is the durable record dump_file() reads back; _loading
# is a transient stack, popped clean by the time load() returns.
class LoadedFilesAreTracked(avocado.Test):
    def write(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_a_single_file_is_recorded(self):
        main = self.write("main.yaml", "distribution:\n    release: trixie\n")
        build = BuildCmd()
        build.load(main)
        self.assertEqual(build.loaded_files, [os.path.realpath(main)])

    # 'requires'-pulled files count too, innermost last -- the order a
    # person reading top to bottom would actually reach them in.
    def test_requires_pulled_files_are_included_in_order(self):
        shared = self.write("shared.yaml", "distribution:\n    release: trixie\n")
        main = self.write("main.yaml", "requires:\n    - shared\n")
        build = BuildCmd()
        build.load(main)
        self.assertEqual(build.loaded_files,
                         [os.path.realpath(main), os.path.realpath(shared)])

    # The same fragment reached by two different chains still loads
    # twice (the test above this class), but is only one file: listed
    # once, not once per chain that reached it.
    def test_a_fragment_listed_twice_is_recorded_once(self):
        self.write("shared.yaml", "distribution:\n    release: trixie\n")
        self.write("left.yaml", "requires:\n    - shared\n")
        self.write("right.yaml", "requires:\n    - shared\n")
        main = self.write("main.yaml", "requires:\n    - left\n    - right\n")
        build = BuildCmd()
        build.load(main)
        self.assertEqual(len(build.loaded_files), 4)  # main, left, right, shared

    # Text handed straight to 'loads()' (a fake spec loaded in a test,
    # say) has no file of its own to record.
    def test_text_loaded_with_loads_is_not_a_file(self):
        build = BuildCmd()
        build.loads("distribution:\n    release: trixie\n")
        self.assertEqual(build.loaded_files, [])

if __name__ == "__main__":
    avocado.main()
