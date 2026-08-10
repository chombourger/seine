# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import subprocess

from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine
from seine.utils     import apt_sources
from seine.utils     import locked

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

class BuilderImage(Bootstrap):
    def create(self, hostBootstrap):
        return self.build(BUILDER_IMAGE_SCRIPT.format(
            hostBootstrap.name,
            self.distro["source"],
            self.distro["release"],
            self._sources(),
            "apt-{}".format(self.distro["release"]),
            REPOSITORY), base=hostBootstrap.name)

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
    def exec(self, args, architecture=None, volumes=None, workdir=None,
             environment=None, check=True):
        return ContainerEngine.run(
            self._args(args, architecture, volumes, workdir, environment),
            check=check)

    # As exec(), but returns what the command printed. Used for the small
    # questions only the source tree can answer, such as the date its
    # changelog was last touched.
    def output(self, args, architecture=None, volumes=None, workdir=None,
               environment=None):
        return ContainerEngine.check_output(
            self._args(args, architecture, volumes, workdir, environment))

    def _args(self, args, architecture, volumes, workdir, environment):
        cmd = ["container", "run", "--rm"] + SBUILD_RUN_OPTIONS
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
    @property
    def inputs(self):
        return "%s.inputs" % self.path

    def current(self, digest):
        if self.exists() == False or os.path.isfile(self.inputs) == False:
            return False
        with open(self.inputs, "r") as f:
            return f.read().strip() == digest

    def create(self, builderImage):
        # Another build may be making the same chroot: they want the same
        # bytes, so one makes it and the other finds it made rather than
        # both writing one tarball.
        with locked(self.path):
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
            "--customize-hook=sync-out /var/cache/apt/archives /var/cache/mmdebstrap",
            self.distro["release"],
            "/root/.cache/sbuild/%s" % self.filename,
        ] + apt_sources(self.distro)
        digest = hashlib.sha256(" ".join(args).encode()).hexdigest()[:16]
        if self.current(digest):
            return self

        volumes = [(ContainerEngine.downloads(self.distro["release"]),
                    "/var/cache/mmdebstrap")]
        try:
            builderImage.exec(args, self.architecture, volumes=volumes)
        except subprocess.CalledProcessError:
            # A half-written tarball would be picked up as a usable chroot
            # by the next build.
            for path in [self.path, self.inputs]:
                if os.path.isfile(path):
                    os.unlink(path)
            raise
        with open(self.inputs, "w") as f:
            f.write("%s\n" % digest)
        return self

BUILDER_IMAGE_SCRIPT = """
FROM {0}
RUN --mount=type=cache,target=/var/cache/apt/archives,id={4},sharing=locked \
     apt-get update -qqy &&                       \
     apt-get install -qqy --no-install-recommends \
         sbuild mmdebstrap uidmap zstd            \
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
