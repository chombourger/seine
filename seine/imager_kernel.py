# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine           import packages
from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine
from seine.utils import IMAGER_KIND

# Fallback kernel package per architecture, used when the spec does not set
# 'imager: kernel:'. This is the kernel that boots the (throwaway) guestfs
# appliance used to partition/format/install-grub -- it has nothing to do
# with the kernel installed into the produced image by the user's playbook.
DEFAULT_PACKAGES = {
    "amd64": "linux-image-amd64",
    "arm64": "linux-image-arm64",
    "armhf": "linux-image-armmp",
    "i386":  "linux-image-686",
}

class ImagerKernel(Bootstrap):
    # The kernel libguestfs boots to write the image. Built on the target
    # bootstrap, so it has to say what it is rather than inherit that
    # image's answer.
    kind = IMAGER_KIND

    def __init__(self, source):
        self.source = source
        self.targetBootstrap = source.targetBootstrap
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
        return os.path.join("imager-kernel", self.distro["source"], self.distro["release"], self.distro["architecture"])

    def create(self):
        return self.build(
            IMAGER_KERNEL_SCRIPT.format(
                self.targetBootstrap.name, self.package,
                packages.apt_setup_layer(self.distro)),
            base=self.targetBootstrap.name,
            options=packages.build_volumes(self.distro))

    # Extracts vmlinuz and the matching /lib/modules/<version> tree from the
    # image built by create() into output_dir. Returns (vmlinuz, moduledir,
    # version).
    def extract(self, output_dir):
        # /lib is usually a symlink to /usr/lib (usrmerge), so the tar member
        # itself is named usr/lib/modules/... -- handle both in case a
        # non-usrmerge distro is ever targeted.
        ContainerEngine.extractImage(self.name, output_dir, lambda n:
            n.startswith("boot/vmlinuz-") or
            n.startswith("lib/modules/") or
            n.startswith("usr/lib/modules/"))

        candidates = [os.path.join(output_dir, "usr", "lib", "modules"),
                      os.path.join(output_dir, "lib", "modules")]
        found = {}  # version -> modules_root actually containing it
        for modules_root in candidates:
            if os.path.isdir(modules_root):
                for version in os.listdir(modules_root):
                    found[version] = modules_root
        if len(found) != 1:
            raise RuntimeError(
                "expected exactly one kernel under /lib/modules in the imager kernel "
                "image (package '%s'), found: %s" % (self.package, sorted(found)))
        version, modules_root = next(iter(found.items()))
        vmlinuz = os.path.join(output_dir, "boot", "vmlinuz-%s" % version)
        return vmlinuz, os.path.join(modules_root, version), version

# The kernel's postinst hook (initramfs-tools) runs 'update-initramfs -c'
# unconditionally on a fresh kernel install -- the only thing it checks is
# the INITRD=No environment variable, not update-initramfs.conf. We never
# read the initrd this package produces (only vmlinuz + modules), so skip
# generating it at all.
IMAGER_KERNEL_SCRIPT = """
FROM {0}
{2}RUN apt-get update -qqy && \\
    INITRD=No apt-get install -qqy --no-install-recommends {1} && \\
    apt-get clean
CMD /bin/true
"""
