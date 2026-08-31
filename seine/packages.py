# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import collections
import functools
import hashlib
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import yaml

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

from seine        import kernel
from seine        import module
from seine        import signing
from seine.cache_index import PACKAGE, Index, say, since
from seine.sbuild import BuilderImage
from seine.tasks  import Task
from seine.sbuild import OUTPUT
from seine.sbuild import REPOSITORY
from seine.sbuild import SbuildChroot
from seine.utils  import apt_sources
from seine.utils  import feeds
from seine.utils  import locked
from seine.utils  import offline_apt_script
from seine.utils  import offline_suites
from seine.utils  import vendor_mountpoint
from seine.utils  import ContainerEngine
from seine.utils  import GIT_EMAIL
from seine.utils  import GIT_NAME
from seine.utils  import HOST_ARCH
from seine.utils  import WORKDIR
from seine.utils  import redact
from seine.utils  import redactions

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

# Who a rebuild is for. 'target' is what the image installs and is what a
# package that says nothing gets. 'host' is for the machine doing the
# building: a code generator a later package build-depends on, or a tool
# the imager runs.
#
# A list of roles rather than a word meaning "two of them": a compat
# architecture beside the image's -- i386 on amd64, armhf on arm64 -- is a
# third role to come, and a specification that had said 'both' would then
# be saying which two without naming them.
SCOPES = ["host", "target"]
DEFAULT_SCOPE = ["target"]

# What a package's 'apt-preferences' is keyed by when it names no release,
# i.e. when one text is meant for all of them. Not a release anything can
# be called, so it cannot collide with one.
ANY_RELEASE = None

# Build types 'extends' knows about, and the settings each of them takes.
# Each build type is a module of its own -- seine/kernel.py and the rest --
# holding the settings it takes, what it does to a source, and the checks
# that go with it. What is left here is what a source package is regardless
# of what is built from it.
#
# A dictionary rather than a registry the build types register into: they
# are not peers -- a module is built against a kernel -- and an order
# written out in the code that calls them is easier to follow than one
# falling out of a loop over plugins.
EXTENSIONS = {
    "kernel": kernel.SETTINGS,
    "module": module.SETTINGS,
}


# The date a build is pinned to when the source can say nothing about
# one: no changelog, and no revision with a date on it. Fixed rather than
# now, since a date that moves makes two builds of one specification
# produce different source packages. 2000-01-01, which is nobody's
# release date and obviously not a real one.
FALLBACK_EPOCH = 946684800





# What apt-ftparchive remembers about the packages it has already read,
# so that indexing the repository after every build does not re-hash a
# kernel's worth of .debs each time.
INDEX_CACHE = ".packages.db"







# Appended to the version of every package rebuilt here, so it sorts above
# the distribution's own and says plainly that it is not it. 'revision'
# overrides it per package.
DEFAULT_REVISION = "mod1"

# What a Debian version's revision may hold -- letters, digits, '+', '.'
# and '~', never '-': a hyphen ends the revision and starts a second one,
# which 'dpkg-parsechangelog' reads as "not a version" rather than as two
# fields. A flavour name is free to be friendlier than that; whether it
# is used as a revision is what has to check.
DEBIAN_REVISION = re.compile(r"^[A-Za-z0-9+.~]+$")



class Package:
    def __init__(self, spec, index):
        self.index = index
        if type(spec) != type({}):
            raise ValueError("package #%d is not a dictionary!" % index)
        # An entry says what it is, one way or the other: a 'source' is how
        # a package asking to be built says it, and a 'name' alone is how a
        # description under 'defaults' says which package it is about.
        if "source" not in spec and type(spec.get("name")) != type(""):
            raise ValueError(
                "package #%d has neither a 'source' to build nor a 'name' "
                "saying which package it describes!" % index)

        self.spec = spec
        self.priority = spec.get("priority", 500)
        # Which file wrote each setting, for the messages that ask someone
        # to change one: a package described by three files has three
        # answers, and the useful one is per setting.
        self.origins = spec.get("_origins", {})

        self._parse_source(spec.get("source"))
        self.name = self._parse_name(spec)
        self.extends = self._parse_extends(spec)
        self.after = self._parse_list(spec, "after")
        self.before = self._parse_list(spec, "before")
        self.cross = self._parse_bool(spec, "cross")
        self.options = self._parse_list(spec, "options")
        self.patches = self._parse_list(spec, "patches")
        self.profiles = self._parse_list(spec, "profiles")
        self.sha256 = self._parse_digest(spec, "sha256")
        # A kernel with 'derived-flavours' defaults its revision to the
        # name(s) it derives, not 'mod1': two files rebuilding 'linux'
        # under different flavours would otherwise publish the same
        # '<source>_<version>+mod1.dsc'. 'revision' still overrides it.
        default_revision = DEFAULT_REVISION
        if self.kernel_derived_flavours:
            names = sorted(set(name for derived in self.kernel_derived_flavours.values()
                               for name in derived))
            # '.', not '-': a derived flavour is free to be named
            # 'cloud-edge', and joining two such names with '-' would be
            # indistinguishable from the hyphen 'dpkg-parsechangelog'
            # itself reads as ending the revision.
            default_revision = ".".join(names)
        if ("revision" not in spec and default_revision != DEFAULT_REVISION
                and DEBIAN_REVISION.match(default_revision) is None):
            raise self._error(
                "'%s' cannot be a Debian revision on its own -- only "
                "letters, digits, '+', '.' and '~' may appear there, "
                "never '-' -- so this needs 'revision' written down "
                "instead of taken from the flavour name(s)"
                % default_revision)
        self.revision = spec.get("revision", default_revision)
        if type(self.revision) != type(""):
            raise self._error("'revision' shall be a string")
        self.scope = self._parse_scope(spec)
        self.apt_preferences = self._parse_apt_preferences(spec)
        # A kernel is described per architecture and named for one. Its
        # flavour is a name within an architecture -- 'arm64', 'amd64',
        # 'rpi' -- so one 'flavour' cannot be right for two of them, and a
        # specification asking for both would be asking for a kernel it
        # has not described. Two entries, each naming its own flavour, is
        # what that means.
        if self.kernel and len(self.scope) > 1:
            raise self._error(
                "'extends: kernel' takes one 'scope' role: a kernel is "
                "configured per architecture, down to the name of its "
                "flavour, and '%s' asks for one kernel to be several. List "
                "the architectures as separate packages, each with the "
                "'flavour' that architecture has." % ", ".join(self.scope))
        self.source_date_epoch = self._parse_epoch(spec)
        self.upstream_version = self._parse_version(spec)


    def _error(self, message):
        return ValueError("package #%d ('%s'): %s" % (self.index, self.source, message))

    # Whether two entries describe the same build, for a caller merging
    # several specifications' package lists -- building only one of two
    # differently-configured packages sharing a name would ship the wrong
    # one. Compares raw settings, ignoring '_origins' (which file wrote it)
    # and 'priority' (where it sorts in one list) -- neither is part of
    # what gets built.
    IGNORED_SETTINGS = ("_origins", "priority")

    def same_as(self, other):
        mine = {k: v for k, v in self.spec.items()
                if k not in Package.IGNORED_SETTINGS}
        theirs = {k: v for k, v in other.spec.items()
                 if k not in Package.IGNORED_SETTINGS}
        return mine == theirs

    def _parse_source(self, source):
        self.source = source
        # Defaults for the fields only some of the schemes carry, so callers
        # may read them without caring which scheme they got -- or, for an
        # entry that only describes a package, that it named no source.
        self.scheme = None
        self.name = None
        self.version = None
        self.parameters = {}
        self.source_name = None

        # A description leaves where the source comes from to the file that
        # asks for the build. It is checked like any other entry, so a
        # misspelt setting is reported by the file holding it.
        if source is None:
            return

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

        # What the URI says this is called, kept apart from what the package
        # is called: fetching uses this one, while 'name' is what the
        # specification and the repository call the result.
        self.source_name = self.name

    # What this package is called, which is the source package it produces
    # rather than the last word of the URI it came from. The two are the
    # same for a source carrying its own debian/ directory, and not for a
    # tree carrying none: what seine generates from a clone should not be
    # named after the repository that happens to hold it.
    #
    # It is the name everything else uses -- 'before' and 'after', the
    # build's stamp, what the graph calls the step -- so a package renamed
    # is renamed everywhere at once.
    def _parse_name(self, spec):
        name = spec.get("name")
        if name is None:
            return self.source_name
        if type(name) != type(""):
            raise self._error("'name' shall be a string")
        # Checked here rather than by dpkg part-way through a build: what
        # this names is a Debian source package, and policy says what one
        # may be called.
        if re.match(r"^[a-z0-9][a-z0-9+.-]+$", name) is None:
            raise self._error(
                "'name' is '%s', which is not a source package name: those "
                "are lowercase, start with a letter or a digit, are at "
                "least two characters, and hold only letters, digits and "
                "'+', '-' or '.'" % name)
        return name

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
                if setting in EXTENSIONS[kind]:
                    continue
                # A module names its kernels once per architecture, so its
                # settings are not a fixed list: '<arch>-kernels' is one
                # of them for whatever architecture is being built for.
                if kind == "module" and module.MODULE_KERNELS.match(setting):
                    continue
                expected = ", ".join(sorted(EXTENSIONS[kind]))
                if kind == "module":
                    expected += ", <architecture>-kernels"
                raise self._error(
                    "'extends: %s' has no '%s' setting, expected one of %s"
                    % (kind, setting, expected))

        kernel.parse(self, extends)
        module.parse(self, extends)
        return extends

    # The upstream version of a source seine writes the packaging for.
    # Not the 'version' a 'source' URI pins, which says which of the
    # archive's versions to fetch -- this one says what is being built,
    # for a tree that has no way of saying it.
    #
    # A tree that carries its own debian/ carries a changelog, and that
    # says what version is being built. A bare upstream tree says nothing:
    # a git revision is not a version, and a tag is not one either until
    # somebody decides which part of it counts. So the specification says
    # it, and says it for every package seine packages itself.
    def _parse_version(self, spec):
        version = spec.get("version")
        # A string, and not a number that looks like one: yaml reads an
        # unquoted 1.10 as a float, and a float is 1.1, which is a
        # different version and a silent one.
        if version is not None and type(version) != type(""):
            raise self._error(
                "'version' shall be a string: write it in quotes, since a "
                "version is not a number -- yaml reads 1.10 as 1.1")
        if self.module and version is None and self.source is not None:
            raise self._error(
                "'version' is not set. seine writes the packaging for an "
                "out-of-tree module, so nothing in the tree says what "
                "version is being built -- the specification has to.")
        return version

    # apt preferences to put in front of this package's build, as
    # apt_preferences(5) writes them. Taken verbatim: what can be said in
    # that file is apt's to define, and a setting that parsed it would be a
    # second, smaller language to keep up to date.
    #
    # Written as one text for every release, or keyed by release when they
    # need different ones -- which is the ordinary case rather than the
    # exception, since what a pin names is a suite: 'Pin: release
    # n=bookworm' is not a thing to say while building trixie. A release
    # the mapping does not name gets none, so a package needing a pin for
    # one release alone says only that.
    def _parse_apt_preferences(self, spec):
        preferences = spec.get("apt-preferences")
        if preferences is None:
            return {}
        if type(preferences) == type(""):
            return {ANY_RELEASE: self._checked_preferences(preferences, None)}
        if type(preferences) != type({}):
            raise self._error(
                "'apt-preferences' shall be a string, as written in a file "
                "under /etc/apt/preferences.d, or a mapping of release names "
                "to one")
        return {release: self._checked_preferences(text, release)
                for release, text in preferences.items()}

    def _checked_preferences(self, text, release):
        where = "" if release is None else " for '%s'" % release
        if type(text) != type(""):
            raise self._error(
                "'apt-preferences'%s shall be a string, as written in a file "
                "under /etc/apt/preferences.d" % where)
        if len(text.strip()) == 0:
            raise self._error("'apt-preferences'%s is empty" % where)
        return text

    # What this package's build may install on the release being built,
    # which is what it said for that release or what it said for all of
    # them. Nothing, for a package that named neither.
    def preferences_for(self, release):
        if release in self.apt_preferences:
            return self.apt_preferences[release]
        return self.apt_preferences.get(ANY_RELEASE)

    # Who this rebuild is for, as one role or a list of them. Whether it
    # was written down at all is kept beside it: a scope nothing asked for
    # is one a dependent may widen, and one the specification wrote is an
    # answer rather than a default.
    def _parse_scope(self, spec):
        scope = spec.get("scope")
        self.scoped = scope is not None
        if scope is None:
            return list(DEFAULT_SCOPE)
        if type(scope) == type(""):
            scope = [scope]
        if type(scope) != type([]) or any(type(r) != type("") for r in scope):
            raise self._error(
                "'scope' shall be a role or a list of them, one of %s"
                % ", ".join(SCOPES))
        for role in scope:
            if role not in SCOPES:
                raise self._error(
                    "'scope' has no '%s' role, expected one of %s"
                    % (role, ", ".join(SCOPES)))
        if len(scope) == 0:
            raise self._error(
                "'scope' is empty: a package is rebuilt for someone, and a "
                "package for no one is one to leave out")
        return sorted(set(scope))

    # A sha256 as it is written down: sixty-four hexadecimal digits,
    # checked here so a truncated one is reported against the file that
    # holds it rather than against the download it fails to match.
    def _parse_digest(self, spec, key):
        digest = spec.get(key)
        if digest is None:
            return None
        if type(digest) != type("") or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise self._error(
                "'%s' shall be a sha256, which is 64 hexadecimal digits -- "
                "quote it if it happens to be all digits, or YAML reads it as "
                "a number" % key)
        return digest.lower()

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

    # Patches and fragments are given relative to the YAML file that listed
    # them, which is not necessarily the one being built: specifications are
    # assembled from several files through 'requires'. They arrive here
    # already resolved against it, since a package may be described by more
    # than one file and each names its own files.
    # The file that wrote a setting, named as its path -- 'source',
    # 'extends.kernel.upstream'. None for a setting nothing wrote down.
    def origin_of(self, setting):
        return self.origins.get(setting)

    def patch_files(self):
        return self._files(self.patches)

    def kernel_fragment_files(self):
        return self._files(self.kernel_fragments)

    def kernel_derived_flavour_files(self):
        files = []
        for derived in (self.kernel_derived_flavours or {}).values():
            for fragments in derived.values():
                files += fragments
        return self._files(files)

    # Every local file this package's own spec entry names -- patches,
    # kernel config, derived-flavour fragments. One definition, so a
    # caller (the digest below, or anything else) never has to repeat
    # the concatenation, and a future fourth category is one line here.
    def referenced_files(self):
        return (self.patch_files() + self.kernel_fragment_files()
               + self.kernel_derived_flavour_files())

    def _files(self, names):
        return [os.path.normpath(n) for n in names]


