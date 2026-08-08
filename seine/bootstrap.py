# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

from abc import ABC, abstractmethod

import os
import subprocess
import tempfile

from seine.utils import ContainerEngine

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

    def getName(self):
        if self._name is None:
            self._name = self.defaultName()
        return self._name

    def setName(self, name):
        self._name = name

    name = property(getName, setName)

class HostBootstrap(Bootstrap):
    def create(self):
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(HOST_BOOTSTRAP_SCRIPT.format(
            self.distro["source"],
            self.distro["release"],
            "apt-{}".format(self.distro["release"])))
        dockerfile.close()

        try:
            ContainerEngine.run([
                "build", "--rm", "--squash",
                "-t", self.name, "-f", dockerfile.name],
                check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            ContainerEngine.run(["image", "prune", "-f"])
            os.unlink(dockerfile.name)
        return self

    def defaultName(self):
        return os.path.join("bootstrap", self.distro["source"], self.distro["release"], "all")

class TargetBootstrap(Bootstrap):
    def create(self, hostBootstrap):
        self.hostBootstrap = hostBootstrap
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(TARGET_BOOTSTRAP_SCRIPT.format(
            self.hostBootstrap.name,
            self.distro["architecture"],
            self.distro["release"],
            self.distro["uri"],
            "mmdebstrap-{}".format(self.distro["release"])
        ))
        dockerfile.close()

        try:
            ContainerEngine.run([
                "build", "--rm",
                "-t", self.name,
                "-f", dockerfile.name], check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            ContainerEngine.run(["image", "prune", "-f"])
            os.unlink(dockerfile.name)
        return self

    def defaultName(self):
        return os.path.join(
                "bootstrap",
                self.distro["source"],
                self.distro["release"],
                self.distro["architecture"])

HOST_BOOTSTRAP_SCRIPT = """
FROM {0}:{1} AS base
RUN --mount=type=cache,target=/var/cache/apt/archives,id={2},sharing=shared \
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
