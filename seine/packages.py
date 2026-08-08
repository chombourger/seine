# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import shlex
import shutil
import tempfile
import time

from seine.sbuild import BuilderImage
from seine.sbuild import REPOSITORY
from seine.sbuild import SbuildChroot
from seine.utils  import ContainerEngine
from seine.utils  import HOST_ARCH

# A source package to rebuild, as described by one entry of the spec's
# 'packages' section. Where the source comes from is given as a URI:
#
#   apt://busybox[=1:1.37.0-6]     the distribution's own source package
#   https://.../busybox_1.dsc      a source package published elsewhere
#   git://host/busybox.git;rev=..  a tree carrying its own debian/ directory
#
# Only .dsc files are accepted over https: a plain upstream tarball has no
# debian/ directory and so cannot be built, and pairing one with packaging
# taken from somewhere else is a second source this does not model yet.
SCHEMES = ["apt", "git", "https"]

class Package:
    def __init__(self, spec, index):
        self.index = index
        if type(spec) != type({}):
            raise ValueError("package #%d is not a dictionary!" % index)
        if "source" not in spec:
            raise ValueError("package #%d has no 'source' specified!" % index)

        self.spec = spec
        self.dirname = spec.get("_dirname", "")
        self.priority = spec.get("priority", 500)

        self._parse_source(spec["source"])
        self.cross = self._parse_bool(spec, "cross")
        self.options = self._parse_list(spec, "options")
        self.patches = self._parse_list(spec, "patches")
        self.profiles = self._parse_list(spec, "profiles")
        self.source_date_epoch = self._parse_epoch(spec)

    def _error(self, message):
        return ValueError("package #%d ('%s'): %s" % (self.index, self.source, message))

    def _parse_source(self, source):
        self.source = source
        if type(source) != type(""):
            raise ValueError("package #%d has a non-string 'source'!" % self.index)
        if "://" not in source:
            raise ValueError(
                "package #%d ('%s') has no URI scheme: expected one of %s"
                % (self.index, source, ", ".join("%s://" % s for s in SCHEMES)))

        self.scheme, rest = source.split("://", 1)
        if self.scheme not in SCHEMES:
            raise self._error("unsupported URI scheme '%s://', expected one of %s"
                % (self.scheme, ", ".join("%s://" % s for s in SCHEMES)))
        if len(rest) == 0:
            raise self._error("URI has nothing after its scheme")

        # Defaults for the fields only some of the schemes carry, so callers
        # may read them without caring which scheme they got.
        self.name = None
        self.version = None
        self.parameters = {}

        if self.scheme == "apt":
            self.name, _, self.version = rest.partition("=")
            if len(self.name) == 0:
                raise self._error("no source package name given")
            if self.version == "":
                self.version = None
        elif self.scheme == "https":
            if not rest.endswith(".dsc"):
                raise self._error(
                    "https sources shall point at a .dsc file: an upstream "
                    "tarball carries no debian/ directory to build from")
            self.name = os.path.basename(rest).split("_")[0]
        elif self.scheme == "git":
            # bitbake's notation: location followed by ;key=value pairs.
            location, *parameters = rest.split(";")
            for parameter in parameters:
                key, _, value = parameter.partition("=")
                self.parameters[key] = value
            # A branch name moves, and a build that cannot be repeated is
            # not worth caching, let alone calling reproducible.
            if len(self.parameters.get("rev", "")) == 0:
                raise self._error(
                    "git sources shall be pinned with ';rev=<commit>' so the "
                    "same specification always rebuilds the same source")
            self.name = os.path.basename(location).removesuffix(".git")

    def _parse_bool(self, spec, key):
        value = spec.get(key)
        if value is not None and type(value) != type(True):
            raise self._error("'%s' shall be either true or false" % key)
        return value

    def _parse_list(self, spec, key):
        values = spec.get(key, [])
        if type(values) != type([]):
            raise self._error("'%s' shall be a list" % key)
        for value in values:
            if type(value) != type(""):
                raise self._error("'%s' shall be a list of strings" % key)
        return values

    def _parse_epoch(self, spec):
        value = spec.get("source_date_epoch")
        if value is None:
            return None
        if type(value) != type(0):
            raise self._error("'source_date_epoch' shall be a number of seconds")
        return value

    # Patches are given relative to the YAML file that listed them, which is
    # not necessarily the one being built: specifications are assembled from
    # several files through 'requires'.
    def patch_files(self):
        return [os.path.normpath(os.path.join(self.dirname, p)) for p in self.patches]

