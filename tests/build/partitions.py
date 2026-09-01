#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class GptPartitionTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: gpt
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
        except:
            self.fail("parsing of a specification with a 'gpt' partition table failed!")

class MsDosPartitionTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: msdos
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
        except:
            self.fail("parsing of a specification with a 'msdos' partition table failed!")

class UnsupportedPartitionTable(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    table: unsupported-partition-table
                    partitions:
                        - label: rootfs
                          where: /
            """)
            build.parse()
            self.fail("parsing should have failed (invalid partition table)!")
        except ValueError as e:
            if str(e) != "'unsupported-partition-table' is not a supported partition table!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class PartitionMissingLabel(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    partitions:
                          where: /
            """)
            build.parse()
            self.fail("parsing should have failed (missing partition label)!")
        except ValueError as e:
            if str(e) != "one of the partitions does not have a 'label' defined!":
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

if __name__ == "__main__":
    avocado.main()
