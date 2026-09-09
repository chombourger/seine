# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import subprocess

from seine.bootstrap   import Bootstrap
from seine.cache_index import CHROOT, Index, say, since
from seine.oci_bundle  import import_bundled
from seine.container import ContainerEngine
from seine.utils     import apt_sources
from seine.utils     import apt_sources_dockerfile
from seine.utils     import APT_CLEANUP
from seine.utils     import feeds
from seine.utils     import locked
from seine.utils     import offline_suites
from seine.utils     import vendor_mountpoint
from seine.utils     import BUILDER_KIND
from seine.utils     import HOST_ARCH
from seine.utils     import PRIVILEGED_RUN_OPTIONS

# Path of the rebuilt-package repository, same in the builder container
# and (via sbuild's bind mount) inside the chroot -- one sources.list
# entry works in both places.
REPOSITORY = "/packages"

# Where a build writes its output. Builds share one repository, so each
# gets its own output dir instead; the publish step moves files across.
OUTPUT = "/output"

class BuilderImage(Bootstrap):
    kind = BUILDER_KIND

    def create(self, hostBootstrap):
        return self.build(self.dockerfile(hostBootstrap), base=hostBootstrap.name)

    # Split out so a caller can inspect/digest the dockerfile without a
    # podman to build it. The image name alone does not capture what
    # _sources() bakes in, so this is also used to detect feed collisions.
    def dockerfile(self, hostBootstrap):
        return BUILDER_IMAGE_SCRIPT.format(
            hostBootstrap.name,
            self.distro["source"],
            self.distro["release"],
            self._sources(),
            "apt-{}".format(self.distro["release"]),
            REPOSITORY,
            APT_CLEANUP)

    # Same feeds the image itself uses, plus deb-src for 'apt-get source'.
    # Offline suites are skipped: their vendor repo is refreshed between
    # builds, so packages.py's fetch() adds that line at exec time instead
    # of baking a path that would go stale.
    def _sources(self):
        offline = set(offline_suites(self.distro))
        online = [feed for feed in feeds(self.distro)
                 if feed["suite"] not in offline]
        return apt_sources_dockerfile(self.distro, online, sources=True)

    # Host arch, not the target's -- named so storage from another host
    # misses and rebuilds native instead of running sbuild's unshare()
    # under emulation, where it fails.
    def defaultName(self):
        return os.path.join("builder", self.distro["source"],
                            "%s-%s" % (self.distro["release"], HOST_ARCH))

    # Runs 'args' in a throwaway builder container with the namespace
    # privileges sbuild needs. Pass 'architecture' to mount that chroot
    # cache where sbuild expects it; 'tty' is for sbuild's own use.
    def exec(self, args, architecture=None, volumes=None, workdir=None,
             environment=None, check=True, tty=False):
        return ContainerEngine.run(
            self._args(args, architecture, volumes, workdir, environment, tty),
            check=check)

    # Like exec(), but returns the command's output.
    def output(self, args, architecture=None, volumes=None, workdir=None,
               environment=None):
        return ContainerEngine.check_output(
            self._args(args, architecture, volumes, workdir, environment, False))

    def _args(self, args, architecture, volumes, workdir, environment, tty):
        cmd = ["container", "run", "--rm"] + PRIVILEGED_RUN_OPTIONS
        if tty:
            cmd += ["-t"]
        if architecture is not None:
            cmd += ["-v", "%s:/root/.cache/sbuild" %
                    ContainerEngine.chroots(self.distro["release"], architecture)]
        for host, container in volumes or []:
            cmd += ["-v", "%s:%s" % (host, container)]
        for name, value in (environment or {}).items():
            cmd += ["-e", "%s=%s" % (name, value)]
        if workdir is not None:
            cmd += ["-w", workdir]
        return cmd + [self.name] + args

