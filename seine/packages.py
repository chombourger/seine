# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
import re
import shlex
import shutil
import tempfile
import time

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

from seine.sbuild import BuilderImage
from seine.sbuild import REPOSITORY
from seine.sbuild import SbuildChroot
from seine.utils  import apt_sources
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

# Build types 'extends' knows about, and the settings each of them takes.
# A kernel is configured rather than patched: Debian builds its kernels
# from a stack of kconfig files under debian/, so a fragment appended to
# the right one is both easier to write and less likely to conflict with
# the next point release than a patch would be.
EXTENSIONS = {
    "kernel": ["config", "featureset", "flavour"],
}

# Debian identifies a kernel by architecture, featureset and flavour, and
# a flavour name only means something within its featureset -- amd64's
# realtime kernel and its ordinary one are both the 'amd64' flavour, of
# the 'rt' and 'none' featuresets. So both have to be named to pick one,
# and 'none' is the one nearly everything wants.
DEFAULT_FEATURESET = "none"

# Appended to the version of every package rebuilt here, so it sorts above
# the distribution's own and says plainly that it is not it. 'revision'
# overrides it per package.
DEFAULT_REVISION = "mod1"

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
        self.extends = self._parse_extends(spec)
        self.after = self._parse_list(spec, "after")
        self.before = self._parse_list(spec, "before")
        self.cross = self._parse_bool(spec, "cross")
        self.options = self._parse_list(spec, "options")
        self.patches = self._parse_list(spec, "patches")
        self.profiles = self._parse_list(spec, "profiles")
        self.revision = spec.get("revision", DEFAULT_REVISION)
        if type(self.revision) != type(""):
            raise self._error("'revision' shall be a string")
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

    # Settings that only mean something for a particular kind of package go
    # under 'extends', named after the kind. A kernel's configuration and
    # flavour would be silently meaningless on a busybox entry; naming the
    # kind makes both the intent and the mistake visible.
    def _parse_extends(self, spec):
        extends = spec.get("extends", {})
        if type(extends) != type({}):
            raise self._error("'extends' shall be a dictionary of build types")

        for kind in extends:
            if kind not in EXTENSIONS:
                raise self._error(
                    "'extends' has no '%s' build type, expected one of %s"
                    % (kind, ", ".join(sorted(EXTENSIONS))))
            settings = extends[kind]
            if type(settings) != type({}):
                raise self._error("'extends: %s' shall be a dictionary" % kind)
            for setting in settings:
                if setting not in EXTENSIONS[kind]:
                    raise self._error(
                        "'extends: %s' has no '%s' setting, expected one of %s"
                        % (kind, setting, ", ".join(sorted(EXTENSIONS[kind]))))

        kernel = extends.get("kernel", {})
        self.kernel_config = self._parse_list(kernel, "config")
        self.kernel_flavour = kernel.get("flavour")
        self.kernel_featureset = kernel.get("featureset", DEFAULT_FEATURESET)
        for setting, value in [("flavour", self.kernel_flavour),
                               ("featureset", self.kernel_featureset)]:
            if value is not None and type(value) != type(""):
                raise self._error(
                    "'extends: kernel: %s' shall be a string" % setting)
        self.kernel = "kernel" in extends
        return extends

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
        return self._files(self.patches)

    def kernel_config_files(self):
        return self._files(self.kernel_config)

    def _files(self, names):
        return [os.path.normpath(os.path.join(self.dirname, n)) for n in names]

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

    # Applies the 'extends: kernel:' settings to a fetched kernel source.
    #
    # Debian assembles each kernel's configuration from a stack of kconfig
    # files, the last of which is the architecture's own -- appending to it
    # puts the specification's fragments last, where they win. The kernel's
    # own 'oldconfig' then turns off whatever the disabled options were
    # holding up, so a fragment says what it means rather than having to
    # list every symbol underneath it.
    #
    # None of this needs a patch: the configuration lives in debian/, which
    # a "3.0 (quilt)" package lets us edit directly.
    def extend_kernel(self, package, sourcedir):
        if package.kernel == False:
            return

        architecture = self.distro["architecture"]
        config = os.path.join(sourcedir, "debian", "config", architecture, "config")
        if os.path.isfile(config) == False:
            raise ValueError(
                "package '%s' is built as a kernel, but its source has no "
                "debian/config/%s/config to configure"
                % (package.source, architecture))

        fragments = package.kernel_config_files()
        for fragment in fragments:
            if os.path.isfile(fragment) == False:
                raise ValueError("package '%s': no such kernel configuration "
                                 "fragment: %s" % (package.source, fragment))
        if len(fragments) > 0:
            with open(config, "a") as f:
                for fragment in fragments:
                    f.write("\n# %s, added by seine\n" % os.path.basename(fragment))
                    with open(fragment, "r") as contents:
                        f.write(contents.read())

        if package.kernel_flavour is not None:
            self._restrict_flavour(package, sourcedir, architecture)

        # debian/control lists a binary package per flavour and is generated
        # from the files edited above, so it has to be rebuilt before the
        # source package is; sbuild would otherwise build every flavour the
        # unmodified control file still mentions.
        #
        # DEBIAN_KERNEL_DISABLE_SIGNED is what makes the rebuild reachable
        # at all. On the architectures that ship a signed kernel, 'linux'
        # builds linux-image-<abi>-<flavour>-unsigned, and the package the
        # linux-image-<flavour> metapackage actually depends on --
        # linux-image-<abi>-<flavour> -- is built by a *different* source
        # package, linux-signed-<arch>, which takes the unsigned one and
        # signs it with a key we do not have. Rebuilding 'linux' alone
        # therefore produces a kernel nothing installs: apt keeps taking
        # the distribution's signed one, and the image looks fine while
        # containing none of the configuration asked for.
        #
        # Turning signing off makes 'linux' build that name itself, which
        # the pin then prefers. A locally rebuilt kernel could not have
        # carried Debian's signature in any case; Secure Boot with one
        # needs a key of your own.
        #
        # PYTHONDONTWRITEBYTECODE: the generator is written in python and
        # leaves __pycache__ behind in debian/, which dpkg-source then
        # refuses to put in a source package ("unwanted binary file").
        self.builderImage.exec(
            ["debian/rules", "debian/control"],
            volumes=[(os.path.dirname(sourcedir), WORKDIR)],
            workdir="%s/%s" % (WORKDIR, os.path.basename(sourcedir)),
            environment={"PYTHONDONTWRITEBYTECODE": "1",
                         "DEBIAN_KERNEL_DISABLE_SIGNED": "1"},
            check=False)

        if package.kernel_flavour is not None:
            self._check_flavour(package, sourcedir, architecture)

    # Restricting the build only takes effect once the rules have been
    # regenerated from the edited defines, and that regeneration reports
    # success by failing -- its own message says so. Rather than read
    # anything into its exit status, check what it produced: the generated
    # rules carry one setup target per kernel, named by featureset and
    # flavour, and exactly the one asked for should be left.
    #
    # Asking what is left, rather than confirming that particular kernels
    # are gone, is deliberate. A flavour name is not unique on its own --
    # amd64's realtime and ordinary kernels are both the 'amd64' flavour --
    # so a check phrased as "these should have disappeared" has nothing to
    # look for in exactly the case that matters.
    def _check_flavour(self, package, sourcedir, architecture):
        control = os.path.join(sourcedir, "debian", "control")
        if os.path.isfile(control) == False:
            raise ValueError(
                "package '%s': restricting the kernel left no debian/control "
                "behind" % package.source)

        kernels = self._kernel_packages(control, architecture)
        if len(kernels) != 1:
            raise ValueError(
                "package '%s': restricting the kernel to %s/%s did not take "
                "effect, %d kernels are still built for '%s': %s"
                % (package.source, package.kernel_featureset,
                   package.kernel_flavour, len(kernels), architecture,
                   ", ".join(kernels)))

    # The kernel image packages debian/control builds for an architecture.
    # It is read as the deb822 it is rather than pattern-matched, and the
    # binary packages are asked about rather than the generated makefiles:
    # those write a target and its dependencies on one line, name families
    # that are not kernels at all, and are altogether the wrong thing to
    # be parsing to answer "what will this build".
    def _kernel_packages(self, control, architecture):
        found = []
        with open(control, "r") as f:
            stanzas = f.read().split("\n\n")

        for stanza in stanzas:
            fields = {}
            for line in stanza.split("\n"):
                field = re.match(r"^([A-Za-z-]+):\s*(.*)$", line)
                if field:
                    fields[field.group(1)] = field.group(2)
            name = fields.get("Package", "")
            if architecture not in fields.get("Architecture", "").split():
                continue
            # linux-image-<version>-<abi>-<something>, the versioned image
            # packages, one per kernel -- as opposed to the metapackages
            # and the debug packages beside them.
            if re.match(r"^linux-image-[0-9][0-9.]*-[0-9]+-", name) and \
               name.endswith("-dbg") == False:
                found.append(name)
        return sorted(set(found))

    # Gives the rebuild a version of its own, above the distribution's, and
    # says in the changelog that it is not the distribution's package. What
    # a machine is running can then be read off its versions rather than
    # inferred, and apt prefers ours on version alone rather than only
    # because of the pin.
    #
    # It matters twice over for a kernel: the packaging refuses to disable
    # signed code in a release build, and rightly, since what comes out is
    # not what was released -- so without this the rebuilt kernel keeps the
    # name only a signed build may use, and nothing ever installs it.
    #
    # The entry is dated at SOURCE_DATE_EPOCH, like the patches committed to
    # a git tree, so it does not change from one rebuild to the next.
    def local_release(self, package, sourcedir, epoch):
        path = os.path.join(sourcedir, "debian", "changelog")
        with open(path, "r") as f:
            changelog = f.read()

        heading = re.match(r"^(\S+) \(([^)]+)\)", changelog)
        if heading is None:
            raise ValueError("package '%s': debian/changelog does not start "
                             "with a version" % package.source)
        source, version = heading.group(1), heading.group(2)
        date = format_datetime(datetime.fromtimestamp(epoch, timezone.utc))

        entry = ("%s (%s+%s) UNRELEASED; urgency=medium\n\n"
                 "  * Rebuilt by seine.\n\n"
                 " -- %s <%s>  %s\n\n"
                 % (source, version, package.revision, GIT_NAME, GIT_EMAIL, date))
        with open(path, "w") as f:
            f.write(entry + changelog)

    # Cuts the build down to the one kernel asked for. Debian builds every
    # featureset and flavour an architecture has, and for the kernel each
    # of those is a full build -- on amd64, a cloud flavour and a realtime
    # kernel besides the one an appliance wants.
    def _restrict_flavour(self, package, sourcedir, architecture):
        root = os.path.join(sourcedir, "debian", "config", architecture)
        featuresets = self._defines_list(os.path.join(root, "defines"),
                                         "featuresets")
        if package.kernel_featureset not in featuresets:
            raise ValueError(
                "package '%s': architecture '%s' has no '%s' kernel "
                "featureset, expected one of %s"
                % (package.source, architecture, package.kernel_featureset,
                   ", ".join(sorted(featuresets))))

        defines = os.path.join(root, package.kernel_featureset, "defines")
        flavours = self._defines_list(defines, "flavours")
        if package.kernel_flavour not in flavours:
            raise ValueError(
                "package '%s': the '%s' featureset of architecture '%s' has "
                "no '%s' kernel flavour, expected one of %s"
                % (package.source, package.kernel_featureset, architecture,
                   package.kernel_flavour, ", ".join(sorted(flavours))))

        self._defines_replace(os.path.join(root, "defines"), "featuresets",
                              [package.kernel_featureset])
        self._defines_replace(defines, "flavours", [package.kernel_flavour])
        # Both may name a flavour that has just been removed.
        self._defines_set(defines, "default-flavour", package.kernel_flavour)
        self._defines_set(defines, "quick-flavour", package.kernel_flavour)

    # debian/config/*/defines are ini-like, with list values written one
    # per line and indented under their key.
    def _defines_list(self, path, key):
        values = []
        collecting = False
        with open(path, "r") as f:
            for line in f:
                if line.strip() == "%s:" % key:
                    collecting = True
                elif collecting:
                    if line.startswith(" ") and len(line.strip()) > 0:
                        values.append(line.strip())
                    else:
                        break
        return values

    def _defines_replace(self, path, key, values):
        with open(path, "r") as f:
            lines = f.readlines()

        out = []
        skipping = False
        for line in lines:
            if line.strip() == "%s:" % key:
                out.append(line)
                out += [" %s\n" % v for v in values]
                skipping = True
            elif skipping:
                if line.startswith(" ") and len(line.strip()) > 0:
                    continue
                skipping = False
                out.append(line)
            else:
                out.append(line)

        with open(path, "w") as f:
            f.writelines(out)

    def _defines_set(self, path, key, value):
        with open(path, "r") as f:
            lines = f.readlines()
        out = ["%s: %s\n" % (key, value) if line.startswith("%s:" % key) else line
               for line in lines]
        with open(path, "w") as f:
            f.writelines(out)

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

        # The fetched source came with a .dsc of its own, and ours is about
        # to be written beside it under a different name -- the local
        # revision changed the version. Take the old one out of the way
        # first, so what is left is unambiguously what we built and sbuild
        # cannot be handed the source we started from.
        for name in os.listdir(workdir):
            if name.endswith(".dsc"):
                os.unlink(os.path.join(workdir, name))

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
    # for another release, architecture, or from another feed is not the
    # same package. The feeds go in whole rather than the distribution's
    # bare 'uri': moving a feed -- to another snapshot, say -- leaves that
    # 'uri' untouched while changing every version the build would see.
    #
    # The rootfs 'baseline' is deliberately not part of it. Packages are
    # built against the buildd chroot, which is made from the distribution
    # settings above; the image the root file-system is later composed from
    # has no bearing on what comes out of the build.
    def stamp(self, package, depends=None):
        digest = hashlib.sha256()
        for part in [package.source,
                     ",".join(package.profiles),
                     ",".join(package.options),
                     str(self.cross(package)),
                     str(package.source_date_epoch),
                     package.revision,
                     str(package.kernel_featureset),
                     str(package.kernel_flavour),
                     self.distro["source"],
                     self.distro["release"],
                     self.distro["architecture"],
                     "\n".join(apt_sources(self.distro, sources=True)),
                     self.chroot_architecture(package)]:
            digest.update(part.encode())
        # Patches and kernel configuration fragments count by content, not
        # by name: editing one without touching the specification has to be
        # enough to ask for a rebuild, all the more for a kernel, where the
        # alternative is silently keeping one built from the fragment as it
        # used to read.
        for path in package.patch_files() + package.kernel_config_files():
            with open(path, "rb") as f:
                digest.update(f.read())

        # A package built against another has to be rebuilt when that one
        # changes: it was compiled and linked against what that package
        # installed. Folding the dependency's digest in says so, and says
        # it transitively, since that digest already carries its own.
        for name in sorted(depends or {}):
            digest.update(depends[name].encode())

        return os.path.join(self._stamps(),
                            "%s_%s" % (package.name, digest.hexdigest()[:16]))

    # The stamp of every package, in build order, each one folding in the
    # stamps of what it is built after. The order is what makes that
    # possible: a package's dependencies have been given their digest
    # before it is given its own.
    def stamps(self, packages):
        digests = {}
        stamps = []
        for package in packages:
            depends = {d.name: digests[d.name] for d in getattr(package, "depends", [])
                       if d.name in digests}
            stamp = self.stamp(package, depends)
            digests[package.name] = os.path.basename(stamp).rsplit("_", 1)[1]
            stamps.append((package, stamp))
        return stamps

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
        pending = [(p, s) for p, s in self.stamps(packages)]
        pending = [(p, s) for p, s in pending
                   if rebuild or os.path.isfile(s) == False]
        if len(pending) == 0:
            return

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
                self.local_release(package, sourcedir, epoch)
                self.extend_kernel(package, sourcedir)

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
# it is unsigned and was produced locally moments ago, and it is preferred
# over the distribution's. The pin matches on an empty origin, which is
# what a file:// repository has.
#
# 900, deliberately not the 1001 that would let a rebuild replace a
# *higher* version from the archive. Above 1000 apt will downgrade an
# already-installed package to match, and a repository holding anything
# older than what a chroot already has then breaks every build in it:
#
#   The following packages will be DOWNGRADED: linux-libc-dev
#   E: Packages were downgraded and -y was used without --allow-downgrades
#
# It is not needed for its original purpose either, now that every rebuilt
# package carries a local revision and so sorts above the distribution's
# own version to begin with. This only decides which of two origins to
# prefer, and leaves installed packages alone.
SOURCES_LIST = "/etc/apt/sources.list.d/seine-packages.list"
PREFERENCES  = "/etc/apt/preferences.d/seine-packages"

