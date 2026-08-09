#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

class MergeNewPartitionWithoutPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: data
                      where: /var
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 2 or parts[0]["label"] != "rootfs" or parts[1]["label"] != "data":
            self.fail("expected 2 partitions: 'rootfs' and 'data' (got %s)" % parts)

class MergeNewPartitionWithPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: data
                      priority: 800
                      where: /var
                    - label: boot
                      priority: 100
                      where: /boot
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 3 or parts[0]["label"] != "boot" or parts[1]["label"] != "rootfs" or parts[2]["label"] != "data":
            self.fail("expected 3 partitions: 'boot', 'rootfs' and 'data' (got %s)" % parts)

class MergePartitionWithAdditionalAttributes(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: rootfs
                      flags:
                          - boot
                          - primary
                      size: 256MiB
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "rootfs":
            self.fail("expected 1 partition: 'rootfs' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 2:
            self.fail("expected 2 partition flags: got %s" % part["flags"])
        if part["size"] != 256 * 1024 * 1024:
            self.fail("expected size of 256MiB: got %s" % part["size"])
        if part["where"] != "/":
            self.fail("expected 'where' to be '/': got %s" % part["where"])

class MergePartitionFlags(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
                      flags:
                          - boot
        """)
        build.loads("""
            image:
                partitions:
                    - label: rootfs
                      flags:
                          - boot
                          - primary
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "rootfs":
            self.fail("expected 1 partition: 'rootfs' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 2:
            self.fail("expected 2 partition flags: got %s" % part["flags"])

class MergePartitionFlagRemoved(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
                      flags:
                          - boot
                          - primary
        """)
        build.loads("""
            image:
                partitions:
                    - label: rootfs
                      flags:
                          - ~boot
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "rootfs":
            self.fail("expected 1 partition: 'rootfs' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 1:
            self.fail("expected 1 partition flag: got %s" % part["flags"])

class MergeClearPartitionFlagsButNoneSet(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: rootfs
                      where: /
        """)
        build.loads("""
            image:
                partitions:
                    - label: rootfs
                      flags:
                          - ~boot
                          - ~primary
        """)
        spec = build.parse()
        parts = spec["image"]["partitions"]
        if len(parts) != 1 or parts[0]["label"] != "rootfs":
            self.fail("expected 1 partition: 'rootfs' (got %s)" % parts)
        part = parts[0]
        if len(part["flags"]) != 0:
            self.fail("expected 0 partition flags: got %s" % part["flags"])

class MergeNewVolumeWithoutPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: main
                      group: main
                      size: 1GiB
                      flags:
                          - lvm
                volumes:
                    - label: rootfs
                      group: main
                      where: /
        """)
        build.loads("""
            image:
                volumes:
                    - label: data
                      group: main
                      where: /var
        """)
        spec = build.parse()
        vols = spec["image"]["volumes"]
        if len(vols) != 2 or vols[0]["label"] != "rootfs" or vols[1]["label"] != "data":
            self.fail("expected 2 volumes: 'rootfs' and 'data' (got %s)" % vols)

class MergeNewVolumesWithPriorities(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: main
                      group: main
                      size: 1GiB
                      flags:
                          - lvm
                volumes:
                    - label: rootfs
                      group: main
                      where: /
        """)
        build.loads("""
            image:
                volumes:
                    - label: data
                      group: main
                      priority: 800
                      where: /var
                    - label: boot
                      group: main
                      priority: 100
                      where: /boot
        """)
        spec = build.parse()
        vols = spec["image"]["volumes"]
        if len(vols) != 3 or vols[0]["label"] != "boot" or vols[1]["label"] != "rootfs" or vols[2]["label"] != "data":
            self.fail("expected 3 volumes: 'boot', 'rootfs' and 'data' (got %s)" % vols)

class MergeVolumeAttributes(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
            image:
                filename: simple-test.img
                partitions:
                    - label: main
                      group: main
                      size: 1GiB
                      flags:
                          - lvm
                volumes:
                    - label: rootfs
                      group: main
                      size: 750MiB
                      where: /
        """)
        build.loads("""
            image:
                volumes:
                    - label: rootfs
                      size: 500MiB
        """)
        spec = build.parse()
        vols = spec["image"]["volumes"]
        if len(vols) != 1 or vols[0]["label"] != "rootfs":
            self.fail("expected 1 volume: 'rootfs' (got %s)" % vols)
        vol = vols[0]
        if vol["size"] != 750 * 1024 * 1024:
            self.fail("expected size of 750MiB: got %s" % vol["size"])

if __name__ == "__main__":
    avocado.main()

class FeedsAreAddedRatherThanReplaced(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                          uri: http://snapshot.debian.org/archive/debian/20260101
                        - suite: bookworm-security
                          uri: http://snapshot.debian.org/archive/debian-security/20260101
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        # A fragment adding one feed keeps the others, URIs and all: a
        # specification built from a snapshot has changed exactly those,
        # and restating them here is what it must not have to do.
        build.loads("""
                distribution:
                    feeds:
                        - suite: bookworm-backports
        """)
        feeds = build.spec["distribution"]["feeds"]
        self.assertEqual([f["suite"] for f in feeds],
                         ["bookworm", "bookworm-security", "bookworm-backports"])
        self.assertEqual(feeds[0]["uri"],
                         "http://snapshot.debian.org/archive/debian/20260101")

class FeedsAreOverriddenBySuite(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        # Naming a suite that is already there settles it rather than
        # adding a second entry for it.
        build.loads("""
                distribution:
                    feeds:
                        - suite: bookworm
                          valid-until: false
        """)
        feeds = build.spec["distribution"]["feeds"]
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]["valid-until"], False)
