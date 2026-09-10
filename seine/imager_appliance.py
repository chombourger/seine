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
# Needed inside the built appliance itself (LVM_WRAPPER_SCRIPT's
# interpreter and LD_PRELOAD library). Listed both here, so supermin can
# resolve them, and in its own hint directory, so it bundles them in.
EXTRA_APPLIANCE_PACKAGES = ["libfaketime", "python3"]
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
        apt_packages = (APT_PACKAGES + EXTRA_APPLIANCE_PACKAGES
                        + (UKI_APT_PACKAGES if self.distro["release"] != "bookworm" else []))
        return self.build(
            IMAGER_APPLIANCE_SCRIPT.format(
                base=self.source.targetBootstrap.name,
                apt_setup=packages.apt_setup_layer(self.distro),
                kernel=self.package,
                apt_packages=" ".join(apt_packages),
                extra_packages=" ".join(EXTRA_APPLIANCE_PACKAGES),
                host_cpu=info["host_cpu"],
                triplet=info["triplet"],
                binaries=" ".join(BINARIES),
                lvm_wrapper=LVM_WRAPPER_SCRIPT),
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

# lvm2 dispatches pvcreate/vgcreate/lvcreate through one 'lvm' binary
# chosen by argv[0]'s basename, and gives each a random UUID with no
# override -- this wrapper freezes the time and pins the UUIDs instead.
LVM_WRAPPER_SCRIPT = r"""#!/usr/bin/python3
import hashlib
import os
import re
import subprocess
import sys
import time

REAL = "/usr/sbin/.lvm-real/lvm"
BACKUP = "/tmp/seine-vgcfg-restore"

def cmdline(name):
    with open("/proc/cmdline") as f:
        m = re.search(r"\b%s=(\S+)" % name, f.read())
    return m.group(1) if m else None

epoch = cmdline("faketime")
seed = cmdline("seed")

if epoch:
    os.environ["TZ"] = "UTC"
    os.environ["LD_PRELOAD"] = "@LIBFAKETIME@"
    os.environ["FAKETIME"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(int(epoch)))

def run(*args):
    subprocess.run([REAL] + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# A stable UUID derived from the spec's own seed plus a name, so the same
# spec always gets the same ids and two different specs never collide.
def uuid_for(key):
    digest = hashlib.md5(("%s:%s" % (seed, key)).encode()).hexdigest()
    parts = [digest[0:6], digest[6:10], digest[10:14],
             digest[14:18], digest[18:22], digest[22:26], digest[26:32]]
    return "-".join(parts)

# Replaces only the id value after "<name> {", so every other byte (and
# so every offset and checksum) stays exactly where it was.
def pin(blob, name, new_id):
    pattern = rb'(' + re.escape(name).encode() + rb' \{\s*\n[ \t]*id = ")[0-9A-Za-z-]+(")'
    return re.sub(pattern, lambda m: m.group(1) + new_id.encode() + m.group(2), blob)

def pin_ids(vg):
    run("vgcfgbackup", "-f", BACKUP, vg)
    with open(BACKUP, "rb") as f:
        text = f.read()

    device = re.search(rb'device = "([^"]+)"', text)
    pe_start = re.search(rb'pe_start = (\d+)', text)
    # A PV entry sits at the same indentation as an LV entry, so only
    # look for LV names after "logical_volumes {" -- otherwise a PV's
    # already-correct id gets overwritten with an LV-keyed one.
    after_lvs = text.split(b"logical_volumes {", 1)
    lvs = [m.decode() for m in re.findall(rb'\n\t\t([A-Za-z0-9_.+-]+) \{\n', after_lvs[1])] \
        if len(after_lvs) > 1 else []

    for name in [vg] + lvs:
        key = "vg:%s" % name if name == vg else "lv:%s:%s" % (vg, name)
        text = pin(text, name, uuid_for(key))
    with open(BACKUP, "wb") as f:
        f.write(text)
    run("vgcfgrestore", "--force", "--yes", "-f", BACKUP, vg)
    os.remove(BACKUP)

    # vgcfgrestore only fixes the live metadata copy. lvm2 never
    # overwrites metadata in place -- it appends each change after the
    # last one -- so the original, unpinned copy this command wrote is
    # still sitting in the PV's metadata area and needs scrubbing too.
    if device and pe_start:
        ring_size = int(pe_start.group(1)) * 512
        with open(device.group(1), "r+b") as f:
            ring = f.read(ring_size)
            for name in [vg] + lvs:
                key = "vg:%s" % name if name == vg else "lv:%s:%s" % (vg, name)
                ring = pin(ring, name, uuid_for(key))
            f.seek(0)
            f.write(ring)

args = sys.argv[1:]
sub = args[0] if args else None

if not seed or sub not in ("pvcreate", "vgcreate", "lvcreate"):
    os.execv(REAL, [REAL] + args)

if sub == "pvcreate":
    dev = args[-1]
    os.execv(REAL, [REAL] + args + ["--uuid", uuid_for("pv:%s" % dev), "--norestorefile"])

# guestfsd always calls "vgcreate <vgname> <pvdev...>" and
# "lvcreate --yes -L <size> -n <lvname> <vgname>", so the VG name sits
# at a different argv position for each.
vg = args[1] if sub == "vgcreate" else args[-1]
rc = subprocess.run([REAL] + args).returncode
if rc == 0:
    pin_ids(vg)
sys.exit(rc)
"""

# Only vmlinuz and modules are needed, so skip the initramfs build.
#
# Split into separate RUN steps: a heredoc can't sit inside a
# backslash-continued RUN, and the moved-aside real 'lvm' binary must be
# listed as a supermin hostfile so it isn't dropped from the build.
IMAGER_APPLIANCE_SCRIPT = """
FROM {base}
{apt_setup}RUN apt-get update -qqy && \\
    INITRD=No apt-get install -qqy --no-install-recommends \\
        {kernel} supermin libguestfs0 {apt_packages} && \\
    mkdir -p /appliance /extra-tools /seine-hints /usr/sbin/.lvm-real && \\
    mv /usr/sbin/lvm /usr/sbin/.lvm-real/lvm && \\
    echo /usr/sbin/.lvm-real/lvm >/seine-hints/hostfiles && \\
    printf '%s\\n' {extra_packages} >/seine-hints/packages

RUN <<'SEINE_LVM_WRAPPER' cat >/usr/sbin/lvm
{lvm_wrapper}SEINE_LVM_WRAPPER

RUN libfaketime=$(dpkg -L libfaketime | grep -E '/libfaketime\\.so\\.[0-9]+$') && \\
    sed -i "s#@LIBFAKETIME@#$libfaketime#" /usr/sbin/lvm && \\
    chmod +x /usr/sbin/lvm

RUN supermin --build --verbose --copy-kernel -f ext2 --host-cpu {host_cpu} \\
        /usr/lib/{triplet}/guestfs/supermin.d /seine-hints -o /appliance && \\
    for bin in {binaries}; do \\
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
