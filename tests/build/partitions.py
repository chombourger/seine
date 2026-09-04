#!/usr/bin/env python3

import avocado
import os
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.partition import PartitionHandler

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

class RoFsRequiresGptTable(avocado.Test):
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
                        - label: usr
                          where: /usr
                          type: squashfs
            """)
            build.parse()
            self.fail("parsing should have failed (read-only type needs a gpt table)!")
        except ValueError as e:
            if "needs a 'gpt' partition table" not in str(e):
                self.fail("parsing did not return the error we expected!")
        except avocado.core.exceptions.TestFail:
            raise
        except Exception as e:
            self.fail("parsing caused an unknown error: %s" % str(type(e)))

class RoFsOnGptTableIsAccepted(avocado.Test):
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
                        - label: usr
                          where: /usr
                          type: squashfs
            """)
            build.parse()
        except:
            self.fail("parsing of a read-only partition on a 'gpt' table failed!")

class RoFsOnLvmVolumeIsAccepted(avocado.Test):
    def test(self):
        try:
            build = BuildCmd()
            build.loads("""
                image:
                    filename: simple-test.img
                    partitions:
                        - label: pv
                          flags:
                              - lvm
                          group: main
                          size: 512MiB
                    volumes:
                        - label: usr
                          group: main
                          where: /usr
                          type: squashfs
                          size: 128MiB
            """)
            build.parse()
        except:
            self.fail("parsing of a read-only LVM volume failed!")

# 'source:' routes a partition/volume's content to a declared
# 'multiconfig:' group's own rootfs instead of this specification's own --
# see PartitionHandler._validate_sources().
class SourceMustNameADeclaredGroup(avocado.Test):
    def test(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "disk.img",
                    "partitions": [
                        {"label": "rootfs", "source": "main", "where": "/"},
                    ],
                },
            })
            self.fail("an undeclared 'source:' group was accepted!")
        except ValueError as e:
            self.assertIn("main", str(e))
            self.assertIn("not one of the declared", str(e))

# Groups are side-by-side, non-overlapping OSes -- each declared group
# needs exactly one root, so zero or more than one is a parse-time error,
# not an ambiguous disk.
class EachGroupNeedsExactlyOneRoot(avocado.Test):
    # A declared group nothing references at all (built alongside the
    # disk, not yet routed into it) is left alone -- only a group with
    # a sourced mount and no root among
    # them is the error.
    def test_a_group_nothing_references_is_left_alone(self):
        try:
            PartitionHandler().parse({
                "multiconfig": {"main": ["main.yaml"]},
                "image": {
                    "filename": "disk.img",
                    "partitions": [{"label": "rootfs", "where": "/"}],
                },
            })
        except:
            self.fail("an unreferenced 'multiconfig:' group was rejected!")

    def test_zero_roots_among_a_referenced_groups_mounts_is_an_error(self):
        try:
            PartitionHandler().parse({
                "multiconfig": {"main": ["main.yaml"]},
                "image": {
                    "filename": "disk.img",
                    "partitions": [
                        {"label": "rootfs", "where": "/"},
                        {"label": "main-usr", "source": "main", "where": "/usr"},
                    ],
                },
            })
            self.fail("a referenced group with no root partition was accepted!")
        except ValueError as e:
            self.assertIn("main", str(e))

    def test_two_is_an_error(self):
        try:
            PartitionHandler().parse({
                "multiconfig": {"main": ["main.yaml"]},
                "image": {
                    "filename": "disk.img",
                    "partitions": [
                        {"label": "one", "source": "main", "where": "/"},
                        {"label": "two", "source": "main", "where": "/"},
                    ],
                },
            })
            self.fail("two root partitions for one group were accepted!")
        except ValueError as e:
            self.assertIn("main", str(e))

class SourcedPartitionsAreAccepted(avocado.Test):
    def test(self):
        ph = PartitionHandler()
        ph.parse({
            "multiconfig": {"main": ["main.yaml"], "recovery": ["recovery.yaml"]},
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "main-root", "source": "main", "where": "/"},
                    {"label": "recovery-root", "source": "recovery", "where": "/"},
                ],
            },
        })
        self.assertEqual(
            {p["label"]: p["source"] for p in ph.partitions},
            {"main-root": "main", "recovery-root": "recovery"})

# A specification with no 'multiconfig:' key declares nothing for
# 'source:' to be one of, so this stays a pure no-op for it -- confirmed
# rather than assumed, since it is what keeps a plain spec byte-identical
# to before 'source:' existed.
class NoMulticonfigKeyIsUnaffected(avocado.Test):
    def test_a_plain_specification_still_parses(self):
        try:
            PartitionHandler().parse({
                "image": {
                    "filename": "simple.img",
                    "partitions": [{"label": "rootfs", "where": "/"}],
                },
            })
        except:
            self.fail("a specification with no 'multiconfig:' key was rejected!")

    def test_source_is_never_added_by_default(self):
        ph = PartitionHandler()
        ph.parse({
            "image": {
                "filename": "simple.img",
                "partitions": [{"label": "rootfs", "where": "/"}],
            },
        })
        self.assertNotIn("source", ph.partitions[0])

# 'distribute()' only ever grows the mount whose own 'source' matches the
# one it was called for -- a tar member from one group's tarball must
# never fatten another group's partition, even when both mounts are named
# 'where: "/"' the same way.
class DistributeScopesBySource(avocado.Test):
    def test_a_mount_only_matches_its_own_source(self):
        ph = PartitionHandler()
        ph.parse({
            "multiconfig": {"main": ["main.yaml"], "recovery": ["recovery.yaml"]},
            "image": {
                "filename": "disk.img",
                "partitions": [
                    {"label": "main-root", "source": "main", "where": "/"},
                    {"label": "recovery-root", "source": "recovery", "where": "/"},
                ],
            },
        })
        main_root = next(p for p in ph.mounts if p["label"] == "main-root")
        recovery_root = next(p for p in ph.mounts if p["label"] == "recovery-root")
        recovery_before = recovery_root["_size"]

        member = tarfile.TarInfo("etc/hostname")
        member.size = 4096
        matched = ph.distribute(member, source="main")

        self.assertIs(matched, main_root)
        self.assertEqual(recovery_root["_size"], recovery_before,
                         "recovery's own partition grew from main's content")

if __name__ == "__main__":
    avocado.main()
