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
EXTRA_IMAGER_TOOLS_SCRIPT = """
FROM {0}
{1}RUN apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends squashfs-tools erofs-utils && \\
    mkdir -p /extra-tools && \\
    for bin in /usr/bin/mksquashfs /usr/bin/mkfs.erofs; do \\
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

class ExtraImagerTools(Bootstrap):
    kind = IMAGER_KIND

    def __init__(self, source):
        self.source = source
        distro = source.spec["distribution"]
        super().__init__(distro, source.options)

    def defaultName(self):
        return os.path.join("imager-extra-tools", self.distro["source"],
                            self.distro["release"], self.distro["architecture"])

    def create(self):
        return self.build(
            EXTRA_IMAGER_TOOLS_SCRIPT.format(self.source.targetBootstrap.name,
                                             packages.apt_setup_layer(self.distro)),
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
