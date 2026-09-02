# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

from abc import ABC, abstractmethod

import hashlib
import os
import tempfile

from seine.cache_index import IMAGE, Index, say, since
from seine.oci_bundle import import_bundled
from seine.tasks import Task
from seine.utils import ContainerEngine
from seine.utils import HOST_ARCH
from seine.utils import INPUTS_LABEL
from seine.utils import KIND_LABEL
from seine.utils import ROOTFS_KIND
from seine.utils import apt_sources
from seine.utils import apt_sources_dockerfile
from seine.utils import base_feed
from seine.utils import feed_digest
from seine.utils import locked
from seine.utils import TOOLING_KIND
from seine.utils import vendor_mountpoint

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
    # inputs is already there -- or a 'seine-oci-<hostarch>' package left
    # one under /usr/share/seine/oci with the same inputs, which is worth
    # a look before building it here. A bundle whose inputs no longer
    # match (an archive that moved since it was packaged) leaves 'current'
    # false, and this builds it exactly as if there had been no bundle.
    def build(self, dockerfile, base=None, options=None):
        if self.current(dockerfile, base) == False:
            import_bundled()
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
    # 'vendor_digest' is offline_dockerfile_digest()'s return: folded
    # into the Dockerfile text so a vendor refresh invalidates the
    # cached image (see create()'s own comment). None when the caller
    # never computed one -- every test construction, and anything not
    # going offline, which never reads it.
    # 'force_online' is vendor.py's own resolve/fetch pipeline: it builds
    # this same image before it can do anything, so it must never itself
    # go looking for a repository it exists to fill. Named apart from a
    # plain HostBootstrap (defaultName() below) so the two never thrash
    # one another's cached tag when 'apt-pull-mode: offline' makes them
    # genuinely different images.
    def __init__(self, distro, options, vendor_digest=None, host_architecture=None,
                force_online=False):
        self.vendor_digest = vendor_digest
        self.host_architecture = host_architecture or HOST_ARCH
        self.force_online = force_online
        super().__init__(distro, options)

    # The one step everything else waits for: the image every container
    # seine builds is made from. 'needs' lets a caller make this wait on
    # 'vendor' first -- see Image.shared_tasks()'s own comment on why
    # that edge has to point this way round when going offline.
    def task(self, needs=None):
        return Task("bootstrap-host", self.create, needs=needs)

    def _offline(self):
        return False if self.force_online else \
               self.distro.get("apt-pull-mode") == "offline"

    def create(self):
        build_options = ["--squash"]
        emulated = self.host_architecture != HOST_ARCH
        if emulated:
            build_options += ["--platform", "linux/%s" % self.host_architecture]
        mount = ""
        digest_comment = ""
        if self._offline():
            from seine import vendor
            release = self.distro["release"]
            where = vendor.offline_build_context(release)
            build_options += ["--build-context",
                              "%s=%s" % (vendor.BUILD_CONTEXT, where)]
            mount = "--mount=type=bind,from=%s,target=%s,ro" % (
                vendor.BUILD_CONTEXT, vendor_mountpoint(release))
            digest_comment = "# vendor digest: %s" % self.vendor_digest
        return self.build(HOST_BOOTSTRAP_SCRIPT.format(
            self.distro["source"],
            self.distro["release"],
            "apt-{}".format(self.distro["release"]),
            self._sources(),
            mount,
            digest_comment,
            _qemu_fetch(self.host_architecture, emulated),
            APT_CLEANUP), options=build_options)

    # base_feed() alone, the same reasoning as TargetBootstrap's own
    # dockerfile(): these packages need nothing from backports or
    # -security, and a second feed would only cost this image its
    # sharing with specifications that differ there.
    def _sources(self):
        return apt_sources_dockerfile(self.distro, [base_feed(self.distro)],
                                      offline=self._offline())

    def defaultName(self):
        return os.path.join("bootstrap", self.distro["source"], self.distro["release"],
                            "vendor" if self.force_online else "all")

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

    # Bootstrapped from base_feed() alone, not every feed listed: a second
    # feed costs this image its sharing with every specification that
    # differs only there. Applied later, to the running container, by
    # AnsibleContainerRunner._configure_feeds().
    def create(self, hostBootstrap):
        self.hostBootstrap = hostBootstrap
        return self.build(self.dockerfile(), base=self.hostBootstrap.name)

    # Split out from create() so a test can read what this would bootstrap
    # from without a podman to build it.
    def dockerfile(self):
        return TARGET_BOOTSTRAP_SCRIPT.format(
            self.hostBootstrap.name,
            self.distro["architecture"],
            self.distro["release"],
            " ".join("'%s'" % source for source in
                     apt_sources(self.distro, entries=[base_feed(self.distro)])),
            "mmdebstrap-{}".format(self.distro["release"]))

    def defaultName(self):
        return os.path.join(
                "bootstrap",
                self.distro["source"],
                self.distro["release"],
                self.distro["architecture"],
                feed_digest(self.distro))

