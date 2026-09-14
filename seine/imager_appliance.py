# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine           import packages
from seine.bootstrap import Bootstrap
from seine.container import ContainerEngine
from seine.utils import IMAGER_KIND

# Fallback kernel per arch when the spec has no 'imager: kernel:'.
DEFAULT_PACKAGES = {
    "amd64": "linux-image-amd64",
    "arm64": "linux-image-arm64",
    "armhf": "linux-image-armmp",
    "i386":  "linux-image-686",
}

# Files libguestfs expects in a LIBGUESTFS_PATH "fixed appliance" directory.
APPLIANCE_FILES = ["kernel", "initrd", "root", "README.fixed"]

# Debian multiarch triplet and supermin --host-cpu value per architecture.
ARCH_INFO = {
    "amd64": {"triplet": "x86_64-linux-gnu",   "host_cpu": "x86_64"},
    "arm64": {"triplet": "aarch64-linux-gnu",  "host_cpu": "aarch64"},
    "armhf": {"triplet": "arm-linux-gnueabihf", "host_cpu": "armv7l"},
    "i386":  {"triplet": "i386-linux-gnu",      "host_cpu": "i686"},
}

# Run as container commands, not extracted like BINARIES below.
APT_PACKAGES = ["squashfs-tools", "erofs-utils", "binutils", "sbsigntool",
                "cryptsetup-bin", "mtools", "e2fsprogs"]
BINARIES = [
    "/usr/bin/mksquashfs", "/usr/bin/mkfs.erofs",
    # Rebuilds a verity hash tree, see imager.py's _build_verity().
    "/usr/sbin/veritysetup",
    # Rebuild a FAT partition deterministically, see
    # imager.py's _normalize_fat_tree().
    "/usr/bin/mformat", "/usr/bin/mcopy", "/usr/bin/mmd",
    # Rebuild an ext2/3/4 partition deterministically, see
    # imager.py's _normalize_ext_mount().
    "/usr/sbin/mke2fs", "/usr/sbin/debugfs",
]

# UKI needs systemd 257; not available for bookworm.
UKI_APT_PACKAGES = ["systemd-ukify", "systemd-boot-efi"]

# One custom appliance per (source, release, architecture), built the
# same way for every architecture. Bundles the fixed appliance and the
# extra tool binaries in one container, so we build it only once.
class ImagerAppliance(Bootstrap):
    kind = IMAGER_KIND

    def __init__(self, source):
        self.source = source
        self.keep = source.options["keep"]
        distro = source.spec["distribution"]
        imager_spec = source.spec.get("imager") or {}
        self.package = imager_spec.get("kernel") or DEFAULT_PACKAGES.get(distro["architecture"])
        if self.package is None:
            raise ValueError(
                "no 'imager: kernel:' package configured in the specification and no "
                "default is known for architecture '%s'" % distro["architecture"])
        super().__init__(distro, source.options)

    def defaultName(self):
        return os.path.join("imager-appliance", self.distro["source"],
                            self.distro["release"], self.distro["architecture"])

    def create(self):
        arch = self.distro["architecture"]
        info = ARCH_INFO.get(arch)
        if info is None:
            raise NotImplementedError(
                "building the imager appliance for architecture "
                "'%s' is not yet supported (unknown multiarch triplet)" % arch)
        apt_packages = (APT_PACKAGES
                        + (UKI_APT_PACKAGES if self.distro["release"] != "bookworm" else []))
        return self.build(
            IMAGER_APPLIANCE_SCRIPT.format(
                self.source.targetBootstrap.name,
                packages.apt_setup_layer(self.distro),
                self.package, " ".join(apt_packages),
                info["host_cpu"], info["triplet"],
                " ".join(BINARIES)),
            base=self.source.targetBootstrap.name,
            options=packages.build_volumes(self.distro))

    # Flat, not real paths like /usr/bin: /usr may be the mount being
    # packed away on a usrmerged system.
    def extract(self, output_dir):
        ContainerEngine.extractImage(self.name, output_dir, lambda n:
            n.startswith("appliance/") or n.startswith("extra-tools/"))

        appliance_dir = os.path.join(output_dir, "appliance")
        readme_path = os.path.join(appliance_dir, "README.fixed")
        if not os.path.isfile(readme_path):
            with open(readme_path, "w") as f:
                f.write(APPLIANCE_README)

        missing = [f for f in APPLIANCE_FILES if not os.path.isfile(os.path.join(appliance_dir, f))]
        if missing:
            raise RuntimeError(
                "fixed appliance for architecture '%s' is missing: %s"
                % (self.distro["architecture"], missing))

        tools_root = os.path.join(output_dir, "extra-tools")
        extra_tools_files = [os.path.join(dirpath, filename)
                             for dirpath, _, filenames in os.walk(tools_root)
                             for filename in filenames]
        return appliance_dir, extra_tools_files

APPLIANCE_README = """\
This is a "fixed appliance" for libguestfs, built by seine using supermin
directly (see seine/imager_appliance.py). Point LIBGUESTFS_PATH at this
directory to use it in place of libguestfs's own supermin auto-build.
"""

# Only vmlinuz and modules are needed, so skip the initramfs build.
IMAGER_APPLIANCE_SCRIPT = """
FROM {0}
{1}RUN apt-get update -qqy && \\
    INITRD=No apt-get install -qqy --no-install-recommends \\
        {2} supermin libguestfs0 {3} && \\
    mkdir -p /appliance /extra-tools && \\
    supermin --build --verbose --copy-kernel -f ext2 --host-cpu {4} \\
        /usr/lib/{5}/guestfs/supermin.d -o /appliance && \\
    for bin in {6}; do \\
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
