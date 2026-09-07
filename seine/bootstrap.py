# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

from abc import ABC, abstractmethod

import hashlib
import os
import tempfile

from seine.cache_index import IMAGE, Index, say, since
from seine.oci_bundle import import_bundled
from seine.tasks import Task
from seine.container import ContainerEngine
from seine.utils import HOST_ARCH
from seine.utils import INPUTS_LABEL
from seine.utils import KIND_LABEL
from seine.utils import ROOTFS_KIND
from seine.utils import apt_sources
from seine.utils import apt_sources_dockerfile
from seine.utils import APT_CLEANUP
from seine.utils import base_feed
from seine.utils import feed_digest
from seine.utils import locked
from seine.utils import TOOLING_KIND
from seine.utils import vendor_mountpoint

class Bootstrap(ABC):
    # Each subclass sets its own 'kind' label rather than inheriting it:
    # podman copies labels from the base image, so an unset kind would
    # leak the base's kind instead.
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

    # Hashes the Dockerfile plus the base image's own inputs digest (or its
    # id, if it has none), stored as a label so a rebuild is triggered when
    # either changes. Digest, not id, for the base: two machines bootstrap
    # the same spec into different bytes, so id would be machine-specific.
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

    # Builds 'dockerfile' unless an image with matching inputs already
    # exists, checking first for one bundled by a 'seine-oci-<hostarch>'
    # package under /usr/share/seine/oci.
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
        # Storage lock (shared) keeps a concurrent prune/cache-clear from
        # sweeping this build's intermediates; the per-name lock only
        # serializes builds of the same image, not all images.
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
    # 'vendor_digest': folded into the Dockerfile so a vendor refresh
    # invalidates the cached image; None outside offline builds.
    # 'force_online': set by vendor.py's own fetch pipeline, which must
    # build this image without going through the offline vendor path it
    # exists to fill. Gets its own cache tag (see defaultName()) so it
    # never collides with a plain offline HostBootstrap.
    def __init__(self, distro, options, vendor_digest=None, host_architecture=None,
                force_online=False):
        self.vendor_digest = vendor_digest
        self.host_architecture = host_architecture or HOST_ARCH
        self.force_online = force_online
        super().__init__(distro, options)

    # The base image every seine container is built from. 'needs' lets a
    # caller order this after 'vendor' when going offline.
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

    # base_feed() alone: a second feed would only cost this image its
    # sharing with specs that differ there, and nothing here needs
    # backports or -security anyway.
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

    # Bootstrapped from base_feed() alone, same sharing reasoning as
    # HostBootstrap._sources(); the rest of the feeds are applied later
    # by AnsibleContainerRunner._configure_feeds().
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

# Foreign-ISA qemu-user-static interpreters this HOST_ARCH needs to
# cross-bootstrap other architectures. Native CPU compat (amd64 running
# i386, arm64 running armhf) needs none -- but that compat is real
# silicon only, so an emulated host (built via --platform) needs its
# compat architecture's interpreter fetched too, hence the *_EMULATED
# table below.
QEMU_ARCHS = {
    "amd64": ["aarch64", "arm"],
    "arm64": ["x86_64", "i386"],
}
QEMU_ARCHS_EMULATED = {
    "amd64": ["aarch64", "arm", "i386"],
    "arm64": ["x86_64", "i386", "arm"],
}

# Downloads (never installs) both 'qemu-user' and 'qemu-user-static'
# .debs and extracts them into one tree, since which package holds the
# real static binaries vs. just symlinks differs by release. 'true' when
# there is nothing to cross-bootstrap, so an unlisted HOST_ARCH still
# builds rather than failing.
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
RUN {7}
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
RUN  apt-get clean -qqy
"""
