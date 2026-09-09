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

# Builds the same snapshot-pinned spec as a full disk image, twice, and
# checks the two '.img' files are byte-for-byte identical.
class DiskImageIsByteIdenticalAcrossTwoBuilds(avocado.Test):
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
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel/bootloader packages are amd64-only")

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

    # Same disk layout as examples/common/pc-image.yaml (EFI, /boot,
    # root and /var), but self-contained so it can use our own
    # snapshot-pinned feeds instead of the real ones. Plain GPT
    # partitions, not LVM: lvm2 has no reproducible-builds support at
    # all (no SOURCE_DATE_EPOCH equivalent, and its own metadata
    # writes always stamp themselves with the real time), so an LVM
    # layout can never be made byte-identical here.
    def specification(self):
        where = os.path.join(self.workdir, "reproducible-disk.yml")
        with open(where, "w") as f:
            f.write(
                "distribution:\n"
                "    release: bookworm\n"
                "    architecture: amd64\n"
                "    architectures: [amd64]\n"
                "    uri: https://snapshot.debian.org/archive/debian/%(ts)s\n"
                "    feeds:\n"
                "        - suite: bookworm\n"
                "          valid-until: false\n"
                "        - suite: bookworm-updates\n"
                "          valid-until: false\n"
                "        - suite: bookworm-security\n"
                "          uri: https://snapshot.debian.org/archive/debian-security/%(ts)s\n"
                "          valid-until: false\n"
                "imager:\n"
                "    kernel: linux-image-amd64\n"
                "playbook:\n"
                "    - name: base packages\n"
                "      priority: 100\n"
                "      tasks:\n"
                "          - name: install systemd and udev\n"
                "            apt:\n"
                "                state: present\n"
                "                name: [systemd-sysv, udev]\n"
                "    - name: boot packages\n"
                "      priority: 800\n"
                "      tasks:\n"
                "          - name: install grub\n"
                "            apt:\n"
                "                state: present\n"
                "                name: [grub-efi-amd64, grub-efi-amd64-signed]\n"
                "          - name: install kernel and firmware\n"
                "            apt:\n"
                "                state: present\n"
                "                name: [linux-image-amd64, firmware-linux-free]\n"
                "image:\n"
                "    filename: reproducible-disk.img\n"
                "    table: gpt\n"
                "    size: 3072MiB\n"
                "    partitions:\n"
                "        - label: efi\n"
                "          type: vfat\n"
                "          size: 16MiB\n"
                "          where: /efi\n"
                "          flags: [boot, primary]\n"
                "        - label: boot\n"
                "          type: ext2\n"
                "          size: 128MiB\n"
                "          where: /boot\n"
                "          flags: [primary]\n"
                "        - label: root\n"
                "          type: ext4\n"
                "          size: 2048MiB\n"
                "          where: /\n"
                "          flags: [primary]\n"
                "        - label: data\n"
                "          type: ext4\n"
                "          size: 512MiB\n"
                "          where: /var\n"
                "          flags: [primary]\n"
                % {"ts": SNAPSHOT})
        return [where]

    def image(self, space):
        found = glob.glob(os.path.join(
            space["SEINE_BUILD_DIR"], "deploy", "bookworm", "reproducible-disk.img"))
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
            "disk images: %s" % (SNAPSHOT, self.firstDifference(one, two)))
