# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import subprocess

from seine.bootstrap   import Bootstrap
from seine.cache_index import CHROOT, Index, say, since
from seine.utils     import ContainerEngine
from seine.utils     import apt_sources
from seine.utils     import locked
from seine.utils     import BUILDER_KIND

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

# Where the repository of rebuilt packages is mounted, both in the builder
# container and -- through sbuild's own bind mount -- inside the chroot it
# builds in. The same path in both, so one sources.list entry works in
# either place.
REPOSITORY = "/packages"

# Where a build drops what it produced, which is not the repository.
#
# Builds run beside each other, into a repository they share, so "what did
# this build write" cannot be answered by looking at what appeared there
# while it ran -- what appeared there is also whatever else was building.
# Each build writes into a directory of its own and the step that
# publishes moves them across, which answers it by construction.
OUTPUT = "/output"

class BuilderImage(Bootstrap):
    # Where packages are built, holding the buildd chroot: derived from
    # the host bootstrap and worth carrying.
    kind = BUILDER_KIND

    def create(self, hostBootstrap):
        return self.build(self.dockerfile(hostBootstrap), base=hostBootstrap.name)

    # Split out from create() so a caller can ask what this would build
    # from -- and digest() it -- without a podman to build it. Unlike
    # TargetBootstrap's own dockerfile(), this image's name does not cover
    # what _sources() bakes in: two specifications sharing a release can
    # still collide here if their feeds differ, which is what such a
    # caller is checking for.
    def dockerfile(self, hostBootstrap):
        return BUILDER_IMAGE_SCRIPT.format(
            hostBootstrap.name,
            self.distro["source"],
            self.distro["release"],
            self._sources(),
            "apt-{}".format(self.distro["release"]),
            REPOSITORY)

    # The builder installs build dependencies and fetches sources from the
    # same places the image itself is built from, with deb-src alongside so
    # 'apt-get source' has somewhere to look. Anything else rebuilds a
    # package from a different version than the one the image would have
    # installed -- which for the security suite means rebuilding a source
    # that is missing the fixes apt would otherwise have given it.
    def _sources(self):
        sources = apt_sources(self.distro, sources=True)
        return " && ".join(
            "echo '%s' >> /etc/apt/sources.list.d/seine.list" % s for s in sources)

    # No architecture in the name: this image is always of the host's own
    # architecture, and it is the chroot inside it that carries the target's.
    def defaultName(self):
        return os.path.join("builder", self.distro["source"], self.distro["release"])

    # Runs 'args' inside a throwaway builder container with the namespace
    # privileges sbuild needs. 'volumes' is a list of (host, container)
    # pairs. Passing an 'architecture' mounts that architecture's chroot
    # cache where sbuild looks for its tarballs by default, so neither
    # mmdebstrap nor sbuild needs telling where the chroot lives; steps
    # that do not enter a chroot at all leave it out.
    #
    # 'tty' allocates one inside the container, for sbuild alone --
    # see packages.py's own build() for why.
    def exec(self, args, architecture=None, volumes=None, workdir=None,
             environment=None, check=True, tty=False):
        return ContainerEngine.run(
            self._args(args, architecture, volumes, workdir, environment, tty),
            check=check)

    # As exec(), but returns what the command printed. Used for the small
    # questions only the source tree can answer, such as the date its
    # changelog was last touched.
    def output(self, args, architecture=None, volumes=None, workdir=None,
               environment=None):
        return ContainerEngine.check_output(
            self._args(args, architecture, volumes, workdir, environment, False))

    def _args(self, args, architecture, volumes, workdir, environment, tty):
        cmd = ["container", "run", "--rm"] + SBUILD_RUN_OPTIONS
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

    # What this chroot is called in the cache index, which is what a report
    # of it says and what an eviction would name.
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

    # The tarball keeps the name sbuild looks for, so what it was made from
    # is recorded beside it instead -- the same idea as a package's stamp.
    # Without it a chroot made from one set of feeds goes on being used
    # after the specification has changed them, and packages are built
    # against an archive the image no longer has.
    #
    # Not '<tarball>.inputs', which is what this was: sbuild takes every
    # file in its cache directory matching '<dist>-<arch>.t<anything>' for a
    # chroot tarball, and 'bookworm-amd64.tar.zst.inputs' matches as surely
    # as 'bookworm-amd64.tar.zst' does. Its find_tarball() keeps the last
    # one readdir hands it, so which of the two a build unpacks is decided
    # by the order of a directory -- and the build that gets this one dies
    # with 'tar: This does not look like a tar archive'.
    @property
    def inputs(self):
        return os.path.join(os.path.dirname(self.path), "%s-%s.inputs"
                            % (self.distro["release"], self.architecture))

    # The names an earlier seine put beside the tarball, both of which still
    # look like a chroot to sbuild and are still sitting in caches.
    @property
    def _mistakable(self):
        return ["%s.inputs" % self.path, "%s.lock" % self.path]

    # What a chroot is called while it is being made. sbuild unpacks the
    # tarball without taking the lock that making one takes, so writing
    # onto it in place gave a reader a zstd stream that stopped in the
    # middle. The rename below is atomic and costs no lock; holding one
    # over the unpacking would serialise the package builds instead.
    #
    # No release or architecture in the name: sbuild takes every file
    # matching '<dist>-<arch>.t<anything>' for a chroot. The '.tar.zst'
    # stays, it is what tells mmdebstrap the format.
    TEMPORARY = ".seine-new.tar.zst"

    @property
    def temporary(self):
        return os.path.join(os.path.dirname(self.path), SbuildChroot.TEMPORARY)

    def current(self, digest):
        if self.exists() == False or os.path.isfile(self.inputs) == False:
            return False
        with open(self.inputs, "r") as f:
            return f.read().strip() == digest

    def create(self, builderImage):
        # Another build may be making the same chroot: they want the same
        # bytes, so one makes it and the other finds it made rather than
        # both writing one tarball.
        #
        # The lock is taken on the digest's name rather than the tarball's,
        # for the same reason the digest is not called '<tarball>.inputs':
        # '<tarball>.lock' looks like a chroot tarball to sbuild too, and
        # bookworm's sbuild does not skip the empty file it is.
        with locked(self.inputs):
            # A cache made by an earlier seine has both of these in it, and
            # leaving them there is leaving a build to fail on a coin toss.
            for stale in self._mistakable:
                if os.path.isfile(stale):
                    os.unlink(stale)
            return self._create(builderImage)

    def _create(self, builderImage):

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
            # apt's staging directory belongs to a user of the chroot's
            # own, so copying it out puts a directory in the cache that
            # this user cannot unlink -- which is what stopped 'seine
            # cache clear' from emptying the downloads. Nothing wants it:
            # what is in it is a download that did not finish.
            "--customize-hook=rm -rf \"$1\"/var/cache/apt/archives/partial",
            "--customize-hook=sync-out /var/cache/apt/archives /var/cache/mmdebstrap",
            self.distro["release"],
            "/root/.cache/sbuild/%s" % self.filename,
        ] + apt_sources(self.distro)
        # Digested before the name is swapped in below: where a chroot is
        # written is not what it is made from, and caches made before this
        # still match.
        digest = hashlib.sha256(" ".join(args).encode()).hexdigest()[:16]
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
        try:
            # Not 'architecture=self.architecture': that mounts the chroot
            # cache directory of 'builderImage's own distro, which for a
            # vendor's resolver differs from this chroot's -- see
            # VendorResolver.base_chroot().
            builderImage.exec(args, volumes=volumes)
        except subprocess.CalledProcessError:
            # Only what this run wrote: the chroot that was there is whole
            # and still matches its inputs, where removing the pair made
            # every build remake a chroot because one of them failed.
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
RUN --mount=type=cache,target=/var/cache/apt/archives,id={4},sharing=locked \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         sbuild mmdebstrap uidmap zstd apt-utils  \
         dpkg-dev devscripts quilt git            \
         ca-certificates curl iproute2 openssh-client \
         debhelper python3-jinja2 python3-dacite kernel-wedge
