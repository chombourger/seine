# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

from abc import ABC, abstractmethod

import hashlib
import os
import tempfile

from seine.cache_index import IMAGE, Index, say, since
from seine.tasks import Task
from seine.utils import ContainerEngine
from seine.utils import INPUTS_LABEL
from seine.utils import KIND_LABEL
from seine.utils import ROOTFS_KIND
from seine.utils import apt_sources
from seine.utils import locked
from seine.utils import TOOLING_KIND

class Bootstrap(ABC):
    # What this image is, as the label every image carries. Said by each
    # class rather than left to be inherited: podman hands an image the
    # labels of the one it was built FROM, so an image that did not say
    # would answer with its base's answer.
    kind = TOOLING_KIND

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
    # it now, and what the image it is built FROM was built from. Recorded on
    # the image as a label, so an image is rebuilt when either has changed.
    #
    # Without it an image is only ever matched by name, and every image
    # derived from another goes stale the moment that one is rebuilt --
    # silently, since a stale image is a working image, just not one built
    # from what the specification now says. An image built by a seine that
    # did not label them has no label, and is rebuilt once.
    #
    # What the base was built from, rather than which bytes it came out as:
    # its own label if it has one, and only otherwise its id. The two answer
    # the same question on one machine and different questions across two,
    # since two machines bootstrapping the same root file-system from the
    # same specification do not produce the same bytes -- so keying on the
    # id made every image standing on a bootstrapped one machine-specific.
    # Keying on the label is what seine does everywhere else: a package's
    # stamp and a chroot's digest say what went in, not what came out.
    #
    # An image pulled from a registry has no label of ours, and its id is
    # already the same on both machines, so that is what is used for those.
    def digest(self, dockerfile, base=None):
        digest = hashlib.sha256()
        digest.update(dockerfile.encode())
        if base is not None:
            inputs = ContainerEngine.imageLabel(base, INPUTS_LABEL) \
                     or ContainerEngine.imageId(base) or ""
            digest.update(inputs.encode())
        return digest.hexdigest()[:16]

    def current(self, dockerfile, base=None):
        return ContainerEngine.imageLabel(self.name, INPUTS_LABEL) \
               == self.digest(dockerfile, base)

    # Builds the image from 'dockerfile' unless one built from the same
    # inputs is already there.
    def build(self, dockerfile, base=None, options=None):
        if self.current(dockerfile, base):
            entry = Index().hit(IMAGE, self.name)
            say(self.options, "image %s reused, made %s"
                              % (self.name, since(entry.get("made"))))
            return self

        written = tempfile.NamedTemporaryFile(mode="w", delete=False)
        written.write(dockerfile)
        written.close()
        # One build at a time of one image, and as many different images at
        # once as there are steps wanting them.
        #
        # The storage is held shared, which keeps out what sweeps it: a
        # prune, or a 'seine cache clear' typed in another terminal, is
        # free to take away the untagged intermediates this is standing on
        # while it works. Held here rather than only by 'seine build',
        # since an image can be asked for through the API as well.
        #
        # The image's own name is what two builds of the same image queue
        # on. One lock for the whole storage was what this was, and it made
        # every image on a machine wait for every other.
        try:
            with locked(ContainerEngine.storage_lock(), shared=True), \
                 locked(os.path.join(ContainerEngine.root(), "images.d",
                                     self.name)):
                ContainerEngine.run(
                    ["build", "--rm"] + (options or []) +
                    ["--label", "%s=%s" % (INPUTS_LABEL,
                                           self.digest(dockerfile, base)),
                     "--label", "%s=%s" % (KIND_LABEL, self.kind),
                     "-t", self.name, "-f", written.name], check=True)
        finally:
            if self.options.get("keep"):
                print("keeping '%s' (dockerfile for %s) as requested"
                      % (written.name, self.name))
            else:
                os.unlink(written.name)
        Index().made(IMAGE, self.name)
        say(self.options, "image %s made" % self.name)
        return self

    def getName(self):
        if self._name is None:
            self._name = self.defaultName()
        return self._name

    def setName(self, name):
        self._name = name

    name = property(getName, setName)

class HostBootstrap(Bootstrap):
    # The one step everything else waits for: the image every container
    # seine builds is made from.
    def task(self):
        return Task("bootstrap-host", self.create)

    def create(self):
        return self.build(HOST_BOOTSTRAP_SCRIPT.format(
            self.distro["source"],
            self.distro["release"],
            "apt-{}".format(self.distro["release"])), options=["--squash"])

    def defaultName(self):
        return os.path.join("bootstrap", self.distro["source"], self.distro["release"], "all")

class TargetBootstrap(Bootstrap):
    # The root file-system itself, which is what an export leaves behind.
    kind = ROOTFS_KIND

    # The root file-system is assembled in this one, and the imager's own
    # kernel is fetched through it -- so it is needed even when the
    # specification names a 'baseline' of its own.
    def task(self, hostBootstrap):
        return Task("bootstrap-target",
                    lambda: self.create(hostBootstrap),
                    needs=["bootstrap-host"])

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