# Where the "this has been built" markers live, inside the repository so
# they are thrown away with it. Hidden, and not .debs, so neither apt nor
# dpkg-scanpackages pays them any attention.
STAMPS = ".stamps"

# Where each stamp's digest excerpt lives -- named after it, but never in
# the same directory: '_previous()' and 'cache.py' 's own stamp lookup
# both list STAMPS and match by name prefix, so a sibling file living
# there too would be misread as a stamp itself.
STAMPS_SPEC = ".stamps-spec"

# Where the container that clones finds the ssh agent of the user seine
# runs as, and the hosts that user already trusts. Fixed names rather than
# the paths they have on the host, which nothing here needs to preserve.
SSH_AUTH_SOCK = "/ssh-agent/sock"
SSH_KNOWN_HOSTS = "/root/.ssh/known_hosts"

class Builder:
    # 'redact_patterns' is optional: most callers (most tests among them)
    # have no 'redact:' section to apply, and passing '[]' everywhere for
    # that would be pure noise.
    def __init__(self, distro, options, builderImage, redact_patterns=None):
        self.builderImage = builderImage
        self.distro = distro
        self.options = options
        self._redact_patterns = redact_patterns or []
        # The ABI each rebuilt kernel gave itself, by package name, read
        # off its regenerated debian/control as it was prepared. What a
        # module built against that kernel has to be named for, and not a
        # thing anybody can predict: the UNRELEASED changelog a local
        # rebuild carries changes the shape of it.
        self.abinames = {}
        # What each 'apt://linux-headers-<flavour>' metapackage turned out
        # to name, by (architecture, reference). Asked of apt once and
        # kept for the run: it is the archive's answer, so caching it
        # between runs would be caching the thing that moves.
        self.metapackages = {}
        # Every package this build was asked for, so that a module can be
        # told what the kernel it names resolved to. A module is described
        # by its own entry and by the kernel's, which is a different one.
        # Set once, by tasks() -- see '_tasked' below.
        self.packages = []
        # tasks() fills this and 'metapackages' in once; a shared Builder
        # needs that call to use the union of every image's packages, not
        # one per image, or a module would resolve against only the last
        # list. Enforced in tasks() itself -- asking again with the same
        # list still answers.
        self._tasked = None
        # Which kernels' cross headers this run has already seen to, so
        # that several modules against one kernel do not each decide to
        # make it.
        self._crossed = set()
        # Held while unpacking a chroot and while rewriting the
        # repository, both of which are shared by every package being
        # built at the same time. Compiling is not: that is the part
        # worth doing beside each other.
        self._chroots = threading.Lock()
        # What each package's fetch left behind, until the last build that
        # reads it is done, and what each build produced, until it is
        # published. Both are reached from several tasks at once, hence
        # the lock over the first -- the second is only ever written and
        # read under a key one task owns.
        self._sources = {}
        # The source package each fetch produced, kept apart from the
        # working directory it was built in: that directory belongs to the
        # builds and goes when the last of them is done, which is before
        # anything is published.
        self._source_packages = {}
        self._holding = {}
        self._workdirs = threading.Lock()
        # A fetch shared by every prepare:<name> task naming it -- see
        # _fetch()/_fetch_upstream()/_prepare_source(). One fetch task per
        # key, so nothing here needs a lock to write; freed by count
        # ('_shared_taken' catching up with '_shared_wanted') once read,
        # since a package's build can run long after every prepare task
        # sharing its fetch has already taken a copy.
        self._shared_fetches = {}
        self._shared_wanted = {}
        self._shared_taken = {}
        self._shared_lock = threading.Lock()
        self._built = {}
        self._repository = threading.Lock()
        # The key this build signs with, if it was given one. Asked for
        # here so a key that is not there stops the build now rather than
        # after it has compiled.
        self.signer = signing.signer(options)
        if self.signer is not None:
            self.signer.fingerprint()

    # Cores for one package build: what --parallel said, or the machine
    # divided by how many builds may run at once.
    def parallel(self, package):
        parallel = self.options.get("parallel")
        if parallel is None:
            jobs = max(1, self.options.get("jobs", 1))
            parallel = max(1, (os.cpu_count() or 1) // jobs)
        for option in package.options:
            if option.startswith("parallel="):
                parallel = option
                break
        if type(parallel) == type(""):
            parallel = int(parallel.split("=")[1])
        return max(1, parallel)

    def fetch(self, package, workdir):
        volumes = [(workdir, WORKDIR)]
        # A cross headers package reads from the repository this build is
        # filling, since a kernel built here has its source nowhere else.
        if module.is_cross_package(package):
            # A pair, not the '-v host:container' pair of arguments
            # build_volumes() makes: what exec() takes is the former and
            # it says nothing about being handed the latter.
            volumes.append((repository(self.distro), REPOSITORY))
            self.builderImage.exec(
                module._fetch_cross_args(self, package,
                                         self.distro["architecture"]),
                volumes=volumes, workdir=WORKDIR)
            return self._source_dir(package.name, workdir)

        ssh_volumes, environment = self._ssh(package)
        args, volumes = self._offline_fetch(
            self._fetch_args(package), package, volumes + ssh_volumes)
        self.builderImage.exec(
            args, volumes=volumes, workdir=WORKDIR, environment=environment)
        if package.scheme == "https":
            self._verify(package, workdir, os.path.basename(package.source),
                         package.sha256, "sha256")
        return self._source_dir(package.source, workdir)

    # What a specification said the bytes would be, against what arrived.
    #
    # Checked here rather than in the container that fetched them: the
    # file lands in a directory seine bind-mounted, so it can be read
    # directly, and a container asked to verify itself proves nothing.
    #
    # Only for what is fetched over http: an apt source is checked against
    # the archive's signed index, and a git revision is the hash of what
    # it names, so both already answer for themselves.
    def _verify(self, package, workdir, name, expected, setting):
        path = os.path.join(workdir, name)
        if os.path.isfile(path) == False:
            return

        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
        found = digest.hexdigest()
        if expected is None:
            self._unvouched(package, name, found, setting)
            return
        if found != expected:
            raise ValueError(
                "package '%s': '%s' is not what '%s' says it would be\n"
                "  expected %s\n"
                "  fetched  %s\n"
                "Either the file changed where it is served from, or it was "
                "changed on the way here."
                % (package.source, name, setting, expected, found))

    # Nothing said what these bytes would be, so say what they were --
    # with the file to write it in, which for a package described by
    # several is the one that carries the URI rather than the one that
    # named the package.
    def _unvouched(self, package, name, found, setting):
        where = package.origin_of(
            "extends.kernel.upstream" if setting == "upstream-sha256"
            else "source")
        print("warning: nothing vouches for '%s'" % name)
        print("  it hashes to %s" % found)
        print("  add '%s: %s'%s" % (
            setting, found, " to %s" % where if where else ""))


    # The clone runs in the container, so what authenticates it goes in too:
    # the agent's socket and the hosts already known. The keys stay outside
    # -- this container runs build scripts fetched from elsewhere, with more
    # of the namespace privileges than the others seine builds. A key the
    # agent does not hold cannot be used: 'ssh-add' it before building.
    def _ssh(self, package):
        if package.parameters.get("protocol") != "ssh":
            return [], None

        sock = os.environ.get("SSH_AUTH_SOCK")
        if sock is None:
            raise ValueError(
                "'%s' is fetched over ssh but SSH_AUTH_SOCK is unset: there "
                "is no agent to authenticate with" % package.source)

        volumes = [(sock, SSH_AUTH_SOCK)]
        # An unknown host key stops the clone with a prompt no one can
        # answer. Trusting whatever answers is not the fix: without a
        # known_hosts file the clone fails, which is right for a host the
        # user has never met.
        known_hosts = os.path.expanduser("~/.ssh/known_hosts")
        if os.path.isfile(known_hosts):
            volumes.append((known_hosts, SSH_KNOWN_HOSTS))
        return volumes, {"SSH_AUTH_SOCK": SSH_AUTH_SOCK}


    def _fetch_args(self, package):
        if package.scheme == "apt":
            source = package.source_name
            if package.version is not None:
                source = "%s=%s" % (package.source_name, package.version)
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
        args += [url, package.source_name]
        # The revision is what the specification pinned; the branch only
        # says where to look for it.
        return ["sh", "-c", "%s && cd %s && git checkout --detach %s" % (
            " ".join(args), package.source_name, package.parameters["rev"])]

    # 'apt-get source' reads whatever the builder image's own sources.list
    # says -- which, under 'apt-pull-mode: offline', no longer carries the
    # suite it needs at all: BuilderImage._sources() leaves an offline
    # suite out of what it bakes rather than pointing it at a vendor
    # repository that 'seine vendor' may since have refreshed. Written here
    # instead, into the same throwaway container this fetch already runs
    # in, right before the command that needs it.
    #
    # https:// and git:// sources never touch the image's own apt at all,
    # so they pass through untouched.
    def _offline_fetch(self, args, package, volumes):
        if package.scheme != "apt":
            return args, volumes
        suites = offline_suites(self.distro)
        if len(suites) == 0:
            return args, volumes
        from seine import vendor
        volumes = volumes + [(vendor.deploy_repository(suite), vendor_mountpoint(suite))
                             for suite in suites]
        script = offline_apt_script(self.distro, feeds(self.distro),
                                    "/etc/apt/sources.list.d/seine.list",
                                    offline=True)
        script += "apt-get update -qqy; "
        script += shlex.join(args)
        return ["sh", "-c", script], volumes

    # Every scheme leaves exactly one unpacked source tree behind, next to
    # the .dsc/tarballs it came from, but only apt-get source and dget name
    # it after the upstream version rather than the package. Directories we
    # put there ourselves are hidden, and skipped here.
    def _source_dir(self, source, workdir):
        directories = [d for d in sorted(os.listdir(workdir))
                       if os.path.isdir(os.path.join(workdir, d))
                       and not d.startswith(".")]
        if len(directories) != 1:
            raise ValueError(
                "fetching '%s' produced %d source directories, expected one: %s"
                % (source, len(directories), ", ".join(directories)))
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
        # A module's tree has no changelog to read one from -- the one it
        # will have is about to be written, and dated with what this
        # returns. What it has instead is a revision, and
        # the date that revision was made is a property of the source
        # rather than of the machine or the day, which is what a date a
        # build is pinned to has to be.
        if package.module:
            return self._committed(package, sourcedir)
        source = os.path.join(WORKDIR, os.path.basename(sourcedir))
        timestamp = self.builderImage.output(
            ["dpkg-parsechangelog", "-STimestamp"],
            volumes=[(os.path.dirname(sourcedir), WORKDIR)], workdir=source)
        return int(timestamp.strip())

    # When the revision this was fetched at was made, which for a git
    # tree is the one date about it that is neither the machine's nor
    # today's. A tree that came from anywhere else -- or a clone with the
    # history dropped -- has nothing to say, and the epoch falls back to
    # a fixed one rather than to now: a date that moves is a source
    # package that differs between two builds of the same specification.
    def _committed(self, package, sourcedir):
        source = os.path.join(WORKDIR, os.path.basename(sourcedir))
        try:
            when = self.builderImage.output(
                ["git", "-C", source, "log", "-1", "--format=%ct"],
                volumes=[(os.path.dirname(sourcedir), WORKDIR)],
                workdir=source)
            return int(when.strip())
        except Exception:
            return FALLBACK_EPOCH






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



    # Cross-compiling is the default whenever the target is not the machine
    # seine runs on: emulating a foreign architecture for a whole package
    # build is slow enough to be worth avoiding, even though not every
    # package can be cross-built. 'cross: false' asks for the slow, always
    # working way instead.
    def cross(self, package, architecture):
        # Nothing to cross to: a host package is built for the machine
        # already running the compiler, whatever the specification said.
        if architecture == HOST_ARCH:
            return False
        if package.cross is not None:
            return package.cross
        return True

    # The architecture of the chroot the build runs in: the host's when
    # cross-compiling, since that is what runs the compiler, and the
    # package's own when emulating it.
    def chroot_architecture(self, package, architecture):
        if self.cross(package, architecture):
            return HOST_ARCH
        return architecture






    # The cross headers a build is about to need, made if they are not
    # made already.
    #
    # Once per kernel and not once per module: the second module built
    # against a kernel finds the first one's package in the repository
    # and its stamp beside it, so nothing is built twice however many
    # modules there are. The lock is for the builds seine runs beside
    # each other, which would otherwise each decide to make it.
    def _cross_headers_built(self, package, architecture):
        if package.module == False:
            return
        if self.cross(package, architecture) == False:
            return
        for kernel in module.resolved_kernels(self, package, architecture,
                                              self.packages):
            with self._chroots:
                if kernel.release in self._crossed:
                    continue
                self._crossed.add(kernel.release)
            cross = module._cross_package(kernel, package.index)
            stamp = self.stamp(cross, HOST_ARCH)
            if os.path.isfile(stamp):
                continue
            print("building '%s' for %s" % (cross.name, HOST_ARCH))
            self._rebuild(cross, HOST_ARCH, stamp)
            self._deploy(cross, [HOST_ARCH])








    # The architectures a package is built for, which is what 'scope' says.
    # Collapsed to one when the image is of the machine's own architecture:
    # 'both' then asks for the same source, built the same way, into the
    # same repository, and building it twice would only race with itself.
    def architectures(self, package):
        wanted = []
        if "target" in package.scope:
            wanted.append(self.distro["architecture"])
        if "host" in package.scope:
            wanted.append(HOST_ARCH)
        return sorted(set(wanted))

    # Which of a package's builds produces its 'Architecture: all'
    # binaries, when any of them can.
    #
    # Nominated rather than left to chance. Those binaries are one package
    # under one filename, and one repository holds every architecture, so
    # two builds of a source that both build them write the same file --
    # and which of them landed last would decide what an image installs.
    # Nominating one is also what Debian does: arch-all is built once, by
    # one buildd, not once per architecture.
    #
    # A native build for preference. sbuild hands a cross build '-B' of
    # its own accord, and with reason: an architecture-independent binary
    # is sometimes made by running something that was just built, which a
    # cross build has no way to run.
    #
    # Between two native builds -- which is what 'cross: false' on a
    # package built for two architectures leaves -- the machine's own
    # architecture wins, since the other is being emulated.
    #
    # But a preference is all it is. When every build of a package is a
    # cross build, which is the ordinary shape of a specification building
    # for one board on somebody's laptop, the alternative to asking a
    # cross build for them is not getting them: the .debs would be
    # published without the arch-all packages beside them, and an image
    # installing one would take the distribution's copy of a package it
    # asked to have rebuilt, looking exactly as it should.
    #
    # So the cross build is asked. Most packaging manages it -- an
    # arch-indep binary is commonly documentation or configuration -- and
    # packaging that does not now fails loudly instead of quietly
    # producing less, with 'cross: false' as the way to say so: it builds
    # under emulation, natively, where the question does not arise.
    def indep_architecture(self, package):
        architectures = self.architectures(package)
        natives = [a for a in architectures if self.cross(package, a) == False]
        for candidate in [natives, architectures]:
            if HOST_ARCH in candidate:
                return HOST_ARCH
            if len(candidate) > 0:
                return candidate[0]
        return None

    # What a package's steps are called. The architecture is only added
    # when the package is built for more than one: the graph of a
    # specification that builds for the image alone -- which is every
    # specification that says nothing about 'scope' -- reads as it always
    # has, and 'before'/'after' go on naming a package rather than a build
    # of one.
    def label(self, package, architecture):
        if len(self.architectures(package)) > 1:
            return "%s:%s" % (package.name, architecture)
        return package.name

    # Turns the prepared source tree back into a source package, and says
    # what it is called. Patches added to a quilt series are applied by
    # dpkg-source on the way, and sbuild wants a .dsc anyway.
    #
    # Done once per package rather than once per architecture: a Debian
    # source package is not built for an architecture, it describes every
    # one the packaging supports. Both builds of a 'scope: both' package
    # are then demonstrably of the same source rather than of two trees
    # prepared the same way.
    def source_package(self, package, sourcedir):
        workdir = os.path.dirname(sourcedir)

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
        return dsc[0]

    # A source package is the .dsc and the files it lists, which is not
    # everything lying beside it: the directory a source was fetched into
    # also holds what it was fetched as -- the distribution's own .debian
    # tarball, superseded by the one just written, and for a graft the
    # tarball the tree came in. Taking what the .dsc names is the only
    # answer that stays right, and it is the answer apt-ftparchive checks
    # against when it reads one.
    def source_files(self, workdir, dsc):
        files = [dsc]
        with open(os.path.join(workdir, dsc), "r") as f:
            listing = False
            for line in f:
                if line.startswith(" ") == False:
                    # A field of its own ends the file list; 'Files' and
                    # the Checksums fields all name the same files, so the
                    # first of them is enough.
                    listing = line.startswith("Files:")
                    continue
                if listing == False:
                    continue
                named = line.split()
                if len(named) == 3 and named[2] not in files:
                    files.append(named[2])
        return files

    # Builds one prepared source package for one architecture and leaves
    # the .debs, .changes and .buildinfo it produced in that
    # architecture's host-side repository.
    def build(self, package, workdir, dsc, epoch, architecture, output):
        volumes = [(workdir, WORKDIR), (self.repository(), REPOSITORY),
                   (output, OUTPUT)]

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
        if self.cross(package, architecture):
            args += ["--build=%s" % HOST_ARCH,
                     "--host=%s" % architecture]
        else:
            args += ["--arch=%s" % architecture]

        # Said either way rather than left to sbuild's default, which is
        # to build them whenever the build is a native one: a package
        # built for two architectures natively would then build them
        # twice, into one repository, under one name.
        args += ["--arch-all" if architecture == self.indep_architecture(package)
                 else "--no-arch-all"]
        # How much of the machine this build may use. sbuild works it out
        # from the machine itself, which is right for the one build that
        # was ever running at a time and wrong the moment there are
        # several, each helping itself to every core. So the cores are
        # divided rather than handed out whole, unless the specification
        # says otherwise -- a package whose build is broken in parallel
        # says 'parallel=1' in its options, and a machine that is not
        # CPU-bound can be told to oversubscribe.
        args += ["--jobs=%d" % self.parallel(package)]

        # 'cross' is one of dpkg's own build profiles, and sbuild sets it
        # for a cross build -- but only while it is choosing the profiles
        # itself. Naming any profile takes that decision over, and a
        # specification naming 'nocheck' has no reason to know that it has
        # thereby told the packaging it is building natively. Debian's
        # kernel build-depends on the cross compiler under '<cross>' and
        # the native one under '<!cross>', so without the profile a cross
        # build asks its chroot for a compiler not installable there.
        profiles = list(package.profiles)
        if self.cross(package, architecture) and "cross" not in profiles:
            profiles.append("cross")
        if len(profiles) > 0:
            args += ["--profiles=%s" % ",".join(profiles)]

        # A package may build-depend on one rebuilt before it. The chroot
        # reaches the repository through the bind mount configured in the
        # builder image, so it is an ordinary sources.list entry there --
        # same repository, same pin, same behaviour as everywhere else.
        #
        # One entry covers every architecture, which is what makes a
        # 'scope: host' rebuild useful: a cross build's chroot needs what
        # it build-depends on to be of the *host* architecture, since that
        # is what runs there, and apt takes from this index what the
        # architecture it was asked about can use.
        # The chroot reads the key where the repository is mounted rather
        # than being given a copy of its own: sbuild bind-mounts the
        # repository in, the key is in it, and a keyring installed by a
        # setup command would have to be installed before the update that
        # needs it.
        signed = "[trusted=yes]"
        if self.signer is not None:
            signed = "[signed-by=%s/%s]" % (REPOSITORY, self.signer.keyring())
        args += ["--extra-repository=deb %s file:%s ./" % (signed, REPOSITORY),
                 "--chroot-setup-commands=%s" % apt_preferences_command()]

        # And what this package asked for, in front of its own build and no
        # other. The chroot is unpacked for one build and thrown away, so
        # naming a version here pins what that build is compiled against
        # without deciding anything for the package beside it -- which is
        # what makes it usable at all: a rebuilt kernel puts a
        # linux-libc-dev in the repository that sorts above the release's,
        # and busybox does not build against headers six versions newer
        # than the ones its source expects.
        preferences = package.preferences_for(self.distro["release"])
        if preferences is not None:
            args += ["--chroot-setup-commands=%s"
                     % sbuild_command(package_preferences_command(preferences))]

        args += ["%s/%s" % (WORKDIR, dsc)]

        # sbuild bind-mounts a handful of device nodes into its chroot and
        # complains about each one the container does not have. podman only
        # creates /dev/console when it allocates a terminal, which we have
        # no other use for, so point it at /dev/null: nothing in a build
        # has any business writing to a console, and the alternative is a
        # warning per invocation drowning the output that matters.
        # Without a terminal, libc's stdio block-buffers, so the log
        # lags well behind the build. 'stdbuf' (LD_PRELOAD) does not fix
        # it: sbuild only forwards a short env allowlist (below) into
        # the chroot it builds in, dropping LD_PRELOAD before
        # dpkg-buildpackage/gcc ever see it. 'exec.tty' asks podman for
        # a real terminal instead -- isatty() survives that boundary
        # since it is a property of the descriptor, not the environment.
        script = "ln -sf /dev/null /dev/console; exec %s" % shlex.join(args)

        # Run from the output directory so sbuild drops what it built
        # there, where it is this build's and nobody else's.
        self.builderImage.exec(
            ["sh", "-c", script],
            architecture=self.chroot_architecture(package, architecture),
            volumes=volumes, workdir=OUTPUT, environment=environment, tty=True)

    def repository(self):
        return repository(self.distro)

    # Turns the directory the builds dropped their .debs in into something
    # apt can read: a flat repository with a Packages index. Both the plain
    # and the gzipped index are written, as apt looks for several
    # compressions and complains about each one it does not find.
    # apt-ftparchive rather than dpkg-scanpackages, for the cache: the
    # index is rewritten after every package that builds, and
    # dpkg-scanpackages hashes every .deb in the repository each time.
    # That is the whole repository re-read per package -- and a kernel's
    # debug package alone is larger than most images, so a specification
    # building several of them spends longer hashing what it already
    # hashed than building some of what it hashes.
    #
    # The cache is keyed on what a file is and when it changed, so only
    # what arrived since the last time is read. It lives beside the index
    # it describes; apt has no interest in files it was not pointed at.
    # The Sources index beside it describes the source packages the builds
    # were made from, which are published with them. Nothing seine runs
    # reads it: a build fetches its sources from the distribution, not from
    # here. It is written because a repository holding source packages
    # nothing can find is not holding them in any useful sense -- what it
    # is for is the machine handed a cache, and anything asking what a
    # modified binary was built from.
    # 'cached' is false when a package took the place of one already in
    # the repository, which happens whenever a rebuild produces the same
    # version -- a change to the packaging seine writes, or to anything
    # else the digest counts but the version does not.
    #
    # apt-ftparchive keeps what it has already read in a database, and
    # for a file replaced under its own name it goes on describing the
    # one before: an index announcing the size and hash of a package that
    # is no longer there, which apt fetches and refuses as a hash
    # mismatch. Rather than repair it, the database is set aside for that
    # one run, since what it holds about that file is wrong and it cannot
    # be told which.
    def index(self, cached=True):
        if cached == False:
            stale = os.path.join(self.repository(), INDEX_CACHE)
            if os.path.isfile(stale):
                os.unlink(stale)

        # The Release file goes last and is made fresh every time: it
        # holds the hashes of the indices above it, and the signatures
        # beside it are of the file as it was. Removing them first is what
        # stops apt-ftparchive from hashing yesterday's signature into
        # today's Release.
        for name in ["Release", "Release.gpg", "InRelease"]:
            path = os.path.join(self.repository(), name)
            if os.path.isfile(path):
                os.unlink(path)

        script = ("apt-ftparchive --db " + INDEX_CACHE + " packages . "
                  "> Packages && gzip -9 -c Packages > Packages.gz && "
                  "apt-ftparchive sources . "
                  "> Sources && gzip -9 -c Sources > Sources.gz")
        if self.signer is not None:
            script += " && apt-ftparchive release . > Release"
        self.builderImage.exec(
            ["sh", "-c", script],
            volumes=[(self.repository(), REPOSITORY)], workdir=REPOSITORY)

        # Signed here rather than in the container that wrote it: gpg runs
        # on this machine, where the agent holding the key is.
        if self.signer is not None:
            self.signer.sign_release(os.path.join(self.repository(), "Release"))

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
    def stamp(self, package, architecture=None, depends=None):
        architecture = architecture or self.distro["architecture"]
        digest = hashlib.sha256()
        # Sets, not sequences: which patches are kept and which are dropped
        # does not depend on the order they were written in, and a digest
        # that says otherwise costs a kernel build to reorder two lines.
        for part in [str(package.source),
                     ",".join(package.profiles),
                     ",".join(package.options),
                     str(self.cross(package, architecture)),
                     str(package.source_date_epoch),
                     package.revision,
                     str(package.kernel_featureset),
                     str(package.kernel_flavour),
                     # Every base/name pair, so renaming one or moving it
                     # to derive from a different base is a rebuild even
                     # when every fragment's content stays the same; the
                     # fragments' own content is folded in below, with
                     # the ones 'kernel_fragments' names.
                     ",".join(sorted("%s/%s" % (base, name)
                             for base, derived in (package.kernel_derived_flavours or {}).items()
                             for name in derived)),
                     # 'kernel_configs' is written straight into the
                     # fragment, not read from a file 'referenced_files()'
                     # would catch, so it has to be folded in by hand.
                     # Not sorted: '_write_configs' writes groups in this
                     # order, and two touching the same symbol settle it
                     # by which is written last.
                     "\n".join("%s:%s" % (name, "\n".join(lines))
                              for name, lines in package.kernel_configs.items()),
                     str(package.kernel_abi_suffix),
                     str(package.kernel_upstream),
                     str(package.kernel_upstream_sha256),
                     str(package.sha256),
                     # What the build is allowed to install decides what
                     # comes out of it, so a changed pin is a rebuild. What
                     # this release is pinned to rather than the whole
                     # mapping: another release's pin has no bearing on
                     # what comes out here.
                     str(package.preferences_for(self.distro["release"])),
                     # str(), not a join: keeping nothing and saying
                     # nothing are different answers, and 'None' and '[]'
                     # are what tells them apart.
                     str(package.kernel_keep_patches
                         if package.kernel_keep_patches is None
                         else sorted(package.kernel_keep_patches)),
                     ",".join(sorted(package.kernel_drop_patches)),
                     ",".join(sorted(package.kernel_build_files)),
                     self.distro["source"],
                     self.distro["release"],
                     architecture,
                     # 'offline=': what fetch()/the chroot actually read
                     # from flips between the network and the local
                     # vendor repository with 'apt-pull-mode', so a
                     # rebuild already stamped under one has to be told
                     # apart from one done under the other, the same as
                     # any other feed change already is.
                     "\n".join(apt_sources(self.distro, sources=True,
                                           offline=len(offline_suites(self.distro)) > 0)),
                     self.chroot_architecture(package, architecture),
                     # Who signed it. The .dsc and the .changes carry
                     # their signature inside them, so a build signed by
                     # another key -- or by none -- produced different
                     # files, however identical the .debs beside them
                     # are. Without this, changing the key would leave a
                     # repository serving a source package signed by a
                     # key it no longer carries, and adding one to a
                     # specification already built would sign the index
                     # over sources that are not signed at all.
                     #
                     # It follows that a cache built by somebody else is
                     # rebuilt here rather than adopted, which is the
                     # honest answer: their signature is not ours to
                     # publish.
                     str(self.signer.fingerprint()
                         if self.signer is not None else None),
                     # Whether this is the build that makes the package's
                     # architecture-independent binaries, which is decided
                     # by what the *other* builds are: widening 'scope'
                     # can move the job to another architecture, and the
                     # build that no longer has it produces different
                     # files than the stamp beside it says it did.
                     str(architecture == self.indep_architecture(package)),
                     # Which kernels this module is built against, as the
                     # specification named them. A kernel added to the
                     # list, or taken out of it, changes what binary
                     # packages come out, so it has to change the stamp.
                     #
                     # For a kernel this specification builds, that is
                     # only half of it: what matters is the ABI, which is
                     # not knowable here. The dependency on the kernel
                     # carries it -- a module is built after the kernels
                     # it names, so the kernel's own digest is folded in
                     # below, and a kernel rebuilt for any reason rebuilds
                     # the modules on it.
                     ",".join(sorted(package.module_kernels.get(architecture, []))),
                     # And what the ones naming a moving target turned out
                     # to be. Debian moves a kernel's ABI in a security
                     # update, which leaves 'linux-headers-amd64' pointing
                     # somewhere new while the specification reads exactly
                     # as it did. Without this the modules would still be
                     # the ones built for the ABI before it.
                     ",".join("%s=%s" % (reference, headers)
                              for (a, reference), headers
                              in sorted(self.metapackages.items())
                              if a == architecture),
                     str(package.module_build),
                     str(package.module_target),
                     ",".join(package.module_build_depends),
                     ",".join(package.module_runtime_depends),
                     ",".join(sorted(package.module_modules)),
                     ",".join("%s=%s" % (name, package.module_make_vars[name])
                              for name in sorted(package.module_make_vars)),
                     str(package.upstream_version)]:
            digest.update(part.encode())
        # Patches and kernel configuration fragments count by content, not
        # by name: editing one without touching the specification has to be
        # enough to ask for a rebuild, all the more for a kernel, where the
        # alternative is silently keeping one built from the fragment as it
        # used to read.
        for path in package.referenced_files():
            with open(path, "rb") as f:
                digest.update(f.read())

        # A grafted kernel is built from what the rules kept of the
        # distribution's series, so those rules decide what comes out as
        # surely as a fragment does. By content, for the same reason:
        # editing them has to be enough to ask for a rebuild, or the
        # kernel goes on being the one yesterday's rules produced. Only
        # for a graft -- an ordinary rebuild never consults them.
        if package.kernel_upstream is not None:
            digest.update(kernel.kernel_rules().content)
            digest.update(str(kernel.GRAFT_VERSION).encode())

        # And a module is built by the packaging seine writes for it, so
        # that packaging decides what comes out as surely as a patch
        # does. By content, for the same reason: editing the rules has to
        # be enough to ask for a rebuild, or the modules go on being the
        # ones yesterday's rules produced.
        if package.module:
            digest.update(module.module_packaging()[1])

        # A cross headers package is of one kernel and made by one
        # packaging, and neither is anything the settings above describe:
        # it was made up rather than asked for. The kernel's release
        # changes when that kernel does, which is what has to rebuild it
        # -- a grafted kernel rebuilt is a new ABI, and headers left
        # describing the one before it would have modules built against
        # a kernel that is not there.
        if module.is_cross_package(package):
            digest.update(package.cross_kernel.release.encode())
            digest.update(package.cross_kernel.headers.encode())
            digest.update(module.cross_packaging()[1])

        # A package built against another has to be rebuilt when that one
        # changes: it was compiled and linked against what that package
        # installed. Folding the dependency's digest in says so, and says
        # it transitively, since that digest already carries its own.
        for name in sorted(depends or {}):
            digest.update(depends[name].encode())

        # The architecture is in the name, not only in the digest: one
        # repository holds every architecture's stamps, and what a stamp
        # is looked up by is the build it belongs to. Without it the
        # amd64 build of a package would find the arm64 build's stamp
        # when asking what it left behind last time, and take its .debs
        # away as superseded.
        return os.path.join(self._stamps(), "%s_%s_%s"
                            % (package.name, architecture,
                               digest.hexdigest()[:16]))

    # A file 'stamp()' hashed, written the way the specification wrote
    # it -- relative to whichever file actually declared it
    # ('origin_of()'), not the absolute path 'referenced_files()' deals
    # in. A stamp is meant to travel ('cache export'/'import'); an
    # absolute path baked into one would only be true on the machine
    # that wrote it.
    #
    # 'derived-flavours' can (rarely) be merged from two files at once,
    # and origin tracking only remembers one file per setting -- so a
    # fragment from the file that lost that race resolves against the
    # wrong directory. Still strictly better than an absolute path, and
    # not worth chasing until it actually bites someone.
    def _portable_path(self, package, setting, path):
        origin = package.origin_of(setting)
        if origin is None:
            return path
        return os.path.relpath(path, os.path.dirname(origin))

    # The specification content behind one build, redacted and with
    # every file path made portable -- what 'cache' shows beside a
    # stamp so a person or the AI chat can tell what a cached build
    # actually has in it without re-deriving it from the live spec.
    #
    # Deliberately not every field 'stamp()' hashes: the release/
    # architecture/signer/cross-build context is either already in the
    # stamp's own name or is build environment, not specification
    # content -- what belongs here is what a person reading the
    # specification itself would recognise.
    def digest_excerpt(self, package):
        excerpt = {"source": package.source, "revision": package.revision}
        if package.profiles:
            excerpt["profiles"] = sorted(package.profiles)
        if package.options:
            excerpt["options"] = sorted(package.options)
        if package.sha256:
            excerpt["sha256"] = package.sha256
        if package.patches:
            excerpt["patches"] = [
                self._portable_path(package, "patches", p)
                for p in package.patch_files()]

        extends = {}
        if package.kernel:
            extends["kernel"] = self._kernel_excerpt(package)
        if package.module:
            extends["module"] = self._module_excerpt(package)
        if extends:
            excerpt["extends"] = extends

        return redact(excerpt, self._redact_patterns)

    def _kernel_excerpt(self, package):
        settings = {}
        if package.kernel_flavour:
            settings["flavour"] = package.kernel_flavour
        if package.kernel_featureset != kernel.DEFAULT_FEATURESET:
            settings["featureset"] = package.kernel_featureset
        if package.kernel_fragments:
            settings["fragments"] = [
                self._portable_path(package, "extends.kernel.fragments", p)
                for p in package.kernel_fragment_files()]
        if package.kernel_configs:
            settings["configs"] = package.kernel_configs
        if package.kernel_derived_flavours:
            # Already absolute+normalised ('_resolve_files()' resolves
            # these fragments too, in place, at load time) -- no second
            # pass through '_files()' needed here.
            settings["derived-flavours"] = {
                base: {name: [self._portable_path(
                                  package, "extends.kernel.derived-flavours", p)
                              for p in fragments]
                       for name, fragments in derived.items()}
                for base, derived in package.kernel_derived_flavours.items()}
        if package.kernel_upstream:
            settings["upstream"] = str(package.kernel_upstream)
        if package.kernel_abi_suffix:
            settings["abi-suffix"] = package.kernel_abi_suffix
        if package.kernel_keep_patches is not None:
            settings["keep-patches"] = sorted(package.kernel_keep_patches)
        if package.kernel_drop_patches:
            settings["drop-patches"] = sorted(package.kernel_drop_patches)
        return settings

    def _module_excerpt(self, package):
        settings = {"build": package.module_build, "target": package.module_target}
        if package.module_modules:
            settings["modules"] = sorted(package.module_modules)
        if package.module_build_depends:
            settings["build-depends"] = sorted(package.module_build_depends)
        if package.module_runtime_depends:
            settings["runtime-depends"] = sorted(package.module_runtime_depends)
        if package.module_make_vars:
            settings["make-vars"] = dict(package.module_make_vars)
        return settings

    # Where an excerpt lives: same basename as its stamp, in the sibling
    # directory ('STAMPS_SPEC') rather than beside it, so finding one
    # from the other is swapping a directory, not appending a suffix
    # '_previous()' 's own prefix match would then pick up too.
    def _excerpt_path(self, stamp):
        return os.path.join(self._stamps_spec(),
                            "%s.spec" % os.path.basename(stamp))

    def _record_excerpt(self, stamp, package):
        with open(self._excerpt_path(stamp), "w") as f:
            yaml.dump(self.digest_excerpt(package), f, sort_keys=False)

    # The stamp of every package it is built for, in build order, each one
    # folding in the stamps of what it is built after. The order is what
    # makes that possible: a package's dependencies have been given their
    # digest before it is given its own.
    #
    # A dependency is followed within an architecture rather than across
    # them: what a build was compiled and linked against is the
    # dependency's build for the same architecture, and the host build of
    # a package has no bearing on what its target build produced.
    def stamps(self, packages):
        digests = {}
        stamps = []
        for package in packages:
            for architecture in self.architectures(package):
                depends = {d.name: digests[(d.name, architecture)]
                           for d in getattr(package, "depends", [])
                           if (d.name, architecture) in digests}
                stamp = self.stamp(package, architecture, depends)
                digests[(package.name, architecture)] = \
                    os.path.basename(stamp).rsplit("_", 1)[1]
                stamps.append((package, architecture, stamp))
        return stamps

    def _stamps(self):
        stamps = os.path.join(self.repository(), STAMPS)
        os.makedirs(stamps, exist_ok=True)
        return stamps

    def _stamps_spec(self):
        stamps = os.path.join(self.repository(), STAMPS_SPEC)
        os.makedirs(stamps, exist_ok=True)
        return stamps

    # What a previous build of this source package left in the repository.
    # Each stamp lists the files its build produced, so an older build can
    # be undone without having to guess which binary packages came from
    # which source -- the names rarely match, and one source package
    # commonly produces several.
    def _previous(self, package, architecture):
        previous = {}
        for stamp in sorted(os.listdir(self._stamps())):
            if stamp.startswith("%s_%s_" % (package.name, architecture)) == False:
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
    # 'produced' is what every build being published now made, not only
    # this architecture's: the architecture-independent binaries move
    # between architectures when 'scope' changes, and the build that has
    # just lost them would otherwise take away the copy the build that
    # gained them has only just written.
    def _forget(self, package, architecture, produced):
        for stamp, files in self._previous(package, architecture).items():
            for name in files:
                if name in produced:
                    continue
                path = os.path.join(self.repository(), name)
                if os.path.isfile(path):
                    os.unlink(path)
            os.unlink(stamp)
            excerpt = self._excerpt_path(stamp)
            if os.path.isfile(excerpt):
                os.unlink(excerpt)

    # What a build left in the directory it was given. Everything there is
    # its own, so there is nothing to date or to tell apart: it is the
    # only thing that wrote there.
    def _produced(self, output):
        return sorted(name for name in os.listdir(output)
                      if os.path.isfile(os.path.join(output, name))
                      or os.path.islink(os.path.join(output, name)))

    def _record(self, stamp, produced):
        with open(stamp, "w") as f:
            for name in produced:
                f.write("%s\n" % name)



    # Rebuilds every package the specification asked for, in the order they
    # were sorted into. Each gets a working directory of its own, thrown
    # away afterwards unless --keep was asked for: what is worth keeping
    # (the .debs) is in the repository by then.
    # One task per package, so packages that do not depend on each other
    # can be built beside each other, and a 'packages' barrier for the
    # rest of the build to wait on. The barrier is what lets the root
    # file-system and the imager name one thing that always exists,
    # rather than whichever packages a specification happens to have.
    #
    # They need the host bootstrap and not the target one: packages are
    # built in a chroot of the build architecture, whichever architecture
    # the image is for.
    def tasks(self, packages, hostBootstrap, vendor_task=None):
        if self._tasked is not None and packages != self._tasked:
            raise RuntimeError(
                "Builder.tasks() was called twice on one Builder with two "
                "different package lists -- a shared Builder wants the "
                "union of every image's packages in one call, not one "
                "call per image")
        self._tasked = packages
        module.resolve_kernels(self, packages, hostBootstrap)
        pending = self._pending(packages)
        # Every build in here reaches the builder image's own apt, whether
        # it fetches, resolves build-deps or indexes -- so whenever a
        # 'vendor' task is running ahead of this one (Image.shared_tasks()
        # names it when 'apt-pull-mode: offline' needs it), 'packages-
        # prepare' waits on it too, the same way it already waits on the
        # host bootstrap.
        needs_vendor = [vendor_task] if vendor_task is not None else []
        # Which ones were already built when the graph was made, asked now
        # rather than at the barrier: by then this build has built the rest
        # of them, and a package it just made was not one it reused.
        reused = self.current(packages)
        if len(pending) == 0:
            # Nothing to build, but possibly something to index: a machine
            # that imported a repository has the .debs and nothing to read
            # them with until apt-ftparchive has run over them.
            first = []
            if self._indexable():
                first = [Task("packages-prepare",
                              functools.partial(self._prepare, hostBootstrap),
                              needs=["bootstrap-host"] + needs_vendor)]
            # No 'packages-prepare' to carry it here, so 'packages' itself
            # waits on vendor directly -- otherwise a rerun with nothing
            # pending and nothing to reindex drops the vendor wait
            # entirely, and 'rootfs' (which only waits on 'packages')
            # races it.
            needs = ["bootstrap-host"] + [t.name for t in first]
            if len(first) == 0:
                needs += needs_vendor
            return first + [Task("packages",
                                 functools.partial(self._reused, reused),
                                 needs=needs)]

        tasks = [Task("packages-prepare",
                      functools.partial(self._prepare, hostBootstrap),
                      needs=["bootstrap-host"] + needs_vendor)]

        # A package built against another has to be built after it, which
        # is the same order 'before'/'after' already worked out -- as task
        # dependencies now, so everything else can overlap.
        # What a dependent waits for is the name of the step that puts a
        # package where it can be installed from, not the one that built
        # it.
        names = {package.name: "deploy:%s" % package.name
                 for package, _, _ in pending}

        # A package's builds, gathered so its steps are declared together:
        # one prepare, a build per architecture, and one step publishing
        # them.
        grouped = collections.OrderedDict()
        for package, architecture, stamp in pending:
            grouped.setdefault(package.name, (package, []))[1].append(
                (architecture, stamp))

        # One fetch task per source these packages actually name, shared by
        # every package naming the same one -- see _fetch()/_fetch_upstream().
        # Named for the source ('fetch:linux'), digest-suffixed only where
        # two keys would otherwise share a name; 'used' is shared with the
        # fetch-upstream pass below so the two never collide either.
        fetch_tasks, upstream_tasks, used = {}, {}, {}
        for pname, (package, _builds) in grouped.items():
            fetch_key = self._fetch_key(package)
            if fetch_key is not None and fetch_key not in fetch_tasks:
                name = self._task_name("fetch", package.source_name,
                                       fetch_key, used)
                fetch_tasks[fetch_key] = Task(
                    name, functools.partial(self._fetch, package),
                    needs=["packages-prepare"])

            upstream_key = self._upstream_key(package)
            if upstream_key is not None and upstream_key not in upstream_tasks:
                name = self._task_name("fetch-upstream",
                                       package.kernel_upstream.name,
                                       upstream_key, used)
                upstream_tasks[upstream_key] = Task(
                    name, functools.partial(self._fetch_upstream, package),
                    needs=["packages-prepare"])
        tasks += list(fetch_tasks.values()) + list(upstream_tasks.values())

        # How many prepare:<name> tasks will copy from each fetch, known
        # now from the whole group rather than guessed at as each one
        # copies -- see _taken_shared().
        for pname, (package, _builds) in grouped.items():
            for key in (self._fetch_key(package), self._upstream_key(package)):
                if key is not None:
                    self._shared_wanted[key] = self._shared_wanted.get(key, 0) + 1

        for name, (package, builds) in grouped.items():
            depends = [names[d.name] for d in getattr(package, "depends", [])
                       if d.name in names]
            fetch_key = self._fetch_key(package)
            upstream_key = self._upstream_key(package)
            needs = [fetch_tasks[fetch_key].name]
            if upstream_key is not None:
                needs.append(upstream_tasks[upstream_key].name)
            # A module is the exception to preparing early: its packaging
            # is written as the source is prepared, decided by the ABI of
            # the kernel it names, which does not exist until built here.
            # Only for the kernels it waits on -- one built against the
            # distribution's kernels alone prepares (and fetches) as early
            # as anything else.
            if package.module:
                needs += depends
            prepare = "prepare:%s" % name
            tasks.append(Task(prepare,
                              functools.partial(self._prepare_source, package,
                                                fetch_key, upstream_key),
                              needs=needs))
            waits = [prepare] + depends
            built = []
            for architecture, stamp in builds:
                step = "package:%s" % self.label(package, architecture)
                tasks.append(Task(step,
                                  functools.partial(self._rebuild, package,
                                                    architecture, stamp),
                                  needs=waits))
                built.append(step)

            # One publishing step for every architecture, rather than one
            # each. What comes out of the builds is not a repository per
            # architecture and nothing else: an 'Architecture: all' binary
            # belongs in all of them and is built by only one of them, so
            # somebody has to hold the whole picture -- and the step that
            # decides what an earlier build left behind should be forgotten
            # is the natural somebody.
            tasks.append(Task(names[name],
                              functools.partial(self._deploy, package,
                                                [a for a, _ in builds]),
                              needs=built))

        return tasks + [Task("packages",
                             functools.partial(self._reused, reused),
                             needs=[t.name for t in tasks])]


    # The packages this build did not have to build, recorded as taken from
    # the cache. At the barrier rather than where the decision is made: that
    # decision is also made by '--dry-run', which uses nothing and should
    # leave the index saying so.
    def _reused(self, current):
        for package, architecture, stamp in current:
            key = self.key(package, architecture)
            entry = Index().hit(PACKAGE, key)
            say(self.options, "package %s reused, made %s"
                              % (key, since(entry.get("made"))))

    # What a package is called in the cache index. The release and the
    # architecture are part of it: one index covers a machine, and the same
    # source built for two releases -- or for two architectures -- is two
    # different sets of .debs.
    def key(self, package, architecture=None):
        return "%s/%s/%s" % (self.distro["release"],
                             architecture or self.distro["architecture"],
                             package.name)

    # The packages this build would leave alone, with the stamp that says
    # so. A stamp names the digest of everything that would go into the
    # build, so one that exists means the .debs in the repository were
    # made from exactly these inputs.
    def current(self, packages):
        rebuild = self.options.get("rebuild", False)
        if rebuild:
            return []
        return [(p, a, s) for p, a, s in self.stamps(packages)
                if os.path.isfile(s)]

    # Which packages this build actually has to rebuild, decided before
    # anything is created: reading a stamp costs nothing, and a build
    # whose packages are all current should not make a builder image to
    # discover it.
    def _pending(self, packages):
        if len(packages) == 0:
            return []
        rebuild = self.options.get("rebuild", False)
        return [(p, a, s) for p, a, s in self.stamps(packages)
                if rebuild or os.path.isfile(s) == False]

    # Whether the repository is holding .debs its index does not describe.
    #
    # The index is made from what the directory holds, which makes it the
    # build's to maintain rather than something worth carrying between
    # machines: what a cache is worth carrying for is the .debs, and the
    # machine that receives them can say what is in them in one pass of
    # apt-ftparchive. A build with nothing to rebuild would otherwise leave
    # those .debs sitting there with apt unable to see them.
    def _indexable(self):
        repository = self.repository()
        if os.path.isdir(repository) == False:
            return False
        debs = [name for name in os.listdir(repository) if name.endswith(".deb")]
        if len(debs) == 0:
            return False
        index = os.path.join(repository, "Packages")
        if os.path.isfile(index) == False:
            return True
        # Written before the .debs it describes were, which is what an
        # import leaves behind when it brings newer ones.
        described = os.path.getmtime(index)
        return any(os.path.getmtime(os.path.join(repository, name)) > described
                   for name in debs)

    # Every build has the repository in its sources.list, including the
    # first one, when nothing has been rebuilt yet: apt needs an index to
    # read there, even an empty one, or the build fails before it starts.
    def _prepare(self, hostBootstrap):
        self.builderImage.create(hostBootstrap)
        with locked(self.repository()):
            self._publish_key()
            self.index()

    # The public half of the signing key, kept in the repository it signs.
    # Anything the repository is mounted into can then find the key that
    # answers for it without being told where it is, and a cache carried
    # to another machine takes it along.
    #
    # Named for the key rather than for seine, since it ends up in
    # /etc/apt/keyrings beside other people's.
    def _publish_key(self):
        for name in sorted(os.listdir(self.repository())):
            # A key from a build signed by another key, or by none: what
            # answers for this repository is what signed it last.
            if name.endswith(".gpg"):
                os.unlink(os.path.join(self.repository(), name))
        if self.signer is not None:
            self.signer.export(os.path.join(self.repository(),
                                            self.signer.keyring()))

    def run(self, packages, hostBootstrap):
        module.resolve_kernels(self, packages, hostBootstrap)
        pending = self._pending(packages)
        if len(pending) == 0:
            return

        self._prepare(hostBootstrap)
        grouped = collections.OrderedDict()
        for package, architecture, stamp in pending:
            grouped.setdefault(package.name, (package, []))[1].append(
                (architecture, stamp))
        for package, builds in grouped.values():
            for architecture, stamp in builds:
                self._rebuild(package, architecture, stamp)
            self._deploy(package, [a for a, _ in builds])

    # What fetch() would run, as a key two packages asking for the same
    # bytes can share -- see _fetch(). None for a cross-headers package:
    # it has no source of its own to name, and fetch() already takes a
    # different path for it.
    def _fetch_key(self, package):
        if module.is_cross_package(package):
            return None
        return tuple(self._fetch_args(package))

    # As _fetch_key(), for kernel.fetch_upstream()'s own download: the
    # tree a grafted kernel is built from, the largest download seine
    # makes and fetched apart from the packaging it is grafted onto.
    def _upstream_key(self, package):
        if package.kernel_upstream is None:
            return None
        return tuple(kernel._upstream_args(package.kernel_upstream))

    # One task name per key, 'prefix:label' in the common case -- 'used'
    # is shared across every call so different prefixes never collide
    # either. A label already claimed by a different key gets a digest
    # suffix; claimed by the same key again just answers with the name
    # already given out.
    def _task_name(self, prefix, label, key, used):
        name = "%s:%s" % (prefix, label)
        claimed = used.get(name)
        if claimed is None:
            used[name] = key
            return name
        if claimed == key:
            return name
        return "%s-%s" % (name, hashlib.sha256(repr(key).encode()).hexdigest()[:6])

    # Task body for a shared source fetch -- the network half of what
    # _fetched() (still used for a cross-headers package -- see
    # _fetch_key()) used to do in one step. Populates the canonical copy
    # every prepare:<name> task naming this fetch's key will copy from;
    # tasks() builds exactly one such task per key, so nothing here
    # needs a lock.
    def _fetch(self, package):
        print("fetching '%s'" % package.source)
        canonical = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="fetched-")
        self.fetch(package, canonical)
        self._shared_fetches[self._fetch_key(package)] = canonical

    # As _fetch(), for kernel.fetch_upstream()'s own download.
    def _fetch_upstream(self, package):
        print("fetching '%s'" % package.kernel_upstream)
        canonical = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="fetched-")
        kernel.fetch_upstream(self, package, canonical)
        self._shared_fetches[self._upstream_key(package)] = canonical

    # 'source's contents copied into 'dest' (which already exists);
    # 'source' itself is left for the caller to remove. Symlinks are
    # copied as symlinks, never followed -- a kernel tree is full of
    # them, and dereferencing one into a plain file is a change
    # dpkg-source refuses to represent as a patch.
    def _copy_into(self, source, dest):
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(source):
            item = os.path.join(source, name)
            target = os.path.join(dest, name)
            if os.path.islink(item):
                os.symlink(os.readlink(item), target)
            elif os.path.isdir(item):
                shutil.copytree(item, target, symlinks=True)
            else:
                shutil.copy2(item, target)

    # 'key''s claim on its canonical fetch, given up -- the counterpart to
    # _fetch()/_fetch_upstream() writing it, called by every prepare:<name>
    # task that copied from it. The canonical copy goes once
    # '_shared_taken' catches up with '_shared_wanted'.
    def _taken_shared(self, key):
        if key is None:
            return
        with self._shared_lock:
            self._shared_taken[key] = self._shared_taken.get(key, 0) + 1
            done = self._shared_taken[key] >= self._shared_wanted[key]
            if done:
                canonical = self._shared_fetches.pop(key)
                del self._shared_wanted[key]
                del self._shared_taken[key]
        if done:
            if self.options.get("keep"):
                print("keeping '%s' (a shared fetch) as requested" % canonical)
            else:
                shutil.rmtree(canonical, ignore_errors=True)

    # Fetch and prepare in one step, both from here on used only for a
    # cross-headers package (_fetch_key() names no key for one, so it
    # never reaches tasks()'s own fetch:/prepare: split -- see
    # _cross_headers_built()'s direct call into _rebuild(), which is
    # what calls this when nothing has fetched it yet).
    def _fetched(self, package):
        workdir = tempfile.mkdtemp(dir=ContainerEngine.scratch(),
                                   prefix="source-")
        try:
            print("fetching '%s'" % (package.source or package.name))
            sourcedir = self.fetch(package, workdir)
            kernel.fetch_upstream(self, package, workdir)
            dsc, epoch = self._prepared(package, workdir, sourcedir)
        except:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        source = (workdir, dsc, epoch)
        with self._workdirs:
            self._sources[package.name] = source
            self._holding[package.name] = len(self.architectures(package))
        return source

    # What tasks()'s prepare:<name> task runs, for every real (non
    # cross-headers) package: its own copy of whichever fetch(es)
    # 'fetch_key'/'upstream_key' name, turned into a source package the
    # same way _fetched() used to. Sets what _rebuild() reads exactly as
    # _fetched() did.
    def _prepare_source(self, package, fetch_key, upstream_key):
        workdir = tempfile.mkdtemp(dir=ContainerEngine.scratch(),
                                   prefix="source-")
        try:
            self._copy_into(self._shared_fetches[fetch_key], workdir)
            sourcedir = self._source_dir(package.source, workdir)
            if upstream_key is not None:
                self._copy_into(
                    os.path.join(self._shared_fetches[upstream_key], kernel.UPSTREAM),
                    os.path.join(workdir, kernel.UPSTREAM))
            dsc, epoch = self._prepared(package, workdir, sourcedir)
        except:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        finally:
            self._taken_shared(fetch_key)
            self._taken_shared(upstream_key)

        source = (workdir, dsc, epoch)
        with self._workdirs:
            self._sources[package.name] = source
            # How many builds are still to be handed this source. The
            # directory outlives this step but not the last build that
            # reads it, and with two architectures reading one tree the
            # first to finish is not the one that may throw it away.
            self._holding[package.name] = len(self.architectures(package))
        return source

    # A fetched tree turned into the source package the builds are handed,
    # and the date they are all pinned to.
    def _prepared(self, package, workdir, sourcedir):
        # Taken before the graft, which replaces the changelog it is read
        # from: what dates the build is the packaging we started from,
        # which is a thing the specification pins.
        epoch = self.source_date_epoch(package, sourcedir)
        # A cross headers package is not the kernel it is built from, and
        # none of what follows is about it: no graft, no patches, no
        # local changelog entry on somebody else's source.
        if module.is_cross_package(package):
            module.extend_cross_headers(
                self, package, sourcedir, epoch,
                os.path.join(os.path.dirname(sourcedir), module.CROSS_FETCHED))
            dsc = self.source_package(package, sourcedir)
            self._stage_source(package, os.path.dirname(sourcedir), dsc)
            return dsc, epoch

        if package.kernel_upstream is not None:
            sourcedir = kernel.graft(self, package, workdir, sourcedir, epoch)
        # Before the patches and before the local changelog entry: both
        # read a debian/ directory, and for a module this is what puts
        # one there.
        module.extend(self, package, sourcedir, epoch)
        self.patch(package, sourcedir, epoch)
        # After them: what the series already answers for is not written
        # again, and a specification's own patches are in it by now.
        if package.kernel_upstream is not None:
            kernel.module_lds_patch(package, sourcedir)
        self.local_release(package, sourcedir, epoch)
        kernel.extend(self, package, sourcedir, self.architectures(package))
        dsc = self.source_package(package, sourcedir)
        # Signed before it is staged or built from: a .dsc carries its
        # signature inside itself, so signing it here is what makes the
        # copy that reaches the repository -- and any machine handed the
        # cache -- say who built it. dpkg-source reads a signed one as
        # readily as an unsigned one.
        if self.signer is not None:
            self.signer.clearsign(os.path.join(os.path.dirname(sourcedir), dsc))
        self._stage_source(package, os.path.dirname(sourcedir), dsc)
        return dsc, epoch

    # The source package put where publishing will find it, which is not
    # where it was built: the working directory is thrown away by the last
    # build to finish, and publishing comes after that.
    #
    # Once per package rather than once per build, which is what it is:
    # both architectures were handed this same .dsc, and a source package
    # is not built for an architecture.
    def _stage_source(self, package, workdir, dsc):
        staged = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="source-")
        for name in self.source_files(workdir, dsc):
            shutil.copy(os.path.join(workdir, name), staged)
        self._source_packages[package.name] = staged

    # One build's claim on a fetched source, given up. The last to do so
    # takes the directory with it.
    def _release(self, package):
        with self._workdirs:
            holding = self._holding.get(package.name, 1) - 1
            self._holding[package.name] = holding
            if holding > 0:
                return
            workdir = (self._sources.pop(package.name, None) or (None,))[0]
        if workdir is None:
            return
        if self.options.get("keep"):
            print("keeping '%s' (source of '%s') as requested"
                  % (workdir, package.name))
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    # Putting what was built where the rest of the build can install it
    # from: the repository, and the index apt reads there.
    #
    # Both only once it built. A stamp left by a failed build would skip
    # the package next time and compose the image from whatever the
    # repository happened to hold, and dropping the previous build before
    # this one succeeds would leave the repository with neither.
    #
    # One at a time, and against every other build on the machine as well:
    # an index rewritten while another build's apt reads it is a failure
    # that arrives much later and makes no sense when it does.
    def _deploy(self, package, architectures=None):
        if architectures is None:
            architectures = [self.distro["architecture"]]

        built = {}
        for architecture in architectures:
            stamp, output = self._built.pop((package.name, architecture),
                                            (None, None))
            if stamp is not None:
                built[architecture] = (stamp, output, self._produced(output))
        if len(built) == 0:
            return

        # What all of these builds made, which is what none of them may
        # take away. A build's own files are its to record; keeping the
        # others safe is what stops one architecture from tidying away
        # another's as superseded.
        everything = set()
        for _, _, produced in built.values():
            everything.update(produced)

        # The source package the builds were handed, published once
        # however many of them there were: it is one set of files, named
        # by one .dsc, and neither belongs to an architecture. Recorded in
        # every build's stamp all the same, so it is retired when the last
        # of them goes rather than by whichever is forgotten first.
        staged = self._source_packages.pop(package.name, None)
        sources = [] if staged is None else sorted(os.listdir(staged))
        everything.update(sources)

        # The .changes says what a build produced and with what hashes,
        # which is the thing worth a signature: the .debs beside it are
        # named by it. Signed before anything is moved, so what lands in
        # the repository is signed rather than signed in place afterwards.
        if self.signer is not None:
            for _, output, produced in built.values():
                for name in produced:
                    if name.endswith(".changes"):
                        self.signer.clearsign(os.path.join(output, name))

        # Whether anything published here took the place of a file that
        # was already in the repository. It decides whether the index can
        # be made from what it remembers; see index().
        replaced = False

        with self._repository, locked(self.repository()):
            for name in sources:
                destination = os.path.join(self.repository(), name)
                if os.path.lexists(destination):
                    os.remove(destination)
                    replaced = True
                shutil.move(os.path.join(staged, name), destination)
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)

            for architecture, (stamp, output, produced) in sorted(built.items()):
                for name in produced:
                    destination = os.path.join(self.repository(), name)
                    # What is already there goes first. A file would be
                    # replaced by the move on its own, but a symlink would
                    # not -- and sbuild leaves one beside every build log,
                    # pointing at the newest, so the second build of a
                    # package would stop here with 'File exists' having
                    # already done all of its work.
                    #
                    # lexists, since that symlink may be pointing at a log
                    # an earlier build of the same package has taken away.
                    if os.path.lexists(destination):
                        os.remove(destination)
                        replaced = True
                    shutil.move(os.path.join(output, name), destination)
                shutil.rmtree(output, ignore_errors=True)
                self._forget(package, architecture, everything)
                self._record(stamp, sorted(set(produced) | set(sources)))
                self._record_excerpt(stamp, package)
            # Once, at the end: one repository, and an index of it made
            # while a build of it is half moved in describes neither what
            # was there nor what is.
            self.index(cached=replaced == False)

        for architecture in sorted(built):
            key = self.key(package, architecture)
            Index().made(PACKAGE, key)
            say(self.options, "package %s made" % key)

    # One package: patched, built, and recorded as built. The chroot is
    # shared with whatever else is building at the same time, so it is
    # taken one at a time.
    def _rebuild(self, package, architecture, stamp):
        # What this build needs installed that no specification asked
        # for. Built here rather than planned earlier because a kernel
        # built by this specification only says what its ABI is once its
        # own source has been prepared -- so which cross headers are
        # wanted is not knowable until the kernel that decides it has
        # been built, which 'after' guarantees has happened by now.
        self._cross_headers_built(package, architecture)

        with self._chroots:
            chroot = SbuildChroot(self.distro, self.options,
                                  self.chroot_architecture(package, architecture))
            chroot.create(self.builderImage,
                          offline=len(offline_suites(self.distro)) > 0)

        with self._workdirs:
            source = self._sources.get(package.name)
        if source is None:
            source = self._fetched(package)
        workdir, dsc, epoch = source

        # Where this build writes, which is nowhere anything else does.
        output = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="built-")
        try:
            print("rebuilding '%s' for %s" % (package.source, architecture))
            self.build(package, workdir, dsc, epoch, architecture, output)

            # Handed to the step that publishes it. What a package built
            # against another needs is that one's .deb in the repository,
            # for sbuild to install out of -- which is a later moment than
            # its build finishing.
            self._built[(package.name, architecture)] = (stamp, output)
        except:
            # Left where it is, and said out loud. sbuild writes its build
            # log beside what it produced, so this directory is where the
            # record of the failure is -- what the chroot installed, and
            # the compiler error that stopped it. Throwing it away would
            # leave nothing to read but the exit status.
            #
            # It stays in the scratch space, which 'seine cache clear
            # scratch' empties, rather than being tidied away here by the
            # one build that had something worth keeping.
            print("keeping '%s': what the failed build of '%s' wrote, its "
                  "build log included" % (output, package.name))
            raise
        finally:
            self._release(package)



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