# openssh-client is what git needs to clone a ';protocol=ssh' source. It
# is only a Recommends of git, and recommends are not installed here.
# The last four are for kernel rebuilds: regenerating debian/control
# after restricting the flavours runs the kernel's own generator, which
# wants jinja2, kernel-wedge and dh_listpackages behind them. None of
# them fails in a way that names what is missing.
#
# dacite is what reads debian/config/*/defines.toml into the generator's
# dataclasses, so it is needed by a 6.12 source and not by a 6.1 one --
# and a specification may well build both.
# iproute2 is not optional: sbuild brings the loopback interface up with
# 'ip link set lo up' when it takes the network away from the build, and
# dies rather than warns when 'ip' is missing.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources \
           /etc/apt/sources.list.d/*.list && \
    {3} && \
    apt-get update -qqy
RUN echo 'root:1:65535' > /etc/subuid && \
    echo 'root:1:65535' > /etc/subgid
# The chroot sbuild builds in is a root of its own that our bind mounts do
# not reach into, so the repository of rebuilt packages is bind-mounted a
# second time, by sbuild, at the same path -- letting a package build
# against one rebuilt before it through an ordinary sources.list entry.
# The trailing 1 is what a perl configuration file has to evaluate to.
RUN mkdir -p /etc/sbuild && \
    echo '$unshare_bind_mounts = [ {{ directory => "{5}", mountpoint => "{5}" }} ];' \
        > /etc/sbuild/sbuild.conf && \
    echo '1;' >> /etc/sbuild/sbuild.conf
"""
