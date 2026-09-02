# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine           import packages
from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine
from seine.utils import IMAGER_KIND

# Files libguestfs expects in a LIBGUESTFS_PATH "fixed appliance" directory.
APPLIANCE_FILES = ["kernel", "initrd", "root", "README.fixed"]

# Debian multiarch triplet and supermin --host-cpu value per architecture.
# Only arm64 has been verified via a real cross-arch build so far.
ARCH_INFO = {
    "amd64": {"triplet": "x86_64-linux-gnu",   "host_cpu": "x86_64"},
    "arm64": {"triplet": "aarch64-linux-gnu",  "host_cpu": "aarch64"},
    "armhf": {"triplet": "arm-linux-gnueabihf", "host_cpu": "armv7l"},
    "i386":  {"triplet": "i386-linux-gnu",      "host_cpu": "i686"},
}

# supermin cannot cross-build appliances (it always builds for the arch it
# itself runs as -- see supermin(1)/guestfs-internals(1)). The documented
# way around this is the "fixed appliance" mechanism: build a real appliance
# on a machine that IS the target arch, then point libguestfs at the
# pre-built kernel/initrd/root via LIBGUESTFS_PATH, skipping its own
# supermin auto-build entirely. We get a "machine that is the target arch"
# for free via TargetBootstrap (a mmdebstrap'd rootfs that already
# runs its own binaries transparently under binfmt).
#
# libguestfs-make-fixed-appliance builds this by running `guestfish -a
# /dev/null run`, i.e. it boots the freshly-built appliance under nested
# system emulation (qemu-system-<arch> running inside a qemu-user-static
# emulated container) purely as a side effect of triggering its cache. That
# boot is where all the cost is -- confirmed via LIBGUESTFS_DEBUG=1, which
# shows supermin's own package-tarball build finishing near-instantly, well
# before the (very slow) boot starts. So we call supermin directly with the
# same arguments libguestfs uses internally and skip the boot: it writes
# kernel/initrd/root straight into the output directory, and we validate the
# result afterwards using our own qemu-wrapper harness instead.
class ImagerAppliance(Bootstrap):
    # The appliance libguestfs runs when it cannot build its own, which is
    # every cross build. Built on the target bootstrap, and the image
    # whose reuse is the point of carrying images at all.
    kind = IMAGER_KIND

    def __init__(self, source, imagerKernel):
        self.source = source
        self.imagerKernel = imagerKernel
        self.keep = source.options["keep"]
        distro = source.spec["distribution"]
        super().__init__(distro, source.options)

    def defaultName(self):
        return os.path.join("imager-appliance", self.distro["source"], self.distro["release"], self.distro["architecture"])

    def create(self):
        arch = self.distro["architecture"]
        info = ARCH_INFO.get(arch)
        if info is None:
            raise NotImplementedError(
                "cross-building the imager appliance for architecture "
                "'%s' is not yet supported (unknown multiarch triplet)" % arch)

        # This image is built FROM the imager kernel's, which may carry a
        # sources.list pointing at the rebuilt packages: without the same
        # mount its own 'apt-get update' would fail.
        return self.build(
            IMAGER_APPLIANCE_SCRIPT.format(
                self.imagerKernel.name, info["host_cpu"], info["triplet"]),
            base=self.imagerKernel.name,
            options=packages.build_volumes(self.distro))

    # Extracts kernel/initrd/root/README.fixed from the image built by
    # create() into output_dir/appliance. Returns that directory, suitable
    # for LIBGUESTFS_PATH.
    def extract(self, output_dir):
        ContainerEngine.extractImage(self.name, output_dir, lambda n: n.startswith("appliance/"))

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
        return appliance_dir

APPLIANCE_README = """\
This is a "fixed appliance" for libguestfs, built by seine using supermin
directly (see seine/imager_appliance.py). Point LIBGUESTFS_PATH at this
directory to use it in place of libguestfs's own supermin auto-build.
"""

IMAGER_APPLIANCE_SCRIPT = """
FROM {0}
RUN apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends supermin libguestfs0 && \\
    mkdir -p /appliance && \\
    supermin --build --verbose --copy-kernel -f ext2 --host-cpu {1} \\
        /usr/lib/{2}/guestfs/supermin.d -o /appliance && \\
    apt-get clean
CMD /bin/true
"""
