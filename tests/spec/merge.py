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

class PackagesAreMergedByTheirSourcePackage(avocado.Test):
    def test(self):
        build = BuildCmd()
        # What a suite says about a kernel: which packaging to take, and
        # nothing else.
        build.loads("""
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
        """)
        # What the kernel itself says, which no suite has an opinion on.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
                              flavour: amd64
                      profiles:
                          - noudeb
        """)
        packages = build.spec["packages"]
        self.assertEqual(len(packages), 1)
        # The version the specification asked for survives being described
        # further, or a fragment could not be shared by two suites.
        self.assertEqual(packages[0]["source"], "apt://linux=6.12.95-1~bpo12+1")
        self.assertEqual(packages[0]["profiles"], ["noudeb"])
        self.assertEqual(packages[0]["extends"]["kernel"]["flavour"], "amd64")

class PackagesAreMergedInsideExtends(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: rpi
        """)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
                              featureset: rt
        """)
        kernel = build.spec["packages"][0]["extends"]["kernel"]
        # Settled one setting at a time rather than the 'kernel' entry
        # being replaced whole: what was said first stands, what is new
        # is added.
        self.assertEqual(kernel, {"flavour": "rpi", "featureset": "rt"})

class DifferentPackagesAreLeftApart(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://busybox
        """)
        build.loads("""
                packages:
                    - source: git://example.com/linux.git;rev=deadbeef
                    - source: https://example.com/busybox_1.37.0-6.dsc
        """)
        packages = build.spec["packages"]
        # busybox is busybox wherever its source is fetched from; the
        # kernel is not busybox.
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0]["source"], "apt://busybox")
        self.assertEqual(packages[1]["source"],
                         "git://example.com/linux.git;rev=deadbeef")

class FilesAreResolvedAgainstTheFileThatListedThem(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A package described by two files in two directories: each names
        # its own files, and merging must not leave one of them looking
        # for the other's.
        for dirname in ["boards/rpi", "kernels/6.18"]:
            os.makedirs(os.path.join(self.workdir, dirname), exist_ok=True)
        with open(os.path.join(self.workdir, "boards/rpi/board.yml"), "w") as f:
            f.write("packages:\n"
                    "    - source: apt://linux\n"
                    "      patches:\n"
                    "          - patches/0001-board.patch\n")
        with open(os.path.join(self.workdir, "kernels/6.18/kernel.yml"), "w") as f:
            f.write("packages:\n"
                    "    - source: apt://linux\n"
                    "      extends:\n"
                    "          kernel:\n"
                    "              config:\n"
                    "                  - configs/slim.fragment\n")
        build.load(os.path.join(self.workdir, "boards/rpi/board.yml"))
        build.load(os.path.join(self.workdir, "kernels/6.18/kernel.yml"))

        package = build.spec["packages"][0]
        self.assertEqual(package["patches"],
                         [os.path.join(self.workdir,
                                       "boards/rpi/patches/0001-board.patch")])
        self.assertEqual(package["extends"]["kernel"]["config"],
                         [os.path.join(self.workdir,
                                       "kernels/6.18/configs/slim.fragment")])

class DefaultsDescribeAPackageWithoutBuildingIt(avocado.Test):
    def test(self):
        build = BuildCmd()
        # An architecture file, saying which kernel of it is meant.
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        # Nothing asked for a kernel, so nothing is built.
        self.assertEqual(build.spec.get("packages"), None)

class DefaultsAreFoldedIntoWhatIsBuilt(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.loads("""
                packages:
                    - source: apt://linux=6.12.101-1
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
        """)
        build.parse()
        package = build.image.packages[0]
        self.assertEqual(package.kernel_flavour, "amd64")
        self.assertEqual(package.version, "6.12.101-1")

class ThePackageBeingBuiltBeatsTheDefault(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: cloud-amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        self.assertEqual(build.image.packages[0].kernel_flavour, "cloud-amd64")

class TheLastDefaultWins(avocado.Test):
    def test(self):
        build = BuildCmd()
        # The architecture file, then the board file that sits on it.
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavour: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  featureset: rt
        """)
        build.loads("""
                packages:
                    - source: apt://linux
        """)
        build.parse()
        package = build.image.packages[0]
        # The particular adds to the general rather than replacing it.
        self.assertEqual(package.kernel_flavour, "amd64")
        self.assertEqual(package.kernel_featureset, "rt")

class DefaultsAreCheckedWhereTheyAreWritten(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A default nothing builds still has to make sense, or a typo in an
        # architecture file waits for the one image that rebuilds a kernel.
        build.loads("""
                defaults:
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  flavours: amd64
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.parse()
            self.fail("parsing succeeded for an unknown 'kernel' setting!")
        except ValueError:
            pass

class DefaultsHoldPackagesOnly(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.loads("""
                defaults:
                    playbook:
                        - name: nothing
            """)
            self.fail("parsing succeeded for a 'defaults' section that is not packages!")
        except ValueError:
            pass

class SettingsRememberWhichFileWroteThem(avocado.Test):
    def test(self):
        # An overlay of the shape the examples use: the suite file pins
        # the packaging, the kernel fragment says which tree to graft on,
        # the architecture file says which flavour under 'defaults'. The
        # file to open to change any one of them is a different file.
        build = BuildCmd()
        for name, text in [
                ("suite.yml", """
                    packages:
                        - source: apt://linux=6.12.101-1
                """),
                ("kernel.yml", """
                    packages:
                        - source: apt://linux
                          extends:
                              kernel:
                                  upstream: https://kernel.org/linux-6.18.43.tar.xz
                """),
                ("arch.yml", """
                    defaults:
                        packages:
                            - source: apt://linux
                              extends:
                                  kernel:
                                      flavour: amd64
                """)]:
            path = os.path.join(self.workdir, name)
            with open(path, "w") as f:
                f.write(text)
            build.load(path)
        build.loads("""
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()

        package = build.image.packages[0]
        self.assertEqual(os.path.basename(package.origin_of("source")),
                         "suite.yml")
        self.assertEqual(
            os.path.basename(package.origin_of("extends.kernel.upstream")),
            "kernel.yml")
        self.assertEqual(
            os.path.basename(package.origin_of("extends.kernel.flavour")),
            "arch.yml")
        # Nothing wrote this one down.
        self.assertEqual(package.origin_of("extends.kernel.sha256"), None)
