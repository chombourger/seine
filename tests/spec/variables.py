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
