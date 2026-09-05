# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine           import packages
from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine
from seine.utils import IMAGER_KIND

# Built FROM the target bootstrap: g.sh() in imager.py chroots into the
# mounted target, never the appliance's own filesystem. glibc itself is
# excluded from extraction -- a version-skewed libc.so.6 loaded under the
# target's own (unreplaced) dynamic linker caused a real "stack smashing
# detected" abort; every target already has a working glibc of its own.
#
# 'cryptsetup-bin' (veritysetup) is only pulled in when a 'verity: true'
# mount asks for it -- the installed-package list is part of the
# Dockerfile text Bootstrap.digest() hashes, so the image is rebuilt (not
# silently reused) whenever a spec starts or stops needing it.
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

# 'systemd-ukify'/'systemd-boot-efi'/'binutils'/'sbsigntool' aren't
# ldd-copied like BINARIES below -- imager.py runs them as container
# commands against this image itself, not the guest. 'systemd-boot-efi'
# ships the '.stub' file 'ukify build' needs.
APT_PACKAGES = ["squashfs-tools", "erofs-utils",
                "systemd-ukify", "systemd-boot-efi", "binutils", "sbsigntool"]
BINARIES = ["/usr/bin/mksquashfs", "/usr/bin/mkfs.erofs"]

VERITY_APT_PACKAGES = ["cryptsetup-bin"]
VERITY_BINARIES = ["/usr/sbin/veritysetup"]

class ExtraImagerTools(Bootstrap):
    kind = IMAGER_KIND

    def __init__(self, source, need_verity=False):
        self.source = source
        self.need_verity = need_verity
        distro = source.spec["distribution"]
        super().__init__(distro, source.options)

    def defaultName(self):
        return os.path.join("imager-extra-tools", self.distro["source"],
                            self.distro["release"], self.distro["architecture"])

    def create(self):
        apt_packages = APT_PACKAGES + (VERITY_APT_PACKAGES if self.need_verity else [])
        binaries = BINARIES + (VERITY_BINARIES if self.need_verity else [])
        return self.build(
            EXTRA_IMAGER_TOOLS_SCRIPT.format(
                self.source.targetBootstrap.name,
                packages.apt_setup_layer(self.distro),
                " ".join(apt_packages), " ".join(binaries)),
            base=self.source.targetBootstrap.name,
            options=packages.build_volumes(self.distro))

    # Real paths like /usr/bin aren't a safe upload target: on a usrmerged
    # system they're under /usr, often the very mount being packed away.
    # imager.py uploads these flat under a scratch dir outside every mount
    # instead, with LD_LIBRARY_PATH pointed at it.
    def extract(self, output_dir):
        ContainerEngine.extractImage(self.name, output_dir, lambda n: n.startswith("extra-tools/"))
        root = os.path.join(output_dir, "extra-tools")
        return [os.path.join(dirpath, filename)
                for dirpath, _, filenames in os.walk(root)
                for filename in filenames]