# Where the source of a package is fetched and, later, built. Everything
# happens inside the builder container: the tools involved (apt-get source,
# dget, git) are installed there rather than on the machine seine runs on,
# and the working directory is a host-side directory bind-mounted into it so
# the fetched source outlives the container that fetched it.
WORKDIR = "/src"

# Where the "this has been built" markers live, inside the repository so
# they are thrown away with it. Hidden, and not .debs, so neither apt nor
# dpkg-scanpackages pays them any attention.
STAMPS = ".stamps"

class Builder:
    def __init__(self, distro, options, builderImage):
        self.builderImage = builderImage
        self.distro = distro
        self.options = options

    def fetch(self, package, workdir):
        volumes = [(workdir, WORKDIR)]
        self.builderImage.exec(
            self._fetch_args(package), volumes=volumes, workdir=WORKDIR)
        return self._source_dir(package, workdir)

    def _fetch_args(self, package):
        if package.scheme == "apt":
            source = package.name
            if package.version is not None:
                source = "%s=%s" % (package.name, package.version)
            return ["apt-get", "source", source]

        if package.scheme == "https":
            # -u: the .dsc of a rebuilt or third-party package is not
            # necessarily signed by a key we have, and refusing to fetch it
            # on those grounds would make the scheme useless. What it is
            # allowed to do to the image is the specification's call.
            return ["dget", "-u", package.source]

        # git:// says nothing about how to reach the remote; bitbake spells
        # that ';protocol=', and https is the sane default.
        protocol = package.parameters.get("protocol", "https")
        location = package.source.split("://", 1)[1].split(";")[0]
        url = "%s://%s" % (protocol, location)

        args = ["git", "clone"]
        if "branch" in package.parameters:
            args += ["--branch", package.parameters["branch"]]
        args += [url, package.name]
        # The revision is what the specification pinned; the branch only
        # says where to look for it.
        return ["sh", "-c", "%s && cd %s && git checkout --detach %s" % (
            " ".join(args), package.name, package.parameters["rev"])]

    # Every scheme leaves exactly one unpacked source tree behind, next to
    # the .dsc/tarballs it came from, but only apt-get source and dget name
    # it after the upstream version rather than the package. Directories we
    # put there ourselves are hidden, and skipped here.
    def _source_dir(self, package, workdir):
        directories = [d for d in sorted(os.listdir(workdir))
                       if os.path.isdir(os.path.join(workdir, d))
                       and not d.startswith(".")]
        if len(directories) != 1:
            raise ValueError(
                "fetching '%s' produced %d source directories, expected one: %s"
                % (package.source, len(directories), ", ".join(directories)))
        return os.path.join(workdir, directories[0])

    # Where the patches listed by the specification are staged for the
    # container to reach them. Hidden so _source_dir() does not mistake it
    # for the unpacked source.
    PATCHES = ".patches"

    # Applies the specification's patches to a fetched source tree.
    #
    # A "3.0 (quilt)" package keeps its changes to upstream files in
    # debian/patches and refuses to build with any others in the tree, so
    # patches are added to its series rather than applied to it -- whether
    # or not the source came from git.
    #
    # Anything else (native formats, and the packaging trees kept in git
    # that tend to use them) takes the patch directly. In a git tree that
    # means a commit, since leaving the tree dirty is how gbp-style
    # packaging loses track of what was built; the commit is dated at
    # SOURCE_DATE_EPOCH, with a fixed identity, or the commit hash -- and
    # anything embedding it -- would differ on every rebuild.
    def patch(self, package, sourcedir, epoch):
        if len(package.patches) == 0:
            return

        workdir = os.path.dirname(sourcedir)
        staged = os.path.join(workdir, Builder.PATCHES)
        os.makedirs(staged, exist_ok=True)
        for patch in package.patch_files():
            if not os.path.isfile(patch):
                raise ValueError("package '%s': no such patch file: %s"
                                 % (package.source, patch))
            shutil.copy(patch, staged)

        if self._is_quilt(sourcedir):
            self._patch_series(package, sourcedir)
        else:
            self._patch_tree(package, sourcedir, epoch)

    def _is_quilt(self, sourcedir):
        path = os.path.join(sourcedir, "debian", "source", "format")
        if not os.path.isfile(path):
            # No debian/source/format means the ancient "1.0" format, which
            # has no series to add to.
            return False
        with open(path, "r") as f:
            return "quilt" in f.read()

    def _patch_series(self, package, sourcedir):
        patches = os.path.join(sourcedir, "debian", "patches")
        os.makedirs(patches, exist_ok=True)
        for patch in package.patch_files():
            shutil.copy(patch, patches)

        series = os.path.join(patches, "series")
        # A series file not ending in a newline would otherwise have its
        # last patch glued to the first one added here.
        existing = ""
        if os.path.isfile(series):
            with open(series, "r") as f:
                existing = f.read()
        if len(existing) > 0 and not existing.endswith("\n"):
            existing += "\n"
        with open(series, "w") as f:
            f.write(existing)
            for patch in package.patch_files():
                f.write("%s\n" % os.path.basename(patch))

    def _patch_tree(self, package, sourcedir, epoch):
        git = os.path.isdir(os.path.join(sourcedir, ".git"))
        source = os.path.join(WORKDIR, os.path.basename(sourcedir))
        for patch in package.patch_files():
            staged = "%s/%s/%s" % (WORKDIR, Builder.PATCHES, os.path.basename(patch))
            if git:
                script = ("git apply %s && "
                          "git -c user.name='%s' -c user.email='%s' "
                          "commit --quiet --all --message '%s'"
                          % (staged, GIT_NAME, GIT_EMAIL, os.path.basename(patch)))
                environment = {
                    "GIT_AUTHOR_DATE":    "@%d +0000" % epoch,
                    "GIT_COMMITTER_DATE": "@%d +0000" % epoch,
                }
            else:
                script = "patch -p1 < %s" % staged
                environment = None
            self.builderImage.exec(
                ["sh", "-c", script],
                volumes=[(os.path.dirname(sourcedir), WORKDIR)],
                workdir=source, environment=environment)

    # The date the build is pinned to. dpkg-buildpackage derives one from
    # the changelog on its own, and sbuild passes it down, but a patch
    # committed to a git tree is dated before any of that runs -- so the
    # same value has to be known here.
    def source_date_epoch(self, package, sourcedir):
        if package.source_date_epoch is not None:
            return package.source_date_epoch
        source = os.path.join(WORKDIR, os.path.basename(sourcedir))
        timestamp = self.builderImage.output(
            ["dpkg-parsechangelog", "-STimestamp"],
            volumes=[(os.path.dirname(sourcedir), WORKDIR)], workdir=source)
        return int(timestamp.strip())

    # Cross-compiling is the default whenever the target is not the machine
    # seine runs on: emulating a foreign architecture for a whole package
    # build is slow enough to be worth avoiding, even though not every
    # package can be cross-built. 'cross: false' asks for the slow, always
    # working way instead.
    def cross(self, package):
        if package.cross is not None:
            return package.cross
        return self.distro["architecture"] != HOST_ARCH

    # The architecture of the chroot the build runs in: the host's when
    # cross-compiling, since that is what runs the compiler, and the
    # target's when emulating it.
    def chroot_architecture(self, package):
        if self.cross(package):
            return HOST_ARCH
        return self.distro["architecture"]

    # Rebuilds one package and leaves the .debs, .changes and .buildinfo it
    # produced in the host-side repository. The source tree is turned back
    # into a source package first: patches added to a quilt series are
    # applied by dpkg-source on the way, and sbuild wants a .dsc anyway.
    def build(self, package, sourcedir, epoch):
        workdir = os.path.dirname(sourcedir)
        volumes = [(workdir, WORKDIR), (self.repository(), REPOSITORY)]

        self.builderImage.exec(
            ["dpkg-source", "--build", os.path.basename(sourcedir)],
            volumes=[(workdir, WORKDIR)], workdir=WORKDIR)

        dsc = [f for f in sorted(os.listdir(workdir)) if f.endswith(".dsc")]
        if len(dsc) != 1:
            raise ValueError(
                "building the source package for '%s' produced %d .dsc files, "
                "expected one" % (package.source, len(dsc)))

        # SOURCE_DATE_EPOCH and DEB_BUILD_OPTIONS are passed in the
        # environment: sbuild forwards both into the build, as they are on
        # dpkg's list of variables allowed to reach it. dpkg-buildpackage
        # would work the date out from the changelog by itself, but not
        # when the specification pinned a different one.
        environment = {"SOURCE_DATE_EPOCH": epoch}
        if len(package.options) > 0:
            environment["DEB_BUILD_OPTIONS"] = " ".join(package.options)

        args = [
            "sbuild", "--chroot-mode=unshare",
            "--dist=%s" % self.distro["release"],
            # None of these are seine's business: they check the packaging
            # rather than build it, and would need tooling in the chroot
            # we have no reason to install.
            "--no-run-lintian", "--no-run-piuparts", "--no-run-autopkgtest",
        ]
        if self.cross(package):
            args += ["--build=%s" % HOST_ARCH,
                     "--host=%s" % self.distro["architecture"]]
        else:
            args += ["--arch=%s" % self.distro["architecture"]]
        if len(package.profiles) > 0:
            args += ["--profiles=%s" % ",".join(package.profiles)]

        # A package may build-depend on one rebuilt before it. The chroot
        # reaches the repository through the bind mount configured in the
        # builder image, so it is an ordinary sources.list entry there --
        # same repository, same pin, same behaviour as everywhere else.
        args += ["--extra-repository=deb [trusted=yes] file:%s ./" % REPOSITORY,
                 "--chroot-setup-commands=%s" % apt_preferences_command()]

        args += ["%s/%s" % (WORKDIR, dsc[0])]

        # sbuild bind-mounts a handful of device nodes into its chroot and
        # complains about each one the container does not have. podman only
        # creates /dev/console when it allocates a terminal, which we have
        # no other use for, so point it at /dev/null: nothing in a build
        # has any business writing to a console, and the alternative is a
        # warning per invocation drowning the output that matters.
        script = "ln -sf /dev/null /dev/console; exec %s" % shlex.join(args)

        # Run from the repository so sbuild drops what it built there.
        self.builderImage.exec(
            ["sh", "-c", script], architecture=self.chroot_architecture(package),
            volumes=volumes, workdir=REPOSITORY, environment=environment)

    def repository(self):
        return repository(self.distro)

    # Turns the directory the builds dropped their .debs in into something
    # apt can read: a flat repository with a Packages index. Both the plain
    # and the gzipped index are written, as apt looks for several
    # compressions and complains about each one it does not find.
    def index(self):
        self.builderImage.exec(
            ["sh", "-c", "dpkg-scanpackages --multiversion . > Packages && "
                         "gzip -9 -c Packages > Packages.gz"],
            volumes=[(self.repository(), REPOSITORY)], workdir=REPOSITORY)

    # What a rebuild of this package would depend on, as a file whose
    # presence means it has already been done. Everything the specification
    # says about the package goes into the name, patches included by
    # content, so editing a patch is enough to ask for a rebuild.
    #
    # What is deliberately not in it is the version an unpinned apt://
    # source would resolve to today: knowing it means fetching the source,
    # which is most of the cost this is here to avoid. A specification that
    # pins its versions is therefore exact, and one that does not will keep
    # its first rebuild until --rebuild or a change to the entry. The .debs
    # of a stale build are still there and still installable, so the image
    # is built from something real either way.
    # Everything that would change the .debs goes into the digest: what the
    # specification says about the package, the patches by content, and the
    # distribution the chroot it builds in is made of -- a package built
    # for another release, architecture, or from another mirror is not the
    # same package.
    #
    # The rootfs 'baseline' is deliberately not part of it. Packages are
    # built against the buildd chroot, which is made from the distribution
    # settings above; the image the root file-system is later composed from
    # has no bearing on what comes out of the build.
    def stamp(self, package):
        digest = hashlib.sha256()
        for part in [package.source,
                     ",".join(package.profiles),
                     ",".join(package.options),
                     str(self.cross(package)),
                     str(package.source_date_epoch),
                     self.distro["source"],
                     self.distro["release"],
                     self.distro["architecture"],
                     self.distro["uri"],
                     self.chroot_architecture(package)]:
            digest.update(part.encode())
        for patch in package.patch_files():
            with open(patch, "rb") as f:
                digest.update(f.read())

        return os.path.join(self._stamps(),
                            "%s_%s" % (package.name, digest.hexdigest()[:16]))

    def _stamps(self):
        stamps = os.path.join(self.repository(), STAMPS)
        os.makedirs(stamps, exist_ok=True)
        return stamps

    # What a previous build of this source package left in the repository.
    # Each stamp lists the files its build produced, so an older build can
    # be undone without having to guess which binary packages came from
    # which source -- the names rarely match, and one source package
    # commonly produces several.
    def _previous(self, package):
        previous = {}
        for stamp in sorted(os.listdir(self._stamps())):
            if stamp.startswith("%s_" % package.name) == False:
                continue
            path = os.path.join(self._stamps(), stamp)
            with open(path, "r") as f:
                previous[path] = [line.strip() for line in f if len(line.strip()) > 0]
        return previous

    # Drops what an earlier build of the same source package left behind,
    # keeping anything the build that just ran produced -- rebuilding the
    # same version overwrites its own files rather than replacing them, and
    # they would otherwise be deleted right after being written.
    #
    # Without this the repository would keep every version ever built and
    # go on offering them: the index lists all of them, and apt installs
    # the highest version it is offered, which after a downgrade is the one
    # that was meant to be replaced.
    def _forget(self, package, produced):
        for stamp, files in self._previous(package).items():
            for name in files:
                if name in produced:
                    continue
                path = os.path.join(self.repository(), name)
                if os.path.isfile(path):
                    os.unlink(path)
            os.unlink(stamp)

    # Files in the repository written since 'started'. The index is left
    # out: it is regenerated from whatever the repository holds once every
    # package has been built, and belongs to none of them.
    def _produced(self, started):
        produced = []
        for name in sorted(os.listdir(self.repository())):
            path = os.path.join(self.repository(), name)
            if os.path.isfile(path) == False or name.startswith("Packages"):
                continue
            if os.path.getmtime(path) >= started:
                produced.append(name)
        return produced

    def _record(self, stamp, produced):
        with open(stamp, "w") as f:
            for name in produced:
                f.write("%s\n" % name)

    # Rebuilds every package the specification asked for, in the order they
    # were sorted into. Each gets a working directory of its own, thrown
    # away afterwards unless --keep was asked for: what is worth keeping
    # (the .debs) is in the repository by then.
    def run(self, packages, hostBootstrap):
        if len(packages) == 0:
            return

        # The digest is taken once, here, and carried through to the end:
        # it describes the inputs this build is about to use. Recomputing
        # it afterwards would record whatever the patches and fragments
        # say by then, and a fragment edited while its kernel was building
        # would leave a stamp claiming a kernel that was never built --
        # which the next build would then skip.
        rebuild = self.options.get("rebuild", False)
        pending = [(p, self.stamp(p)) for p in packages]
        pending = [(p, s) for p, s in pending
                   if rebuild or os.path.isfile(s) == False]
        if len(pending) == 0:
            return

        if ContainerEngine.hasImage(self.builderImage.name) == False:
            self.builderImage.create(hostBootstrap)

        # Every build has the repository in its sources.list, including the
        # first one, when nothing has been rebuilt yet: apt needs an index
        # to read there, even an empty one, or the build fails before it
        # starts. It is refreshed after each package so the next one can
        # build against what came out of the last.
        self.index()

        for package, stamp in pending:
            chroot = SbuildChroot(self.distro, self.options,
                                  self.chroot_architecture(package))
            chroot.create(self.builderImage)

            workdir = tempfile.mkdtemp(dir=ContainerEngine.scratch(),
                                       prefix="source-")
            try:
                print("rebuilding '%s'" % package.source)
                sourcedir = self.fetch(package, workdir)
                epoch = self.source_date_epoch(package, sourcedir)
                self.patch(package, sourcedir, epoch)

                # Everything written while the build ran is what it
                # produced, whether it is a new file or one it overwrote.
                started = time.time()
                self.build(package, sourcedir, epoch)
                produced = self._produced(started)

                # Both only once it built. A stamp left by a failed build
                # would skip the package next time and compose the image
                # from whatever the repository happened to hold, and
                # dropping the previous build before this one succeeds
                # would leave the repository with neither.
                self._forget(package, produced)
                self._record(stamp, produced)
                self.index()
            finally:
                if self.options.get("keep"):
                    print("keeping '%s' (source of '%s') as requested"
                          % (workdir, package.name))
                else:
                    shutil.rmtree(workdir, ignore_errors=True)

