#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class MinimalSpec(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
        except:
            self.fail("failed to parse a valid minimal spec!")

class MissingImageFilename(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
            self.fail("parsing succeeded when it should have failed (missing 'filename' in 'image')!")
        except ValueError as e:
            if str(e) != "output 'filename' not specified in 'image' section!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class PlaybookNotAList(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                playbook: impossible
                image:
                    filename: simple-test.img
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
            self.fail("parsing succeeded when it should have failed ('playbook' is not a list)!")
        except ValueError as e:
            if str(e) != "'playbook' shall be a list of Ansible playbooks!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class BaselinePlaybook(avocado.Test):
    def test(self):
        build = BuildCmd()
        baseline = "sample-baseline"
        build.loads("""
            playbook:
                - baseline: %s
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """ % baseline)
        spec = build.parse()
        if spec["baseline"] != baseline:
            self.fail("selected baseline is '%s' but expected '%s'!" % (spec["baseline"], baseline))

class MultipleBaselinesWithPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        baseline = "sample-baseline"
        build.loads("""
            playbook:
                - baseline: least-prio-baseline
                  priority: 900
                - baseline: %s
                  priority: 100
                - baseline: default-prio-baseline
                - baseline: medium-prio-baseline
                  priority: 600
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """ % baseline)
        spec = build.parse()
        if spec["baseline"] != baseline:
            self.fail("selected baseline is '%s' but expected '%s'!" % (spec["baseline"], baseline))

if __name__ == "__main__":
    avocado.main()
