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
        # Two peer files, so the second amends the first's size.
        if vol["size"] != 500 * 1024 * 1024:
            self.fail("expected size of 500MiB: got %s" % vol["size"])

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
        # The suite pins the version and requires the kernel fragment,
        # which says which tree to graft on and has no opinion on the
        # version -- a real 'requires:' chain, not two peer files.
        with open(os.path.join(self.workdir, "kernel.yml"), "w") as f:
            f.write("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
                              flavour: amd64
                      profiles:
                          - noudeb
            """)
        suite = os.path.join(self.workdir, "suite.yml")
        with open(suite, "w") as f:
            f.write("""
                requires:
                    - kernel
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
            """)
        build.load(suite)
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
        # A board file requiring a generic kernel fragment -- a real
        # 'requires:' chain, not two peer files.
        with open(os.path.join(self.workdir, "kernel.yml"), "w") as f:
            f.write("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
                              featureset: rt
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - kernel
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: rpi
            """)
        build.load(board)
        kernel = build.spec["packages"][0]["extends"]["kernel"]
        # Settled one setting at a time rather than the 'kernel' entry
        # being replaced whole: what was said first stands, what is new
        # is added.
        self.assertEqual(kernel, {"flavour": "rpi", "featureset": "rt"})

class APeerFileAmendsAPackageByField(avocado.Test):
    # Two top-level files, as 'seine build a.yaml b.yaml' or a TUI
    # side-load would compose them -- the second amends the first field
    # by field rather than losing to it or replacing the entry whole.
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
                      profiles:
                          - noudeb
        """)
        build.loads("""
                packages:
                    - source: apt://linux=6.12.101-1
        """)
        package = build.spec["packages"][0]
        self.assertEqual(package["source"], "apt://linux=6.12.101-1")
        self.assertEqual(package["profiles"], ["noudeb"])

class ARequiresFragmentDoesNotOverrideAPackageField(avocado.Test):
    # The 'requires:' counterpart of APeerFileAmendsAPackageByField --
    # a board still keeps its own pin over a fragment it requires.
    def test(self):
        build = BuildCmd()
        with open(os.path.join(self.workdir, "fragment.yml"), "w") as f:
            f.write("""
                packages:
                    - source: apt://linux=6.12.101-1
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - fragment
                packages:
                    - source: apt://linux=6.12.95-1~bpo12+1
                      profiles:
                          - noudeb
            """)
        build.load(board)
        package = build.spec["packages"][0]
        self.assertEqual(package["source"], "apt://linux=6.12.95-1~bpo12+1")
        self.assertEqual(package["profiles"], ["noudeb"])

class PackagesAreMergedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        # What asks for the build, naming the tree and what it is called.
        build.loads("""
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
                      version: 580.95.05
        """)
        # What adds to it, which knows the package by name and has no
        # opinion about the revision it is pinned to.
        build.loads("""
                packages:
                    - name: nvidia-open
                      profiles:
                          - nocheck
        """)
        packages = build.spec["packages"]
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["version"], "580.95.05")
        self.assertEqual(packages[0]["profiles"], ["nocheck"])

class PackagesWithDifferentNamesAreNotMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        # One tree, two packages built from it: the name decides, so the
        # same 'source' twice is not one package when they are named
        # apart.
        build.loads("""
                packages:
                    - source: git://example.com/drivers.git;rev=deadbeef
                      name: driver-one
                    - source: git://example.com/drivers.git;rev=deadbeef
                      name: driver-two
        """)
        self.assertEqual([p["name"] for p in build.spec["packages"]],
                         ["driver-one", "driver-two"])

class ANamedPackageStillMergesBySource(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A file that names no package still merges into one that does,
        # since the name it would have had is the one the URI gives.
        build.loads("""
                packages:
                    - source: apt://busybox
                      name: busybox
        """)
        build.loads("""
                packages:
                    - source: apt://busybox
                      profiles:
                          - nocheck
        """)
        self.assertEqual(len(build.spec["packages"]), 1)
        self.assertEqual(build.spec["packages"][0]["profiles"], ["nocheck"])

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
        # kernel is not busybox. Two peer files, so the second's source
        # amends the first's.
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0]["source"],
                         "https://example.com/busybox_1.37.0-6.dsc")
        self.assertEqual(packages[1]["source"],
                         "git://example.com/linux.git;rev=deadbeef")

class VendorEntriesAreMergedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                vendor:
                    - name: openssl
        """)
        build.loads("""
                vendor:
                    - name: openssl
                      arch: [armhf]
                    - name: zlib
        """)
        entries = build.spec["vendor"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "openssl")
        self.assertEqual(entries[0]["arch"], ["armhf"])
        self.assertEqual(entries[1]["name"], "zlib")