# Identity the patch commits are made under. Fixed, like their date: a
# commit made by whoever happens to be running the build is a commit whose
# hash cannot be reproduced by anyone else.
GIT_NAME  = "seine"
GIT_EMAIL = "seine@localhost"

# Making the rebuilt packages visible to apt, wherever apt is being run:
# the chroot packages are built in, the container the root file-system is
# composed in, and the imager's own containers.
#
# Both files are written the same way everywhere so there is one answer to
# "why is this version being installed": the repository is trusted, since
# it is unsigned and was produced locally moments ago, and it is pinned
# above everything else. The pin matches on an empty origin, which is what
# a file:// repository has, and 1001 rather than 1000 is what allows a
# rebuilt package to replace a *higher* version from the archive -- the
# usual case, since a distribution that has since rebuilt the package will
# have a binNMU of it.
SOURCES_LIST = "/etc/apt/sources.list.d/seine-packages.list"
PREFERENCES  = "/etc/apt/preferences.d/seine-packages"

def apt_preferences_command():
    return " && ".join([
        "echo 'Package: *' > %s" % PREFERENCES,
        "echo 'Pin: origin \"\"' >> %s" % PREFERENCES,
        "echo 'Pin-Priority: 1001' >> %s" % PREFERENCES,
    ])

def apt_configuration(mountpoint):
    return "echo 'deb [trusted=yes] file:%s ./' > %s && %s" % (
        mountpoint, SOURCES_LIST, apt_preferences_command())