# Where a package's own preferences go, beside seine's rather than in it:
# the two say different things -- which origin to prefer, and what this
# one build may install -- and a fragment of its own is what lets the
# specification's text be written down exactly as it was given.
#
# Not 'seine-package', which would be a prefix of the name above it and so
# a thing to misread in a directory listing and in anything matching on
# names.
PACKAGE_PREFERENCES = "/etc/apt/preferences.d/seine-build"

# sbuild expands percent escapes in the commands it is handed -- '%s' is
# the interactive shell it would otherwise drop you into, and '%%' is how
# a literal percent is written -- and it does that before any shell sees
# the command. So a command meant to arrive intact has its percents
# doubled.
#
# Found by the file this writes arriving as the expansion of '%s' and apt
# refusing to read it:
#
#   E: Unable to parse package file /etc/apt/preferences.d/seine-build (1)
#
# Doubled here rather than asked of a specification: what it writes is
# apt's language, and sbuild's escaping is no business of its.
def sbuild_command(command):
    return command.replace("%", "%%")

# Written with printf rather than echo: what a specification put here is
# several lines of apt_preferences(5), and echo would need it taken apart
# and put back together again -- and would interpret backslashes in it on
# any shell whose echo does. quote() is what makes the text survive the
# shell it travels through unaltered.
def package_preferences_command(preferences):
    if preferences.endswith("\n") == False:
        preferences += "\n"
    return "printf '%%s' %s > %s" % (shlex.quote(preferences),
                                     PACKAGE_PREFERENCES)