class VendorEntrySettingsAreFirstLoadedWins(avocado.Test):
    def test(self):
        build = BuildCmd()
        # A fragment naming the same entry again cannot override what the
        # file asking for it already pinned -- a real 'requires:' chain,
        # not two peer files.
        with open(os.path.join(self.workdir, "fragment.yml"), "w") as f:
            f.write("""
                vendor:
                    - name: openssl
                      version: ">=1.3"
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - fragment
                vendor:
                    - name: openssl
                      version: ">=1.2"
            """)
        build.load(board)
        self.assertEqual(build.spec["vendor"][0]["version"], ">=1.2")

class VendorExcludeIsAdditive(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("vendor-exclude: [gcc-12]")
        build.loads("vendor-exclude: [gcc-12, texlive]")
        self.assertEqual(build.spec["vendor-exclude"], ["gcc-12", "texlive"])

# Unlike every other 'distribution:' setting (last-loaded wins),
# 'architectures:' is additive and deduplicated, the same as
# 'vendor-exclude:' above -- so a specification composing
# examples/common/amd64.yaml and .../arm64.yaml (each naming its own
# one) ends up with both, not whichever was required last.
# 'architecture' itself (singular) still overwrites.
class DistributionArchitecturesIsAdditive(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    architecture: amd64
                    architectures:
                        - amd64
        """)
        build.loads("""
                distribution:
                    architecture: arm64
                    architectures:
                        - arm64
        """)
        self.assertEqual(build.spec["distribution"]["architecture"], "arm64")
        self.assertEqual(build.spec["distribution"]["architectures"],
                         ["amd64", "arm64"])

        # Naming the same architecture again does not duplicate it.
        build.loads("""
                distribution:
                    architectures:
                        - arm64
        """)
        self.assertEqual(build.spec["distribution"]["architectures"],
                         ["amd64", "arm64"])

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
                    "              fragments:\n"
                    "                  - configs/slim.fragment\n")
        build.load(os.path.join(self.workdir, "boards/rpi/board.yml"))
        build.load(os.path.join(self.workdir, "kernels/6.18/kernel.yml"))

        package = build.spec["packages"][0]
        self.assertEqual(package["patches"],
                         [os.path.join(self.workdir,
                                       "boards/rpi/patches/0001-board.patch")])
        self.assertEqual(package["extends"]["kernel"]["fragments"],
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

IMAGE = """
                image:
                    filename: t.img
                    partitions:
                        - label: rootfs
                          where: /
"""

class ADefaultNeedsNoSourceOfItsOwn(avocado.Test):
    def test(self):
        build = BuildCmd()
        # An architecture file, adding to a package it knows by name. It
        # has no opinion about where the source comes from -- that is
        # said by whoever asks for the build.
        build.loads("""
                defaults:
                    packages:
                        - name: nvidia-open
                          profiles:
                              - nocheck
        """ + IMAGE)
        build.loads("""
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
        """)
        spec = build.parse()
        self.assertEqual(len(spec["packages"]), 1)
        self.assertEqual(spec["packages"][0]["profiles"], ["nocheck"])

class ASourcelessDefaultNobodyBuildsIsDropped(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - name: nvidia-open
                          profiles:
                              - nocheck
        """ + IMAGE)
        build.parse()
        # It described a package nothing asked for, so it described
        # nothing -- and it did not conjure a build with no source.
        self.assertEqual(build.spec.get("packages"), None)

class ADescriptionIsStillChecked(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - name: nvidia-open
                          extends:
                              kernel:
                                  flavur: amd64
        """ + IMAGE)
        try:
            build.parse()
            self.fail("a misspelt setting under 'defaults' was accepted!")
        except ValueError:
            pass

class APackageStillNeedsASource(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - name: nvidia-open
                      profiles:
                          - nocheck
        """ + IMAGE)
        try:
            build.parse()
            self.fail("an entry under 'packages' was built with no source!")
        except ValueError:
            pass

class AnEntryWithNeitherSourceNorNameIsRefused(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                defaults:
                    packages:
                        - profiles:
                              - nocheck
        """ + IMAGE)
        try:
            build.parse()
            self.fail("an entry saying which package it is about was accepted!")
        except ValueError:
            pass

MODULE = """
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
                      version: "580.95.05"
                      extends:
                          module:
                              amd64-kernels:
                                  - apt://linux-headers-amd64
"""

class KernelsAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        # What asks for the modules, naming the distribution's kernel.
        build.loads(MODULE)
        # An architecture file, adding the kernel this build makes.
        build.loads("""
                packages:
                    - name: nvidia-open
                      extends:
                          module:
                              amd64-kernels:
                                  - linux
        """)
        kernels = build.spec["packages"][0]["extends"]["module"]["amd64-kernels"]
        # Both, in the order they were written: neither file is
        # describing the same thing twice, so settling between them
        # would drop a kernel somebody asked to have modules for.
        self.assertEqual(kernels, ["apt://linux-headers-amd64", "linux"])

class TheSameKernelIsNotAddedTwice(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(MODULE)
        build.loads("""
                packages:
                    - name: nvidia-open
                      extends:
                          module:
                              amd64-kernels:
                                  - apt://linux-headers-amd64
                                  - linux
        """)
        kernels = build.spec["packages"][0]["extends"]["module"]["amd64-kernels"]
        self.assertEqual(kernels, ["apt://linux-headers-amd64", "linux"])

class KernelsOfDifferentArchitecturesStayApart(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(MODULE)
        build.loads("""
                packages:
                    - name: nvidia-open
                      extends:
                          module:
                              arm64-kernels:
                                  - apt://linux-headers-arm64
        """)
        module = build.spec["packages"][0]["extends"]["module"]
        self.assertEqual(module["amd64-kernels"], ["apt://linux-headers-amd64"])
        self.assertEqual(module["arm64-kernels"], ["apt://linux-headers-arm64"])

# An architecture file deriving a generic flavour of its own, the way
# 'slim-amd64' is meant to be built on by a board file.
DERIVED = """
                packages:
                    - source: apt://linux
                      revision: fixed1
                      extends:
                          kernel:
                              derived-flavours:
                                  amd64:
                                      slim-amd64: []
"""

class DerivedFlavoursAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(DERIVED)
        # A board file, deriving its own flavour from the one above --
        # not an original Debian flavour, so it only exists once both
        # files' 'derived-flavours' are on the same package.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              derived-flavours:
                                  slim-amd64:
                                      pc: []
        """)
        derived = build.spec["packages"][0]["extends"]["kernel"]["derived-flavours"]
        self.assertEqual(derived, {"amd64": {"slim-amd64": []},
                                   "slim-amd64": {"pc": []}})

class DerivedFlavoursOfTheSameBaseAreMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(DERIVED)
        # Another board deriving from the same base as the first file --
        # both flavours are wanted, so the second file's base is added to
        # rather than replacing what the first already said about it.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              derived-flavours:
                                  amd64:
                                      cloud-pc: []
        """)
        derived = build.spec["packages"][0]["extends"]["kernel"]["derived-flavours"]
        self.assertEqual(derived, {"amd64": {"slim-amd64": [], "cloud-pc": []}})

# Two files rebuilding the same kernel (matched by 'source', neither
# names it) each wanting a config group of their own -- the gap a real
# session hit (build/chats/20260820T080504625424.json): the second
# file's whole 'configs:' was silently dropped rather than merged.
CONFIGS = """
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  debug-page-ref:
                                      - CONFIG_DEBUG_PAGE_REF=y
"""

class KernelConfigGroupsAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(CONFIGS)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  magic-sysrq:
                                      - CONFIG_MAGIC_SYSRQ=n
        """)
        configs = build.spec["packages"][0]["extends"]["kernel"]["configs"]
        self.assertEqual(configs,
                         {"debug-page-ref": ["CONFIG_DEBUG_PAGE_REF=y"],
                          "magic-sysrq": ["CONFIG_MAGIC_SYSRQ=n"]})

class KernelConfigsOfTheSameGroupAreMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(CONFIGS)
        # Another file adding to the same group -- both lines are
        # wanted, so the second file's group adds to rather than
        # replacing what the first already said about it.
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  debug-page-ref:
                                      - CONFIG_DEBUG_PAGE_REF_TRACKING=y
        """)
        configs = build.spec["packages"][0]["extends"]["kernel"]["configs"]
        self.assertEqual(configs,
                         {"debug-page-ref": ["CONFIG_DEBUG_PAGE_REF=y",
                                             "CONFIG_DEBUG_PAGE_REF_TRACKING=y"]})

class KernelConfigLinesAreNotAddedTwice(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(CONFIGS)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  debug-page-ref:
                                      - CONFIG_DEBUG_PAGE_REF=y
        """)
        configs = build.spec["packages"][0]["extends"]["kernel"]["configs"]
        self.assertEqual(configs,
                         {"debug-page-ref": ["CONFIG_DEBUG_PAGE_REF=y"]})

class DefaultsAddTheirKernelsToo(avocado.Test):
    def test(self):
        build = BuildCmd()
        # An architecture file: modules for the kernel we build, if one
        # is built. Under 'defaults', so it asks for nothing itself.
        build.loads("""
                distribution:
                    release: trixie
                    architecture: amd64
                defaults:
                    packages:
                        - name: nvidia-open
                          extends:
                              module:
                                  amd64-kernels:
                                      - linux
        """ + IMAGE)
        build.loads(MODULE)
        build.loads("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        spec = build.parse()
        module = [p for p in spec["packages"]
                  if p.get("name") == "nvidia-open"][0]["extends"]["module"]
        self.assertEqual(module["amd64-kernels"],
                         ["apt://linux-headers-amd64", "linux"])

class ADefaultDropsAKernelNothingBuilds(avocado.Test):
    def test(self):
        build = BuildCmd()
        # The same architecture file, in a specification that builds no
        # kernel of its own -- which is most of them.
        build.loads("""
                distribution:
                    release: trixie
                    architecture: amd64
                defaults:
                    packages:
                        - name: nvidia-open
                          extends:
                              module:
                                  amd64-kernels:
                                      - linux
        """ + IMAGE)
        build.loads(MODULE)
        spec = build.parse()
        module = spec["packages"][0]["extends"]["module"]
        # The description described nothing, so it added nothing. The
        # modules are still built, against the distribution's kernel.
        self.assertEqual(module["amd64-kernels"], ["apt://linux-headers-amd64"])

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
                    requires:
                        - kernel
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
        build.load(os.path.join(self.workdir, "suite.yml"))
        build.load(os.path.join(self.workdir, "arch.yml"))
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

class TheSamePlaybookEntryReachedTwiceIsNotDuplicated(avocado.Test):
    # A fragment reached via two 'requires:' paths used to duplicate
    # its own playbook entry once per path.
    def test(self):
        build = BuildCmd()
        build.loads("""
                playbook:
                    - name: configure user accounts
                      priority: 900
                      tasks:
                          - name: set root password
                            user: name=root password=secret
        """)
        build.loads("""
                playbook:
                    - name: configure user accounts
                      priority: 900
                      tasks:
                          - name: set root password
                            user: name=root password=secret
        """)
        self.assertEqual(len(build.spec["playbook"]), 1)
        self.assertEqual(len(build.spec["playbook"][0]["tasks"]), 1)

class PlaybookEntryTasksAreAddedToRatherThanSettled(avocado.Test):
    # 'tasks:' stays one additive, order-preserving list -- not merged
    # task-by-task -- since ansible tasks run in sequence and folding
    # two same-named tasks into one would be a bigger behavior change
    # than it is for an independent test case.
    def test(self):
        build = BuildCmd()
        build.loads("""
                playbook:
                    - name: configure user accounts
                      tasks:
                          - name: set root password
                            user: name=root password=one
        """)
        build.loads("""
                playbook:
                    - name: configure user accounts
                      tasks:
                          - name: add a second user
                            user: name=alice
        """)
        tasks = build.spec["playbook"][0]["tasks"]
        self.assertEqual([t["name"] for t in tasks],
                         ["set root password", "add a second user"])

class TheSameTestEntryReachedTwiceIsNotDuplicated(avocado.Test):
    # The bug this whole block guards against: a shared fragment's own
    # 'test:' entry, reached via two 'requires:' paths (so loaded
    # twice), used to show up as two -- or, with a third path, three --
    # duplicate entries instead of one.
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: shared login
                      keywords:
                          - name: Log In
                            steps: ["Log Message  hi"]
        """)
        build.loads("""
                test:
                    - name: shared login
                      keywords:
                          - name: Log In
                            steps: ["Log Message  hi"]
        """)
        self.assertEqual(len(build.spec["test"]), 1)

class TestEntriesAreAmendedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      keywords:
                          - name: Log In
                            steps: ["Log Message  hi"]
        """)
        build.loads("""
                test:
                    - name: boot
                      tags: [smoke]
                      setup:
                          connect_target: {}
        """)
        entry = build.spec["test"][0]
        self.assertEqual(entry["tags"], ["smoke"])
        self.assertEqual(entry["setup"], {"connect_target": {}})

