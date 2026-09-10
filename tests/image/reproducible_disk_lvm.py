#!/usr/bin/env python3

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH

# Sibling of reproducible_disk.py's test, but with an LVM PV/VG/LV layout:
# one GPT partition, one VG, two linear LVs. lvm2 stamps random UUIDs and
# wall-clock time with no override; imager_appliance.py's wrapper pins both.
class DiskImageWithLvmIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "reproducible-disk-lvm.img"

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
                % {"arch": HOST_ARCH, "ts": self.SNAPSHOT})
        return [where]
