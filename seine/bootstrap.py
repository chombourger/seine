# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

from abc import ABC, abstractmethod

import hashlib
import os
import tempfile

from seine.utils import ContainerEngine
from seine.utils import INPUTS_LABEL
from seine.utils import apt_sources

class Bootstrap(ABC):
    def __init__(self, distro, options):
        self._name = None
        self.distro = distro
        self.options = options
        super().__init__()

    @abstractmethod
    def create(self):
        pass

    @abstractmethod
    def defaultName(self):
        pass

    # What this image was built from: the Dockerfile seine would write for
    # it now, and the id of the image it is built FROM. Recorded on the
    # image as a label, so an image is rebuilt when either has changed.
    #
    # Without it an image is only ever matched by name, and every image
    # derived from another goes stale the moment that one is rebuilt --
    # silently, since a stale image is a working image, just not one built
    # from what the specification now says. An image built by a seine that
    # did not label them has no label, and is rebuilt once.
    def digest(self, dockerfile, base=None):
        digest = hashlib.sha256()
        digest.update(dockerfile.encode())
        if base is not None:
            digest.update((ContainerEngine.imageId(base) or "").encode())
        return digest.hexdigest()[:16]

    def current(self, dockerfile, base=None):
        return ContainerEngine.imageLabel(self.name, INPUTS_LABEL) \
               == self.digest(dockerfile, base)

    # Builds the image from 'dockerfile' unless one built from the same
    # inputs is already there.
    def build(self, dockerfile, base=None, options=None):
        if self.current(dockerfile, base):
            return self

        written = tempfile.NamedTemporaryFile(mode="w", delete=False)
        written.write(dockerfile)
        written.close()
        try:
            ContainerEngine.run(
                ["build", "--rm"] + (options or []) +
                ["--label", "%s=%s" % (INPUTS_LABEL, self.digest(dockerfile, base)),
                 "-t", self.name, "-f", written.name], check=True)
        finally:
            ContainerEngine.run(["image", "prune", "-f"])
            if self.options.get("keep"):
                print("keeping '%s' (dockerfile for %s) as requested"
                      % (written.name, self.name))
            else:
                os.unlink(written.name)
        return self

    def getName(self):
        if self._name is None:
            self._name = self.defaultName()
        return self._name

    def setName(self, name):
        self._name = name

    name = property(getName, setName)

class HostBootstrap(Bootstrap):
    def create(self):
        return self.build(HOST_BOOTSTRAP_SCRIPT.format(
            self.distro["source"],
            self.distro["release"],
            "apt-{}".format(self.distro["release"])), options=["--squash"])

    def defaultName(self):
        return os.path.join("bootstrap", self.distro["source"], self.distro["release"], "all")

class TargetBootstrap(Bootstrap):
    def create(self, hostBootstrap):
        self.hostBootstrap = hostBootstrap
        return self.build(TARGET_BOOTSTRAP_SCRIPT.format(
            self.hostBootstrap.name,
            self.distro["architecture"],
            self.distro["release"],
            " ".join("'%s'" % source for source in apt_sources(self.distro)),
            "mmdebstrap-{}".format(self.distro["release"])),
            base=self.hostBootstrap.name)

    def defaultName(self):
        return os.path.join(
                "bootstrap",
                self.distro["source"],
                self.distro["release"],
                self.distro["architecture"])

HOST_BOOTSTRAP_SCRIPT = """
FROM {0}:{1} AS base
RUN --mount=type=cache,target=/var/cache/apt/archives,id={2},sharing=locked \
     rm -f /etc/apt/apt.conf.d/docker-clean &&    \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         arch-test debian-archive-keyring gpg     \
         mmdebstrap qemu-user-static
FROM base AS clean-base
RUN rm -rf /usr/share/doc                        \
           /usr/share/info                       \
           /usr/share/man
"""

TARGET_BOOTSTRAP_SCRIPT = """
FROM {0} AS bootstrap
RUN --mount=type=cache,target=/var/cache/mmdebstrap,id={4},sharing=locked \
    export container=lxc;                                            \
    mkdir -p rootfs &&                                               \
    mmdebstrap --mode=root --variant=minbase --include=zstd          \
        --skip=essential/unlink                                      \
        --setup-hook='mkdir -p "$1"/var/cache/apt/archives/'         \
        --setup-hook='sync-in /var/cache/mmdebstrap /var/cache/apt/archives/' \
        --customize-hook='sync-out /var/cache/apt/archives /var/cache/mmdebstrap' \
        --arch {1} {2} rootfs {3} &&                                 \
    cp /usr/bin/qemu-*-static rootfs/usr/bin/ &&                     \
    echo 'APT::Install-Recommends "false";'                          \
        >rootfs/etc/apt/apt.conf.d/00-no-recommends &&               \
    echo 'APT::Install-Suggests "false";'                            \
        >rootfs/etc/apt/apt.conf.d/00-no-suggests
FROM scratch AS base
COPY --from=bootstrap rootfs/ /
RUN  apt-get clean -qqy && \
     rm -rf /usr/share/doc /usr/share/info /usr/share/man
"""