def apt_deconfiguration():
    return "rm -f %s %s" % (SOURCES_LIST, PREFERENCES)

# The same configuration for images built from a Dockerfile: a layer that
# sets apt up, and the bind mount that makes the repository readable while
# that image is being built. Both are empty when the specification rebuilt
# nothing, so an image that has no use for the repository is not given a
# sources.list pointing at a directory that will not be there.
def apt_setup_layer(distro):
    if has_packages(distro) == False:
        return ""
    return "RUN %s\n" % apt_configuration(REPOSITORY)

def build_volumes(distro):
    if has_packages(distro) == False:
        return []
    return ["-v", "%s:%s:ro" % (repository(distro), REPOSITORY)]

# Where the rebuilt packages of a specification are kept, and whether there
# is anything there to install: a specification with no 'packages' section
# leaves the directory without an index, and pointing apt at it would only
# earn a failed 'apt-get update'.
def repository(distro):
    return ContainerEngine.packages(distro["release"], distro["architecture"])

def has_packages(distro):
    return os.path.isfile(os.path.join(repository(distro), "Packages"))

# Validates the 'packages' section and returns it as Package objects,
# ordered the way they will be built. Packages that build-depend on one
# another are ordered by 'priority', as playbooks are.
def parse(spec):
    packages = spec.get("packages", [])
    if type(packages) != type([]):
        raise ValueError("'packages' shall be a list of source packages!")

    parsed = [Package(p, i + 1) for i, p in enumerate(packages)]
    return sorted(parsed, key=lambda p: p.priority)