# Where a repository's own key is installed in whatever is going to read
# from it. Under apt's keyrings directory rather than pointed at where the
# repository is mounted, because it stays behind in the image: the
# repository is gone from the sources.list by then, and what is left is a
# key that can answer for one served from somewhere else.
KEYRINGS = "/etc/apt/keyrings"

def apt_configuration(*mountpoints, keyring=None):
    # A repository nothing signed is trusted because it was made here a
    # moment ago; one that is signed is verified instead, which is worth
    # more the further it travels.
    if keyring is None:
        options = "[trusted=yes]"
        install = ""
    else:
        options = "[signed-by=%s/%s]" % (KEYRINGS, keyring)
        install = "install -D -m 0644 %s/%s %s/%s && " % (
            mountpoints[0], keyring, KEYRINGS, keyring)
    # deb-src beside deb: the repository carries a Sources index over the
    # source packages it holds, and a kernel built here is the only place
    # its source can be fetched from -- there is no archive that has it.
    # What needs it is the headers package built for another architecture
    # out of that kernel's own source.
    lines = " && ".join(
        "echo 'deb%s %s file:%s ./' %s %s"
        % (kind, options, mountpoint,
           ">" if (index == 0 and kind == "") else ">>", SOURCES_LIST)
        for index, mountpoint in enumerate(mountpoints)
        for kind in ["", "-src"])
    return "%s%s && %s" % (install, lines, apt_preferences_command())

