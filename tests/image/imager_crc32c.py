#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.imager import Imager

# _pin_ext_mtimes() patched superblocks with a complemented checksum;
# e2fsck rejected them. ext4 stores crc32c with no final complement.
class Crc32cMatchesExt4sOwnConvention(avocado.Test):
    def test(self):
        # Standard CRC-32C tools report the complemented form.
        textbook = 0xe3069283
        self.assertEqual(Imager._crc32c(None, b"123456789"),
                         textbook ^ 0xffffffff)
