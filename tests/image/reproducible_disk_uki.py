#!/usr/bin/env python3

import avocado
import os
import sys

sys.path.append(os.path.dirname(os.path.realpath(__file__)))

from reproducible_base import ReproducibleDiskImage
from seine.utils import HOST_ARCH

# Sibling of reproducible_disk.py's test, for examples/pc-uki-image/'s
# multiconfig chain, dm-verity '/usr', and signed UKI. Reuses the real
# example, pinned to a snapshot via a side-loaded peer file.
class UkiDiskImageIsByteIdenticalAcrossTwoBuilds(ReproducibleDiskImage, avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600
    FILENAME = "pc-uki.img"
    RELEASE = "trixie"

    def setUp(self):
        super().setUp()
        if HOST_ARCH != "amd64":
            self.cancel("this spec's kernel/bootloader packages are amd64-only")

    # Overrides examples/common/trixie's live feeds with a snapshot-pinned
    # 'distribution:', field by field (a side-loaded file peer-amends,
    # rather than winning outright like a 'requires:' fragment).
    def specification(self):
        where = os.path.join(self.workdir, "snapshot-pin.yml")
        with open(where, "w") as f:
            f.write(
                "distribution:\n"
                "    uri: https://snapshot.debian.org/archive/debian/%(ts)s\n"
                "    feeds:\n"
                "        - suite: trixie\n"
                "          valid-until: false\n"
                "        - suite: trixie-updates\n"
                "          release: trixie\n"
                "          valid-until: false\n"
                "        - suite: trixie-security\n"
                "          release: trixie\n"
                "          uri: https://snapshot.debian.org/archive/debian-security/%(ts)s\n"
                "          valid-until: false\n"
                % {"ts": self.SNAPSHOT})
        return ["examples/pc-uki-image/main.yaml", where]
