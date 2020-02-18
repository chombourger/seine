# seine - Singular Embedded Images Now Easy
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
        equivsfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        equivsfile.write(EQUIVS_CONTROL_FILE)
        equivsfile.close()

        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(HOST_BOOTSTRAP_SCRIPT.format(
            self.distro["source"],
            self.distro["release"],
            os.path.basename(equivsfile.name)))
        dockerfile.close()

        try:
            ContainerEngine.run([
                "build", "--rm", "--squash",
                "-t", self.name, "-f", dockerfile.name,
                "-v", "/tmp:/host-tmp:ro"],
                check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            ContainerEngine.run(["image", "prune", "-f"])
            os.unlink(dockerfile.name)
            os.unlink(equivsfile.name)
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
            self.distro["uri"]
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
RUN                                               \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         debootstrap equivs qemu-user-static &&   \
     mkdir -p /opt/seine &&                       \
     cd /opt/seine &&                             \
     equivs-build /host-tmp/{2} &&                \
     apt-get autoremove -qqy equivs &&            \
     apt-get clean -qqy
FROM base AS clean-base
RUN rm -rf /usr/share/doc                        \
           /usr/share/info                       \
           /usr/share/man
"""

TARGET_BOOTSTRAP_SCRIPT = """
FROM {0} AS bootstrap
RUN                                                                  \
    export container=lxc;                                            \
    qemu-debootstrap --variant=minbase --arch {1} {2} rootfs {3} &&  \
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

EQUIVS_CONTROL_FILE = """
Section: misc
Priority: optional
Standards-Version: 3.9.2

Package: seine-ansible
Depends: ansible, python3-apt
Architecture: all
Description: dependencies for seine
"""
