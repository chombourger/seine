# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import tempfile

from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine

# Rebuilding source packages happens under sbuild's "unshare" backend, the
# one Debian's own buildds use: it needs no schroot, no daemon and no root,
# just user namespaces. Running it inside one of our podman containers means
# nesting a user namespace inside podman's own, which needs four things that
# a plain 'podman run' does not give us -- each of them found by hitting the
# failure it causes:
#
#  * the container has to run as root (uid 0, i.e. the unprivileged user
#    seine runs as). A non-root container user cannot use newuidmap at all:
#    every write to uid_map comes back EPERM, even with the setuid bit
#    intact and CAP_SETUID added to the container.
#
#  * /etc/subuid and /etc/subgid have to cover 65534. apt drops privileges
#    to _apt/nobody inside the chroot and setgroups(65534) fails with
#    EINVAL when that id falls outside the mapped range, which shows up as
#    an unexplained 'apt-get update' failure.
#
#  * CAP_SYS_ADMIN, because sbuild-usernsexec calls sethostname() and
#    podman's default capability set does not include it.
#
#  * an unmasked /proc: podman covers several paths under /proc, and the
#    kernel refuses to mount a fresh procfs inside a nested user namespace
#    while the parent's procfs has submounts hiding parts of it.
#
# The result is a container more privileged than the others seine builds,
# though still an unprivileged one in the kernel's eyes -- uid 0 in it is
# the user seine runs as, so what it can reach is that user's own files,
# not the machine's. It is used only to build packages.
SBUILD_RUN_OPTIONS = [
    "--cap-add=sys_admin",
    "--security-opt", "unmask=ALL",
]

class BuilderImage(Bootstrap):
    def create(self, hostBootstrap):
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(BUILDER_IMAGE_SCRIPT.format(
            hostBootstrap.name,
            self.distro["source"],
            self.distro["release"],
            self.distro["uri"],
            "apt-{}".format(self.distro["release"])))
        dockerfile.close()

        try:
            ContainerEngine.run([
                "build", "--rm",
                "-t", self.name, "-f", dockerfile.name], check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            ContainerEngine.run(["image", "prune", "-f"])
            os.unlink(dockerfile.name)
        return self

    # No architecture in the name: this image is always of the host's own
    # architecture, and it is the chroot inside it that carries the target's.
    def defaultName(self):
        return os.path.join("builder", self.distro["source"], self.distro["release"])

    # Runs 'args' inside a throwaway builder container with the namespace
    # privileges sbuild needs. 'volumes' is a list of (host, container)
    # pairs. The chroot cache is always mounted where sbuild looks for its
    # tarballs by default, so neither mmdebstrap nor sbuild needs telling
    # where the chroot lives.
    def exec(self, args, architecture, volumes=None, check=True):
        cmd = ["container", "run", "--rm"] + SBUILD_RUN_OPTIONS
        cmd += ["-v", "%s:/root/.cache/sbuild" %
                ContainerEngine.chroots(self.distro["release"], architecture)]
        for host, container in volumes or []:
            cmd += ["-v", "%s:%s" % (host, container)]
        cmd += [self.name] + args
        return ContainerEngine.run(cmd, check=check)

# The buildd chroot sbuild unpacks for every package it builds. Producing
# one is a full mmdebstrap run, so it is kept in the host-side cache and
# reused; 'architecture' is the chroot's own, which for a cross build is
# the build architecture rather than the target's.
class SbuildChroot:
    def __init__(self, distro, options, architecture):
        self.architecture = architecture
        self.distro = distro
        self.options = options

    @property
    def filename(self):
        return "%s-%s.tar.zst" % (self.distro["release"], self.architecture)

    @property
    def path(self):
        return os.path.join(
            ContainerEngine.chroots(self.distro["release"], self.architecture),
            self.filename)

    def exists(self):
        return os.path.isfile(self.path)

    def create(self, builderImage):
        if self.exists():
            return self

        # --mode=root: we are already root inside the container, so there
        # is no reason to make mmdebstrap unshare a namespace of its own
        # to get there. The sync-in/sync-out hooks seed apt's archives from
        # the same download cache the rest of the build uses and put newly
        # fetched packages back, as the target bootstrap does.
        args = [
            "mmdebstrap", "--mode=root", "--variant=buildd",
            "--arch=%s" % self.architecture,
            "--setup-hook=mkdir -p \"$1\"/var/cache/apt/archives/",
            "--setup-hook=sync-in /var/cache/mmdebstrap /var/cache/apt/archives/",
            "--customize-hook=sync-out /var/cache/apt/archives /var/cache/mmdebstrap",
            self.distro["release"],
            "/root/.cache/sbuild/%s" % self.filename,
            self.distro["uri"],
        ]
        volumes = [(ContainerEngine.downloads(self.distro["release"]),
                    "/var/cache/mmdebstrap")]
        try:
            builderImage.exec(args, self.architecture, volumes=volumes)
        except subprocess.CalledProcessError:
            # A half-written tarball would be picked up as a usable chroot
            # by the next build.
            if os.path.isfile(self.path):
                os.unlink(self.path)
            raise
        return self

BUILDER_IMAGE_SCRIPT = """
FROM {0}
RUN --mount=type=cache,target=/var/cache/apt/archives,id={4},sharing=locked \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         sbuild mmdebstrap uidmap zstd            \
         dpkg-dev devscripts quilt git            \
         iproute2
# iproute2 is not optional: sbuild brings the loopback interface up with
# 'ip link set lo up' when it takes the network away from the build, and
# dies rather than warns when 'ip' is missing.
RUN echo 'deb-src {3} {2} main' > /etc/apt/sources.list.d/seine-source.list && \
    apt-get update -qqy
RUN echo 'root:1:65535' > /etc/subuid && \
    echo 'root:1:65535' > /etc/subgid
"""