# The key a repository carries, if it carries one and is actually signed
# -- which is how anything mounting it learns which key answers for it
# without being told.
#
# Both halves of that matter. A cache carried to another machine brings
# the public key with it, but not the private one, so the build there
# writes indices nothing has signed: a repository configured 'signed-by'
# on the strength of a key file alone would be one apt refuses to read.
# What says a repository is signed is a signature.
def keyring(distro):
    where = repository(distro)
    if os.path.isfile(os.path.join(where, "InRelease")) == False:
        return None
    for name in sorted(os.listdir(where)):
        if name.endswith(".gpg") and name.startswith("Release") == False:
            return name
    return None

# The sources.list and the pin go, since the repository they name is on
# the machine that did the building. The keyring stays: it says which key
# answers for those packages, and an image updated later from a repository
# signed by the same key needs it to say so.
def apt_deconfiguration():
    return "rm -f %s %s" % (SOURCES_LIST, PREFERENCES)

# The same configuration for images built from a Dockerfile: a layer that
# sets apt up, and the bind mounts that make the repositories readable
# while that image is being built. Both are empty when the specification
# rebuilt nothing, so an image that has no use for a repository is not
# given a sources.list pointing at a directory that will not be there.
def apt_setup_layer(distro):
    if has_packages(distro) == False:
        return ""
    return "RUN %s\n" % apt_configuration(REPOSITORY, keyring=keyring(distro))