# Which foreign-ISA qemu-user-static interpreter this host needs to
# cross-bootstrap the other supported architectures -- native CPU compat
# (amd64 running i386, arm64 running armhf) needs none, confirmed
# deliberately rather than assumed (see packages.py's own SCOPES comment
# on the same pairing). Keyed by HOST_ARCH since it is this machine's own
# architecture, not the target's, that decides which interpreters a
# cross bootstrap running on it will ever call for.
#
# That compat pairing is real silicon only: qemu-user ships each ISA as
# its own binary (qemu-arm vs qemu-aarch64, qemu-i386 vs qemu-x86_64), so
# an EMULATED host (built via --platform, not this machine's own arch)
# gets none of its compat architecture's native support either and needs
# that interpreter fetched too.
QEMU_ARCHS = {
    "amd64": ["aarch64", "arm"],
    "arm64": ["x86_64", "i386"],
}
QEMU_ARCHS_EMULATED = {
    "amd64": ["aarch64", "arm", "i386"],
    "arm64": ["x86_64", "i386", "arm"],
}

# 'qemu-user'/'qemu-user-static' installed together are ~465MiB, covering
# every architecture QEMU supports; this host ever needs at most two. The
# split differs by release -- trixie's 'qemu-user-static' is only
# compatibility symlinks into 'qemu-user', bookworm's is the real static
# binaries -- so both names are downloaded (never installed) and
# extracted into the same tree, and whichever one actually holds the
# bytes resolves the symlinks either way, without asking which release
# this is. 'true' for an architecture with nothing to cross-bootstrap
# (there is none today, but an unlisted HOST_ARCH should build a host
# bootstrap with no interpreters rather than fail one).
def _qemu_fetch(architecture, emulated=False):
    table = QEMU_ARCHS_EMULATED if emulated else QEMU_ARCHS
    archs = table.get(architecture, [])
    if len(archs) == 0:
        return "true"
    wanted = " ".join("/qemu-extract/usr/bin/qemu-%s-static" % a for a in archs)
    return (
        "mkdir -p /qemu-extract && cd /qemu-extract && "
        "(apt-get download qemu-user-static qemu-user || true) && "
        "for deb in *.deb; do dpkg -x \"$deb\" .; done && "
        "cp -L %s /usr/bin/ && "
        "cd / && rm -rf /qemu-extract"
    ) % wanted

HOST_BOOTSTRAP_SCRIPT = """
FROM {0}:{1} AS base
{5}
RUN --mount=type=cache,target=/var/cache/apt/archives,id={2},sharing=locked {4} \
     rm -f /etc/apt/apt.conf.d/docker-clean &&    \
     rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources \
           /etc/apt/sources.list.d/*.list &&      \
     {3} &&                                       \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         arch-test debian-archive-keyring gpg mmdebstrap && \
     {6}
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
        --customize-hook='rm -rf "$1"/var/cache/apt/archives/partial' \
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
