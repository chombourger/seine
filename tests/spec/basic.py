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

if __name__ == "__main__":
    avocado.main()