def build_volumes(distro):
    if has_packages(distro) == False:
        return []
    return ["-v", "%s:%s:ro" % (repository(distro), REPOSITORY)]

# Where the rebuilt packages of a specification are kept: one flat
# repository per release, holding every architecture built for, the way a
# distribution's archive does.
def repository(distro):
    return ContainerEngine.packages(distro["release"])

# Whether there is anything there to install: a specification with no
# 'packages' section leaves the directory without an index, and pointing
# apt at it would only earn a failed 'apt-get update'.
def has_packages(distro):
    return os.path.isfile(os.path.join(repository(distro), "Packages"))

# Validates the 'packages' section and returns it as Package objects,
# ordered the way they will be built.
# What answers for a source, if anything does. A specification that wants
# every download accounted for asks with --require-hashes, and is told
# before a byte is fetched rather than after.
def integrity(package):
    if package.scheme == "apt":
        return "the archive's signed index"
    if package.scheme == "git":
        return "the revision it is pinned to"
    return "a declared sha256" if package.sha256 is not None else None

def upstream_integrity(package):
    upstream = package.kernel_upstream
    if upstream is None:
        return "nothing to fetch"
    if upstream.scheme == "git":
        return "the revision it is pinned to"
    return ("a declared sha256"
            if package.kernel_upstream_sha256 is not None else None)

