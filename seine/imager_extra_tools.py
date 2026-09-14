# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine           import packages
from seine.bootstrap import Bootstrap
from seine.container import ContainerEngine
from seine.utils import IMAGER_KIND

# Built FROM the target bootstrap since g.sh() chroots into the target,
# not the appliance. glibc is skipped: loading a mismatched libc.so.6
# under the target's own dynamic linker caused "stack smashing detected".
EXTRA_IMAGER_TOOLS_SCRIPT = """
FROM {0}
{1}RUN apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends {2} && \\
    mkdir -p /extra-tools && \\
    for bin in {3}; do \\
        cp --parents "$bin" /extra-tools; \\
        for lib in $(ldd "$bin" 2>/dev/null | grep -oE '/[^ ]+'); do \\
            case "$lib" in \\
                */libc.so*|*/libm.so*|*/libpthread.so*|*/librt.so*| \\
                */libdl.so*|*/libresolv.so*|*/libutil.so*|*/libnsl.so*| \\
                */ld-linux*) continue ;; \\
            esac; \\
            [ -f "$lib" ] && cp --parents "$lib" /extra-tools || true; \\
        done; \\
    done && \\
    apt-get clean
CMD /bin/true
"""

# squashfs-tools/erofs-utils/binutils/sbsigntool run as container
# commands in this image too, not copied out like BINARIES below.
APT_PACKAGES = ["squashfs-tools", "erofs-utils", "binutils", "sbsigntool"]
BINARIES = ["/usr/bin/mksquashfs", "/usr/bin/mkfs.erofs"]

# UKI needs systemd 257 (ukify split into its own package there); not
# available for bookworm at all, only from trixie on.
UKI_APT_PACKAGES = ["systemd-ukify", "systemd-boot-efi"]

VERITY_APT_PACKAGES = ["cryptsetup-bin"]
VERITY_BINARIES = ["/usr/sbin/veritysetup"]

# mtools rebuilds a FAT partition deterministically (serial and
# timestamps fixed) -- see imager.py's _normalize_fat_tree().
FAT_APT_PACKAGES = ["mtools"]
FAT_BINARIES = ["/usr/bin/mformat", "/usr/bin/mcopy", "/usr/bin/mmd"]

# mke2fs -d rebuilds an ext2/3/4 partition deterministically from a
# captured directory tree -- see imager.py's _normalize_ext_mount().
# debugfs then fixes up 'lost+found', which mke2fs stamps itself.
EXT_APT_PACKAGES = ["e2fsprogs"]
EXT_BINARIES = ["/usr/sbin/mke2fs", "/usr/sbin/debugfs"]

class ExtraImagerTools(Bootstrap):
    kind = IMAGER_KIND

    def __init__(self, source, need_verity=False, need_fat=False, need_ext=False):
        self.source = source
        self.need_verity = need_verity
        self.need_fat = need_fat
        self.need_ext = need_ext
        distro = source.spec["distribution"]
        super().__init__(distro, source.options)

    def defaultName(self):
        return os.path.join("imager-extra-tools", self.distro["source"],
                            self.distro["release"], self.distro["architecture"])

    def create(self):
        apt_packages = (APT_PACKAGES
                        + (UKI_APT_PACKAGES if self.distro["release"] != "bookworm" else [])
                        + (VERITY_APT_PACKAGES if self.need_verity else [])
                        + (FAT_APT_PACKAGES if self.need_fat else [])
                        + (EXT_APT_PACKAGES if self.need_ext else []))
        binaries = (BINARIES + (VERITY_BINARIES if self.need_verity else [])
                   + (FAT_BINARIES if self.need_fat else [])
                   + (EXT_BINARIES if self.need_ext else []))
        return self.build(
            EXTRA_IMAGER_TOOLS_SCRIPT.format(
                self.source.targetBootstrap.name,
                packages.apt_setup_layer(self.distro),
                " ".join(apt_packages), " ".join(binaries)),
            base=self.source.targetBootstrap.name,
            options=packages.build_volumes(self.distro))

    # Extracted flat (not to real paths like /usr/bin) since /usr may be
    # the mount being packed away on a usrmerged system.
    def extract(self, output_dir):
        ContainerEngine.extractImage(self.name, output_dir, lambda n: n.startswith("extra-tools/"))
        root = os.path.join(output_dir, "extra-tools")
        return [os.path.join(dirpath, filename)
                for dirpath, _, filenames in os.walk(root)
                for filename in filenames]