class ATestEntrysExistingFieldWins(avocado.Test):
    # A board's own 'boot' test entry stands even if a fragment it
    # requires also names an entry called 'boot' -- a real 'requires:'
    # chain, not two peer files.
    def test(self):
        build = BuildCmd()
        with open(os.path.join(self.workdir, "fragment.yml"), "w") as f:
            f.write("""
                test:
                    - name: boot
                      setup:
                          connect_target: {label: secondary}
            """)
        board = os.path.join(self.workdir, "board.yml")
        with open(board, "w") as f:
            f.write("""
                requires:
                    - fragment
                test:
                    - name: boot
                      setup:
                          connect_target: {label: primary}
            """)
        build.load(board)
        self.assertEqual(build.spec["test"][0]["setup"],
                         {"connect_target": {"label": "primary"}})

class ConflictingKeywordsOfTheSameNameAreRefused(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      keywords:
                          - name: Log In
                            steps: ["Log Message  one"]
        """)
        self.assertRaises(ValueError, build.loads, """
                test:
                    - name: boot
                      keywords:
                          - name: Log In
                            steps: ["Log Message  two"]
        """)

class TestCasesAreAmendedByName(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            steps: ["Log Message  hi"]
        """)
        build.loads("""
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            tags: [smoke]
        """)
        cases = build.spec["test"][0]["tests"]
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["tags"], ["smoke"])
        self.assertEqual(cases[0]["steps"], ["Log Message  hi"])