# Every source nothing vouches for, named with the file that wrote it, so
# one run says all of them rather than one per attempt.
def unvouched(packages):
    found = []
    for package in packages:
        if integrity(package) is None:
            found.append((package.source, package.origin_of("source"),
                          "sha256"))
        if upstream_integrity(package) is None:
            found.append((str(package.kernel_upstream),
                          package.origin_of("extends.kernel.upstream"),
                          "upstream-sha256"))
    return found

def parse(spec):
    packages = spec.get("packages", [])
    if type(packages) != type([]):
        raise ValueError("'packages' shall be a list of source packages!")

    parsed = [Package(p, i + 1) for i, p in enumerate(packages)]
    # An entry here asks for a build, and a build needs something to
    # fetch. Naming a package without saying where its source comes from
    # describes it, which is what 'defaults' is for -- and a description
    # left under 'packages' would otherwise be a build of nothing.
    for package in parsed:
        if package.source is None:
            raise ValueError(
                "package '%s' has no 'source' to build from. An entry under "
                "'packages' asks for a package to be built; one that only "
                "describes a package goes under 'defaults'." % package.name)
    # Before ordering, so that a kernel named here is reported by the
    # message written for it rather than by 'after' finding a package it
    # cannot name.
    module.check_references(parsed)
    module.depend_on_kernels(parsed)
    ordered = propagate(order(parsed))
    module.check_kernels(ordered, spec)
    return ordered

