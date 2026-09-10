#!/usr/bin/env python3

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH

# Builds the same snapshot-pinned spec as a full disk image, twice, and
# checks the two '.img' files are byte-for-byte identical. See
# reproducible_disk_lvm.py for the LVM sibling of this test.
class DiskImageIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "reproducible-disk.img"

    def setUp(self):
        super().setUp()
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel/bootloader packages are amd64-only")

    # Same disk layout as examples/common/pc-image.yaml (EFI, /boot,
    # root and /var), but self-contained so it can use our own
    # snapshot-pinned feeds instead of the real ones.
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
                % {"ts": self.SNAPSHOT})
        return [where]
