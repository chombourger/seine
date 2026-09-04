#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class MinimalInitrdSpec(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                packages: []
                initrd:
                    filename: minimal.img
            """)
            build.parse()
        except:
            self.fail("failed to parse a valid minimal 'initrd:' spec!")

class MissingInitrdFilename(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                packages: []
                initrd: {}
            """)
            build.parse()
            self.fail("parsing succeeded when it should have failed (missing 'filename' in 'initrd')!")
        except ValueError as e:
            if str(e) != "output 'filename' not specified in 'initrd' section!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class ImageAndInitrdAreMutuallyExclusive(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    partitions:
                        - label: rootfs
                          where: /
                initrd:
                    filename: minimal.img
            """)
            build.parse()
            self.fail("parsing succeeded when it should have failed ('image' and 'initrd' together)!")
        except ValueError as e:
            if str(e) != "'image' and 'initrd' sections are mutually exclusive!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))