# Carries a package's scope down to what it is built after.
#
# A package built for the host is compiled and linked against what its
# dependencies installed, which have to be of the host's architecture too
# -- so a dependency that said nothing about who it is for is built for
# whoever needs it, on top of the image it was already going to be in.
# Saying it twice is bookkeeping the specification should not have to do.
#
# A dependency that *did* say is not widened, it is an error: an explicit
# scope is an answer, and quietly building a package for an architecture
# its specification ruled out is how a build gives you something you said
# you did not want. The message names both entries, since which of them is
# wrong is not seine's to decide.
#
# In reverse build order, so one pass carries a role the length of a
# chain: a package is reached before everything it is built after.
def propagate(packages):
    for package in reversed(packages):
        for dependency in getattr(package, "depends", []):
            missing = [r for r in package.scope if r not in dependency.scope]
            if len(missing) == 0:
                continue
            if dependency.scoped:
                raise dependency._error(
                    "'scope' is '%s', but '%s' is built for '%s' and is built "
                    "after it -- so it would be built against a '%s' that was "
                    "never built. Add %s to this package's scope, or take it "
                    "off '%s'."
                    % (", ".join(dependency.scope), package.source,
                       ", ".join(package.scope), dependency.name,
                       " and ".join("'%s'" % r for r in missing),
                       package.source))
            dependency.scope = sorted(set(dependency.scope) | set(missing))
    return packages

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
