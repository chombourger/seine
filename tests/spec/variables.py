#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

# A fragment reads what the specification that reached for it has already
# said, which is how one file can speak of an architecture it does not name
# itself.
class SubstitutionsAreResolved(avocado.Test):
    def test_a_fragment_reads_the_specification_so_far(self):
        build = BuildCmd()
        build.loads("""
            distribution:
                architecture: arm64
        """)
        build.loads("""
            imager:
                kernel: linux-image-[[ distribution.architecture ]]
        """)
        self.assertEqual(build.spec["imager"]["kernel"], "linux-image-arm64")

    def test_a_name_that_was_never_set_is_reported(self):
        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.loads("""
                imager:
                    kernel: linux-image-[[ architecture ]]
            """)
        self.assertIn("architecture", str(caught.exception))

    # The file and the line, or the message points at jinja's idea of the
    # template rather than at anything on disk.
    def test_the_error_names_the_file(self):
        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.loads("imager:\n    kernel: [[ nowhere ]]\n")
        self.assertIn("<string>", str(caught.exception))

# What ansible templates on the target is none of seine's business, and
# seine's delimiters are its own so that it never sees them.
class AnsibleKeepsItsOwnTemplating(avocado.Test):
    def test_ansible_variables_are_left_alone(self):
        build = BuildCmd()
        build.loads("""
            playbook:
                - name: greet
                  tasks:
                      - name: say who we are
                        debug:
                            msg: "{{ ansible_facts.hostname }} {% if x %}on{% endif %}"
        """)
        self.assertEqual(
            build.spec["playbook"][0]["tasks"][0]["debug"]["msg"],
            "{{ ansible_facts.hostname }} {% if x %}on{% endif %}")

# The tree is walked once before it is loaded, so that where a name is set
# stops being something anyone has to keep in their head.
class NamesAreFoundWhereverTheyAreSet(avocado.Test):
    def write(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    # 'arch' is listed after 'kernel', so nothing had set the architecture
    # by the time the fragment reading it was loaded.
    def test_a_fragment_reads_what_a_later_fragment_sets(self):
        self.write("kernel.yaml",
                   "imager:\n"
                   "    kernel: linux-image-[[ distribution.architecture ]]\n")
        self.write("arch.yaml", "distribution:\n    architecture: arm64\n")
        main = self.write("main.yaml",
                          "requires:\n    - kernel\n    - arch\n")

        build = BuildCmd()
        spec = build.load(main)
        self.assertEqual(spec["imager"]["kernel"], "linux-image-arm64")

    # Including the file doing the asking, which renders before anything has
    # been merged at all.
    def test_the_first_file_reads_what_it_sets_itself(self):
        main = self.write("main.yaml",
                          "distribution:\n"
                          "    release: trixie\n"
                          "imager:\n"
                          "    kernel: linux-image-[[ distribution.release ]]\n")

        build = BuildCmd()
        spec = build.load(main)
        self.assertEqual(spec["imager"]["kernel"], "linux-image-trixie")

    # The walk is lenient so that it can finish; the load that follows it is
    # not, or a name nothing sets would go quietly empty.
    def test_a_name_nothing_sets_is_still_an_error(self):
        main = self.write("main.yaml",
                          "imager:\n    kernel: linux-image-[[ nowhere ]]\n")

        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.load(main)
        self.assertIn("nowhere", str(caught.exception))

    # Nothing the lenient walk made of a half-known file survives it: the
    # kernel below is empty while probing and has to be the real one after.
    def test_the_lenient_render_does_not_reach_the_specification(self):
        self.write("kernel.yaml",
                   "imager:\n"
                   "    kernel: linux-image-[[ distribution.architecture ]]\n")
        self.write("arch.yaml", "distribution:\n    architecture: amd64\n")
        main = self.write("main.yaml",
                          "requires:\n    - kernel\n    - arch\n")

        build = BuildCmd()
        spec = build.load(main)
        self.assertEqual(spec["imager"]["kernel"], "linux-image-amd64")
        # The walk itself did read that line before it knew the
        # architecture, and what it made of it stayed where it belongs.
        self.assertEqual(build._variables["imager"]["kernel"], "linux-image-")

# The walk has been to every file before anything is loaded, so what it
# knows is worth saying all at once.
class EveryNameThatIsNeverSetIsReported(avocado.Test):
    def write(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_two_bad_specifications_make_one_error(self):
        self.write("kernel.yaml", "imager:\n    kernel: [[ arch ]]\n")
        self.write("boot.yaml", "imager:\n    bootloader: [[ board ]]\n")
        main = self.write("main.yaml",
                          "requires:\n    - kernel\n    - boot\n")

        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.load(main)
        reported = str(caught.exception)
        self.assertIn("kernel.yaml", reported)
        self.assertIn("'arch'", reported)
        self.assertIn("boot.yaml", reported)
        self.assertIn("'board'", reported)

    # What a file asks of a name the specification does set is a question
    # about a value, which the load answers against the real ones.
    def test_a_setting_a_name_does_not_have_is_left_to_the_load(self):
        self.write("arch.yaml", "distribution:\n    architecture: amd64\n")
        main = self.write("main.yaml",
                          "requires:\n    - arch\n"
                          "imager:\n"
                          "    kernel: [[ distribution.flavour ]]\n")

        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.load(main)
        self.assertIn("flavour", str(caught.exception))

class OnlySubstitutionsAreAccepted(avocado.Test):
    # Branching in a specification is 'requires': which fragments are listed
    # says which apply, and a file can be read without being run.
    def test_blocks_are_refused(self):
        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.loads("""
                distribution:
                    [% if true %]
                    release: trixie
                    [% endif %]
            """)
        self.assertIn("requires", str(caught.exception))

    # A require naming a file through a variable makes the set of files a
    # specification is built from depend on the render.
    def test_a_templated_require_is_refused(self):
        build = BuildCmd()
        with self.assertRaises(ValueError) as caught:
            build.loads("""
                requires:
                    - ../common/[[ distribution.architecture ]]
            """)
        self.assertIn("'requires'", str(caught.exception))

    def test_a_plain_require_is_still_read(self):
        build = BuildCmd()
        with self.assertRaises(FileNotFoundError):
            build.loads("""
                requires:
                    - ../common/amd64
            """)

if __name__ == "__main__":
    avocado.main()
