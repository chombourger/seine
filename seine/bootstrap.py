
from abc import ABC, abstractmethod

import os
import subprocess
import tempfile

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
        dockerfile = tempfile.NamedTemporaryFile(delete=False)
        dockerfile.write(str.encode("""
            FROM {0}:{1} AS base
            RUN                                                       \
                 apt-get update -qqy &&                               \
                 apt-get install -qqy debootstrap qemu-user-static && \
                 apt-get clean -qqy
            FROM base AS clean-base
            RUN rm -rf /usr/share/doc /usr/share/info /usr/share/man
        """
        .format(self.distro["source"], self.distro["release"])))
        dockerfile.close()

        try:
            subprocess.run([
                "podman", "build", "--rm", "--squash",
                "-t", self.name, "-f", dockerfile.name],
                check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            os.unlink(dockerfile.name)
        return self

    def defaultName(self):
        return os.path.join("seine", "bootstrap", self.distro["source"], self.distro["release"], "all")

class TargetBootstrap(Bootstrap):
    def create(self, hostBootstrap):
        self.hostBootstrap = hostBootstrap
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {0} AS bootstrap
            RUN                                           \
                export container=lxc;                     \
                debootstrap --arch {1} {2} rootfs {3} &&  \
                cp /usr/bin/qemu-*-static rootfs/usr/bin/
            FROM scratch AS base
            COPY --from=bootstrap rootfs/ /
            FROM base AS ansible
            RUN apt-get update -qqy &&          \
                apt-get install -qqy ansible && \
                apt-get clean -qqy
            FROM base AS clean-base
            RUN rm -rf rootfs /usr/share/doc /usr/share/info /usr/share/man
            FROM ansible AS final
        """
        .format(
            self.hostBootstrap.name,
            self.distro["architecture"],
            self.distro["release"],
            self.distro["uri"]
        ))
        dockerfile.close()

        try:
            subprocess.run([
                "podman", "build", "--rm",
                "-t", self.name,
                "-f", dockerfile.name], check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            os.unlink(dockerfile.name)
        return self

    def defaultName(self):
        return os.path.join("seine", "bootstrap", self.distro["source"], self.distro["release"], self.distro["architecture"])


