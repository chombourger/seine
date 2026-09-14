#!/usr/bin/env python3

import avocado
import glob
import hashlib
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import HOST_ARCH

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# A real timestamp on snapshot.debian.org, so this test does not depend
# on what bookworm's feeds currently serve.
SNAPSHOT = "20260801T000000Z"

# Sibling of reproducible_disk.py's test, but with an LVM PV/VG/LV layout:
# one GPT partition, one VG, two linear LVs. lvm2 stamps random UUIDs and
# wall-clock time with no override; imager_appliance.py's wrapper pins both.
class DiskImageWithLvmIsByteIdenticalAcrossTwoBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds a disk image twice; "
                        "this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build a disk image")

    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space], check=False)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        # ansible-playbook lives beside the python running the tests when
        # they are run from a virtual environment, and the build needs it.
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_CACHE_DIR"] = os.path.join(path, "cache")
        environment["SEINE_BUILD_DIR"] = os.path.join(path, "build")
        self.spaces.append(path)
        return environment

    def seine(self, space, args, log):
        where = os.path.join(self.outputdir, "%s.log" % log)
        with open(where, "w") as f:
            run = subprocess.run(
                [sys.executable, "-u", "./seine.py"] + args,
                cwd=path_to_sources, env=space, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))

    # One VG ('vg_sys') over a single PV, two plain linear LVs on it. No
    # EFI/boot partitions and no kernel/bootloader packages: this disk is
    # never booted, only compared byte-for-byte.
    def specification(self):
        where = os.path.join(self.workdir, "reproducible-disk-lvm.yml")
        with open(where, "w") as f:
            f.write(
                "distribution:\n"
                "    release: bookworm\n"
                "    architecture: %(arch)s\n"
                "    architectures: [%(arch)s]\n"
                "    uri: https://snapshot.debian.org/archive/debian/%(ts)s\n"
                "    feeds:\n"
                "        - suite: bookworm\n"
                "          valid-until: false\n"
                "        - suite: bookworm-updates\n"
                "          valid-until: false\n"
                "        - suite: bookworm-security\n"
                "          uri: https://snapshot.debian.org/archive/debian-security/%(ts)s\n"
                "          valid-until: false\n"
                "packages:\n"
                "    - source: apt://busybox\n"
                "      profiles: [nocheck]\n"
                "image:\n"
                "    filename: reproducible-disk-lvm.img\n"
                "    table: gpt\n"
                "    size: 512MiB\n"
                "    partitions:\n"
                "        - label: system\n"
                "          group: vg_sys\n"
                "          size: 480MiB\n"
                "          flags: [primary, lvm]\n"
                "    volumes:\n"
                "        - label: lv_root\n"
                "          group: vg_sys\n"
                "          size: 300MiB\n"
                "          where: /\n"
                "        - label: lv_data\n"
                "          group: vg_sys\n"
                "          size: 100MiB\n"
                "          where: /var\n"
                % {"arch": HOST_ARCH, "ts": SNAPSHOT})
        return [where]

    def image(self, space):
        found = glob.glob(os.path.join(
            space["SEINE_BUILD_DIR"], "deploy", "bookworm", "reproducible-disk-lvm.img"))
        self.assertEqual(len(found), 1, "no disk image in %s" % space["SEINE_BUILD_DIR"])
        return found[0]

    # The byte offset of the first difference, read in chunks rather
    # than all at once -- these images are large enough that a plain
    # 'a == b' would hold two full copies in memory for no reason.
    CHUNK = 4 * 1024 * 1024

    def firstDifference(self, one, two):
        with open(one, "rb") as f, open(two, "rb") as g:
            offset = 0
            while True:
                a, b = f.read(self.CHUNK), g.read(self.CHUNK)
                if a != b:
                    for i in range(min(len(a), len(b))):
                        if a[i] != b[i]:
                            return "byte offset %d differs (0x%02x vs 0x%02x)" % (
                                offset + i, a[i], b[i])
                    return "one image is longer than the other, at offset %d" % (
                        offset + min(len(a), len(b)))
                if not a:
                    return "no byte difference found, but the digests differ"
                offset += len(a)

    def test(self):
        first = self.space("first")
        second = self.space("second")
        spec = self.specification()

        self.seine(first, ["build", "-v", "--jobs", "2"] + spec, "build-first")
        self.seine(second, ["build", "-v", "--jobs", "2"] + spec, "build-second")

        one, two = self.image(first), self.image(second)
        with open(one, "rb") as f:
            first_digest = hashlib.file_digest(f, "sha256").hexdigest()
        with open(two, "rb") as f:
            second_digest = hashlib.file_digest(f, "sha256").hexdigest()

        self.assertEqual(
            first_digest, second_digest,
            "two builds pinned to the same snapshot (%s) produced different "
            "LVM disk images: %s" % (SNAPSHOT, self.firstDifference(one, two)))
