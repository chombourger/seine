# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import tarfile
import tempfile

from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine

# Files libguestfs expects in a LIBGUESTFS_PATH "fixed appliance" directory.
APPLIANCE_FILES = ["kernel", "initrd", "root", "README.fixed"]

# supermin cannot cross-build appliances (it always builds for the arch it
# itself runs as -- see supermin(1)/guestfs-internals(1)). The documented
# way around this is the "fixed appliance" mechanism: build a real appliance
# on a machine that IS the target arch, then point libguestfs at the
# pre-built kernel/initrd/root via LIBGUESTFS_PATH, skipping its own
# supermin auto-build entirely. We get a "machine that is the target arch"
# for free via TargetBootstrap (a qemu-debootstrap'd rootfs that already
# runs its own binaries transparently under binfmt) -- so we just need
# libguestfs-tools (for libguestfs-make-fixed-appliance) installed there.
class ImagerAppliance(Bootstrap):
    def __init__(self, source, imagerKernel):
        self.source = source
        self.imagerKernel = imagerKernel
        self.keep = source.options["keep"]
        distro = source.spec["distribution"]
        super().__init__(distro, source.options)

    def defaultName(self):
        return os.path.join("imager-appliance", self.distro["source"], self.distro["release"], self.distro["architecture"])

    def container_id(self):
        return self.name.replace("/", "-")

    def create(self):
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(IMAGER_APPLIANCE_SCRIPT.format(self.imagerKernel.name))
        dockerfile.close()
        try:
            ContainerEngine.run([
                "build", "--rm",
                "-t", self.name,
                "-f", dockerfile.name], check=True)
        finally:
            if self.keep:
                print("keeping '%s' (dockerfile for the imager appliance) as requested" % dockerfile.name)
            else:
                os.unlink(dockerfile.name)

    # Extracts kernel/initrd/root/README.fixed from the image built by
    # create() into output_dir/appliance. Returns that directory, suitable
    # for LIBGUESTFS_PATH.
    def extract(self, output_dir):
        cid = self.container_id()
        try:
            ContainerEngine.run(["container", "create", "--name", cid, self.name], check=True)
            podman_proc = ContainerEngine.Popen(["container", "export", cid], stdout=subprocess.PIPE)
            with tarfile.open(fileobj=podman_proc.stdout, mode="r|") as tar:
                for member in tar:
                    if member.name.startswith("appliance/"):
                        tar.extract(member, path=output_dir)
            podman_proc.wait()
        finally:
            ContainerEngine.run(["container", "rm", cid], check=False)

        appliance_dir = os.path.join(output_dir, "appliance")
        missing = [f for f in APPLIANCE_FILES if not os.path.isfile(os.path.join(appliance_dir, f))]
        if missing:
            raise RuntimeError(
                "fixed appliance for architecture '%s' is missing: %s"
                % (self.distro["architecture"], missing))
        return appliance_dir

IMAGER_APPLIANCE_SCRIPT = """
FROM {0}
ENV LIBGUESTFS_BACKEND_SETTINGS=force_tcg
RUN apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends libguestfs-tools && \\
    libguestfs-make-fixed-appliance /appliance && \\
    apt-get clean
CMD /bin/true
"""