def apt_preferences_command():
    return " && ".join([
        "echo 'Package: *' > %s" % PREFERENCES,
        "echo 'Pin: origin \"\"' >> %s" % PREFERENCES,
        "echo 'Pin-Priority: 900' >> %s" % PREFERENCES,
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
# ordered the way they will be built.
def parse(spec):
    packages = spec.get("packages", [])
    if type(packages) != type([]):
        raise ValueError("'packages' shall be a list of source packages!")

    parsed = [Package(p, i + 1) for i, p in enumerate(packages)]
    return order(parsed)

# Orders packages for building. 'priority' says which package would rather
# go first; 'before' and 'after' say which package *has* to, naming the
# other by its package name.
#
# Both are needed. A package that build-depends on another has to be built
# after it, and saying so by giving the two of them priorities that happen
# to sort the right way records the conclusion rather than the reason --
# and quietly stops holding when a third package is added between them.
#
# Constraints win over priority, and priority decides between packages that
# no constraint separates, so adding a 'before' to a specification does not
# rearrange the packages around it.
def order(packages):
    indexes = {}
    for index, package in enumerate(packages):
        indexes.setdefault(package.name, []).append(index)

    # predecessors[i]: packages that have to be built before packages[i].
    predecessors = [set() for _ in packages]
    for index, package in enumerate(packages):
        for name in package.after:
            for other in _referenced(indexes, package, name, "after"):
                predecessors[index].add(other)
        for name in package.before:
            for other in _referenced(indexes, package, name, "before"):
                predecessors[other].add(index)

    # Kahn's algorithm, taking the highest priority package among those
    # whose predecessors have all been built, and the earliest listed among
    # those of equal priority. Quadratic in the number of packages, which
    # is a handful.
    ordered = []
    remaining = set(range(len(packages)))
    while len(remaining) > 0:
        ready = [i for i in remaining if len(predecessors[i] & remaining) == 0]
        if len(ready) == 0:
            raise ValueError(
                "'before'/'after' settings of these packages depend on each "
                "other in a circle: %s" % ", ".join(
                    sorted(packages[i].name for i in remaining)))
        ready.sort(key=lambda i: (packages[i].priority, i))
        chosen = ready[0]
        # What this package is built after, kept so a rebuild of any of
        # them can be seen in its digest. Reachability is not needed here:
        # the digest of a direct dependency already carries its own.
        packages[chosen].depends = [packages[i] for i in predecessors[chosen]]
        ordered.append(packages[chosen])
        remaining.discard(chosen)
    return ordered

# A 'before'/'after' entry names another package of the same specification.
# Naming something that is not there is a typo worth reporting rather than
# a constraint worth ignoring -- the build would otherwise go ahead in an
# order the specification did not ask for.
def _referenced(indexes, package, name, setting):
    if name not in indexes:
        raise package._error(
            "'%s' names '%s', which no package in this specification builds"
            % (setting, name))
    others = [i for i in indexes[name] if i != package.index - 1]
    if len(others) == 0:
        raise package._error("'%s' names the package itself" % setting)
    return others