class ConflictingTestCaseStepsAreRefused(avocado.Test):
    # Unlike an entry's own scalar settings (first-loaded wins), a case
    # name colliding with genuinely different 'steps:' is more likely an
    # authoring accident than deliberate composition, so it is refused
    # rather than silently keeping the first-loaded steps.
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            steps: ["Log Message  one"]
        """)
        self.assertRaises(ValueError, build.loads, """
                test:
                    - name: boot
                      tests:
                          - name: boots up
                            steps: ["Log Message  two"]
        """)

class TestEntryLibraryAndTagsAreAddedToRatherThanSettled(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tags: [smoke]
                      library: [my.pkg.LibraryOne]
        """)
        build.loads("""
                test:
                    - name: boot
                      tags: [regression]
                      library: [my.pkg.LibraryTwo]
        """)
        entry = build.spec["test"][0]
        self.assertEqual(entry["tags"], ["smoke", "regression"])
        self.assertEqual(entry["library"],
                         ["my.pkg.LibraryOne", "my.pkg.LibraryTwo"])

class TestEntryVariablesAreAddedToRatherThanSettled(avocado.Test):
    # A regression test for a real pitfall found while designing this:
    # 'variables' is a flat name -> scalar dict, not nested like
    # 'derived-flavours', and must not be routed through _added()'s
    # dict-merge branch, which assumes a nested dict-of-dicts shape and
    # would raise on a flat one.
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      variables: {TIMEOUT: "30s"}
        """)
        build.loads("""
                test:
                    - name: boot
                      variables: {TIMEOUT: "60s", RETRIES: "3"}
        """)
        self.assertEqual(build.spec["test"][0]["variables"],
                         {"TIMEOUT": "60s", "RETRIES": "3"})

class TestEntriesWithDifferentNamesAreNotMerged(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                test:
                    - name: boot
                      tags: [smoke]
        """)
        build.loads("""
                test:
                    - name: rebuild-busybox
                      tags: [smoke]
        """)
        self.assertEqual([t["name"] for t in build.spec["test"]],
                         ["boot", "rebuild-busybox"])
