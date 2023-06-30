# seine - Singular Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import grp
import os
import subprocess
import sys
import tempfile

from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine

class Qemu(Bootstrap):
    PACKAGES = [
        "qemu-system-x86"
    ]

    def __init__(self, source):
        self.source = source
        self.debug = source.options["debug"]
        self.keep = source.options["keep"]
        self.verbose = source.options["verbose"]
        super().__init__(source.spec["distribution"], source.options)

    def _unlink(self, path, descr):
        if self.keep:
            print("keeping '%s' (%s) as requested" % (path, descr))
        else:
            os.unlink(path)

    def container_id(self):
        return self.image_id().replace("/", "-")

    def image_id(self):
        return self.name

    def defaultName(self):
        return os.path.join("qemu", self.distro["source"], self.distro["release"], "all")

    def create(self):
        if ContainerEngine.hasImage(self.image_id()) is True:
            return

        print("Preparing qemu image...")
        hostBootstrap = self.source.hostBootstrap
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {} AS image
            RUN                            \
                apt-get update -qqy &&     \
                apt-get install -qqy {} && \
                apt-get clean &&           \
                rm -rf /usr/share/info &&  \
                rm -rf /usr/share/man
            CMD /bin/true
        """
        .format(
            hostBootstrap.name,
            " ".join(Qemu.PACKAGES)
        ))
        dockerfile.close()

        imageCreated = False
        try:
            ContainerEngine.run([
                "build", "--rm", "--squash",
                "-t", self.image_id(),
                "-f", dockerfile.name], check=True)
            imageCreated = True
            ContainerEngine.run([
                "container", "create",
                "--name", self.container_id(), self.image_id()], check=True)
        except subprocess.CalledProcessError:
            if imageCreated is True:
                ContainerEngine.run(["image", "rm", self.image_id()], check=False)
            raise
        finally:
            self._unlink(dockerfile.name, "dockerfile for the qemu image")