# The buildd chroot sbuild unpacks for every package build. Making one is
# a full mmdebstrap run, so it's cached and reused; 'architecture' is the
# chroot's own -- the build arch, not the target's, for a cross build.
class SbuildChroot:
    def __init__(self, distro, options, architecture):
        self.architecture = architecture
        self.distro = distro
        self.options = options

    @property
    def filename(self):
        return "%s-%s.tar.zst" % (self.distro["release"], self.architecture)

    # Name used in the cache index, reports, and eviction.
    @property
    def key(self):
        return "%s-%s" % (self.distro["release"], self.architecture)

    @property
    def path(self):
        return os.path.join(
            ContainerEngine.chroots(self.distro["release"], self.architecture),
            self.filename)

    def exists(self):
        return os.path.isfile(self.path)

    # Records what the tarball was built from, so a stale chroot (feeds
    # changed since) is detected instead of silently reused. Named
    # '<dist>-<arch>.inputs', not '<tarball>.inputs': sbuild's
    # find_tarball() matches any '<dist>-<arch>.t*' file, so
    # 'bookworm-amd64.tar.zst.inputs' would itself look like a chroot and
    # could get picked up instead of the real tarball.
    @property
    def inputs(self):
        return os.path.join(os.path.dirname(self.path), "%s-%s.inputs"
                            % (self.distro["release"], self.architecture))

    # Names an older seine left beside the tarball that still look like a
    # chroot to sbuild.
    @property
    def _mistakable(self):
        return ["%s.inputs" % self.path, "%s.lock" % self.path]

    # Name used while a chroot is being made, then renamed into place
    # atomically -- sbuild can unpack a tarball while it's still being
    # written, so writing in place risks a reader seeing a truncated file.
    # No release/arch in the name, since sbuild matches any '<dist>-<arch>.t*'
    # file; '.tar.zst' stays so mmdebstrap knows the format.
    TEMPORARY = ".seine-new.tar.zst"

    @property
    def temporary(self):
        return os.path.join(os.path.dirname(self.path), SbuildChroot.TEMPORARY)

    def current(self, digest):
        if self.exists() == False or os.path.isfile(self.inputs) == False:
            return False
        with open(self.inputs, "r") as f:
            return f.read().strip() == digest

    # 'offline' bakes sources.list from the local vendor repository instead
    # of the network, for 'apt-pull-mode: offline' rebuilds. Left False by
    # vendor.base_chroot(), which shares this cache entry to compute a
    # vendor's build-dependency closure before the vendor repo exists to
    # read from.
    def create(self, builderImage, offline=False):
        # Lock on the digest file, not the tarball: two concurrent builds
        # of the same chroot should share the result, and locking
        # '<tarball>.lock' would itself look like a chroot tarball to
        # sbuild.
        with locked(self.inputs):
            # Clear stale files an older seine left behind.
            for stale in self._mistakable:
                if os.path.isfile(stale):
                    os.unlink(stale)
            return self._create(builderImage, offline)

    def _create(self, builderImage, offline=False):

        # --mode=root: already root in the container, no need for
        # mmdebstrap to unshare its own namespace. sync-in/sync-out seed
        # apt's archives from the shared download cache and put new
        # downloads back, same as the target bootstrap does.
        args = [
            "mmdebstrap", "--mode=root", "--variant=buildd",
            "--arch=%s" % self.architecture,
            # sbuild re-runs apt-get update in this chroot, which needs
            # ca-certificates to trust an https feed (a snapshot pin).
            "--include=ca-certificates",
            "--setup-hook=mkdir -p \"$1\"/var/cache/apt/archives/",
            "--setup-hook=sync-in /var/cache/mmdebstrap /var/cache/apt/archives/",
            # 'partial' is owned by a chroot-internal user, so copying it
            # out would leave an unremovable dir in the cache; it only
            # holds unfinished downloads anyway.
            "--customize-hook=rm -rf \"$1\"/var/cache/apt/archives/partial",
            "--customize-hook=sync-out /var/cache/apt/archives /var/cache/mmdebstrap",
            self.distro["release"],
            "/root/.cache/sbuild/%s" % self.filename,
        ] + apt_sources(self.distro, offline=offline)
        # Digested before the temp name is swapped in below, so where a
        # chroot is written doesn't affect what it's made from. Offline vs
        # online sources digest differently, so an 'apt-pull-mode' flip
        # is treated as a different chroot.
        digest = hashlib.sha256(" ".join(args).encode()).hexdigest()[:16]
        if self.current(digest) == False:
            import_bundled()
        if self.current(digest):
            entry = Index().hit(CHROOT, self.key)
            say(self.options, "chroot %s reused, made %s"
                              % (self.key, since(entry.get("made"))))
            return self

        args[args.index("/root/.cache/sbuild/%s" % self.filename)] = \
            "/root/.cache/sbuild/%s" % SbuildChroot.TEMPORARY

        volumes = [(ContainerEngine.downloads(self.distro["release"]),
                    "/var/cache/mmdebstrap"),
                   (os.path.dirname(self.path), "/root/.cache/sbuild")]
        if offline:
            from seine import vendor
            volumes += [(vendor.deploy_repository(suite), vendor_mountpoint(suite))
                       for suite in offline_suites(self.distro)]
        try:
            # Not 'architecture=self.architecture': that would mount
            # builderImage's own distro chroot cache, which differs from
            # this chroot's for a vendor resolver -- see
            # VendorResolver.base_chroot().
            builderImage.exec(args, volumes=volumes)
        except subprocess.CalledProcessError:
            # Remove only the failed temp file -- an existing chroot stays
            # valid and matching its inputs.
            if os.path.isfile(self.temporary):
                os.unlink(self.temporary)
            raise
        os.replace(self.temporary, self.path)
        with open(self.inputs, "w") as f:
            f.write("%s\n" % digest)
        Index().made(CHROOT, self.key)
        say(self.options, "chroot %s made" % self.key)
        return self

BUILDER_IMAGE_SCRIPT = """
FROM {0}
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources \
           /etc/apt/sources.list.d/*.list && \
    {3}
RUN --mount=type=cache,target=/var/cache/apt/archives,id={4},sharing=locked \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         sbuild mmdebstrap uidmap zstd apt-utils  \
         dpkg-dev devscripts quilt git            \
         ca-certificates curl iproute2 openssh-client \
         debhelper python3-jinja2 python3-dacite kernel-wedge
# openssh-client: git needs it for a ';protocol=ssh' source (only a
# Recommends of git, not installed above). The last four are for kernel
# rebuilds -- jinja2/kernel-wedge/dh_listpackages back the kernel's own
# debian/control generator, dacite reads defines.toml (6.12 sources only).
# iproute2: sbuild needs 'ip link set lo up' and dies without it.
RUN {6}
RUN echo 'root:1:65535' > /etc/subuid && \
    echo 'root:1:65535' > /etc/subgid
# sbuild's chroot is a separate root our bind mounts don't reach, so
# sbuild bind-mounts the package repository into it at the same path too
# -- a package can then build against ones rebuilt before it via a plain
# sources.list entry. Trailing '1;' is required: it's a perl config file.
RUN mkdir -p /etc/sbuild && \
    echo '$unshare_bind_mounts = [ {{ directory => "{5}", mountpoint => "{5}" }} ];' \
        > /etc/sbuild/sbuild.conf && \
    echo '1;' >> /etc/sbuild/sbuild.conf
"""
