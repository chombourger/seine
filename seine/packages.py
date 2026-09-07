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
from seine.kernel import uki
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
from seine.container import ContainerEngine
from seine.utils  import GIT_EMAIL
from seine.utils  import GIT_NAME
from seine.utils  import HOST_ARCH
from seine.utils  import WORKDIR
from seine.utils  import redact
from seine.utils  import redactions

# A source package to rebuild. Where the source comes from:
#   apt://busybox[=1:1.37.0-6]     the distro's own source package
#   https://.../busybox_1.dsc      a .dsc published elsewhere
#   git://host/busybox.git;rev=..  a tree with its own debian/ directory
SCHEMES = ["apt", "git", "https"]

# Who a rebuild is for: 'target' (installed on the image, the default) or
# 'host' (a build tool, e.g. a code generator another package needs).
SCOPES = ["host", "target"]
DEFAULT_SCOPE = ["target"]

# Key for a package's apt-preferences text when it names no release.
ANY_RELEASE = None

# Build types 'extends' knows about, each a module with its own settings.
EXTENSIONS = {
    "kernel": kernel.SETTINGS,
    "module": module.SETTINGS,
    "uki": uki.SETTINGS,
}


# Date a build is pinned to when the source gives none (no changelog, no
# dated revision). Fixed, not "now", so rebuilds stay reproducible.
FALLBACK_EPOCH = 946684800


# apt-ftparchive's own cache of packages already read, so re-indexing the
# repository after a build does not re-hash every .deb again.
INDEX_CACHE = ".packages.db"


# Appended to every rebuilt package's version, so it sorts above the
# distro's own. 'revision' overrides this per package.
DEFAULT_REVISION = "mod1"

# What a Debian revision may contain: letters, digits, '+', '.', '~' --
# never '-', which dpkg-parsechangelog reads as starting a second field.
DEBIAN_REVISION = re.compile(r"^[A-Za-z0-9+.~]+$")


class Package:
    def __init__(self, spec, index):
        self.index = index
        if type(spec) != type({}):
            raise ValueError("package #%d is not a dictionary!" % index)
        # A 'source' asks for a build; a bare 'name' only describes one
        # (used under 'defaults').
        if "source" not in spec and type(spec.get("name")) != type(""):
            raise ValueError(
                "package #%d has neither a 'source' to build nor a 'name' "
                "saying which package it describes!" % index)

        self.spec = spec
        self.priority = spec.get("priority", 500)
        # Which file set each setting, used in error messages.
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
        # name(s) it derives, not 'mod1' -- else two flavours built from
        # different files would collide on one '<source>_<version>+mod1.dsc'.
        default_revision = DEFAULT_REVISION
        if self.kernel_derived_flavours:
            names = sorted(set(name for derived in self.kernel_derived_flavours.values()
                               for name in derived))
            # '.', not '-': a flavour name may itself contain '-'.
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
        # A kernel is per architecture; two flavours under one entry would
        # mean one 'flavour' name for two kernels, which is not possible.
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

    # Settings ignored when comparing two entries for 'same_as': neither
    # is part of what actually gets built.
    IGNORED_SETTINGS = ("_origins", "priority")

    def same_as(self, other):
        mine = {k: v for k, v in self.spec.items()
                if k not in Package.IGNORED_SETTINGS}
        theirs = {k: v for k, v in other.spec.items()
                 if k not in Package.IGNORED_SETTINGS}
        return mine == theirs

    def _parse_source(self, source):
        self.source = source
        # Defaults, so callers don't need to care which scheme was used --
        # or whether the entry named a source at all.
        self.scheme = None
        self.name = None
        self.version = None
        self.parameters = {}
        self.source_name = None

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
            if len(self.parameters.get("rev", "")) == 0:
                raise self._error(
                    "git sources shall be pinned with ';rev=<commit>' so the "
                    "same specification always rebuilds the same source")
            self.name = os.path.basename(location).removesuffix(".git")

        # What the URI names, kept apart from 'name' -- fetching uses this,
        # the specification/repository use 'name'.
        self.source_name = self.name

    # What this package is called: the source package it produces, not
    # the last word of its fetch URI -- those differ for a tree with no
    # debian/ directory of its own.
    def _parse_name(self, spec):
        name = spec.get("name")
        if name is None:
            return self.source_name
        if type(name) != type(""):
            raise self._error("'name' shall be a string")
        if re.match(r"^[a-z0-9][a-z0-9+.-]+$", name) is None:
            raise self._error(
                "'name' is '%s', which is not a source package name: those "
                "are lowercase, start with a letter or a digit, are at "
                "least two characters, and hold only letters, digits and "
                "'+', '-' or '.'" % name)
        return name

    # Settings for a particular kind of build go under 'extends', named
    # after the kind -- so a kernel setting on a busybox entry is a
    # visible mistake instead of a silently ignored one.
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
                # A module names its kernels per architecture, so
                # '<arch>-kernels' is not on the fixed settings list.
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
        uki.parse(self, extends)
        return extends

    # Upstream version of a source, as written in the specification.
    # Not the 'version' a 'source' URI pins (which archive version to
    # fetch) -- this says what is being built, for a tree with no
    # changelog to read it from.
    def _parse_version(self, spec):
        version = spec.get("version")
        # A string, not a number: YAML reads unquoted 1.10 as float 1.1.
        if version is not None and type(version) != type(""):
            raise self._error(
                "'version' shall be a string: write it in quotes, since a "
                "version is not a number -- yaml reads 1.10 as 1.1")
        if self.module and version is None and self.source is not None:
            raise self._error(
                "'version' is not set. seine writes the packaging for an "
                "out-of-tree module, so nothing in the tree says what "
                "version is being built -- the specification has to.")
        if self.uki and version is None:
            raise self._error(
                "'version' is not set. seine writes the packaging for a "
                "UKI wrapper from nothing, so there is no upstream tree "
                "to read one from -- the specification has to say it.")
        return version

    # apt preferences to put in front of this build, written verbatim as
    # apt_preferences(5) expects. Either one text for every release, or a
    # mapping keyed by release; a release named by neither gets none.
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

    # What this build may install for a given release: its per-release
    # text, or the one written for all releases, or nothing.
    def preferences_for(self, release):
        if release in self.apt_preferences:
            return self.apt_preferences[release]
        return self.apt_preferences.get(ANY_RELEASE)

    # Who this rebuild is for. 'scoped' says whether it was written down
    # at all -- an unscoped package may be widened by a dependent later.
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

    # sha256 as written: 64 hex digits, checked here so a bad one is
    # reported against the file that holds it, not the download later.
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

    # File that set a setting, e.g. 'source', 'extends.kernel.upstream'.
    # None if nothing set it.
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

    # Every local file this package's spec entry names.
    def referenced_files(self):
        return (self.patch_files() + self.kernel_fragment_files()
               + self.kernel_derived_flavour_files())

    def _files(self, names):
        return [os.path.normpath(n) for n in names]


# Where "already built" markers live, inside the repository so they are
# thrown away with it. Hidden so apt/dpkg-scanpackages ignore them.
STAMPS = ".stamps"

# Where each stamp's digest excerpt lives, named after it but kept in its
# own directory: same-name matching elsewhere must not pick it up too.
STAMPS_SPEC = ".stamps-spec"

# Where the container that clones finds the host user's ssh agent socket
# and known_hosts. Fixed container-side paths; host paths need not match.
SSH_AUTH_SOCK = "/ssh-agent/sock"
SSH_KNOWN_HOSTS = "/root/.ssh/known_hosts"

class Builder:
    # 'redact_patterns' is optional: most callers have no 'redact:'
    # section, and passing '[]' everywhere would be pure noise.
    def __init__(self, distro, options, builderImage, redact_patterns=None):
        self.builderImage = builderImage
        self.distro = distro
        self.options = options
        self._redact_patterns = redact_patterns or []
        # The ABI each rebuilt kernel gave itself, by package name --
        # what a module built against it must be named for.
        self.abinames = {}
        # What each 'apt://linux-headers-<flavour>' metapackage resolved
        # to, by (architecture, reference). Asked of apt once per run.
        self.metapackages = {}
        # Every package this build was asked for, so a module can look up
        # the kernel it names. Set once, by tasks() -- see '_tasked'.
        self.packages = []
        # Enforces that tasks() is called once, with the union of every
        # image's packages, not once per image.
        self._tasked = None
        # Which kernels' cross headers this run already made, so two
        # modules against one kernel do not each build it.
        self._crossed = set()
        # Held while unpacking a chroot or rewriting the repository --
        # both shared across builds running at the same time.
        self._chroots = threading.Lock()
        # What each fetch left behind until the last build reading it is
        # done, and what each build produced until it is published.
        self._sources = {}
        # Source package each fetch produced, kept apart from the
        # working directory: that directory dies with the last build
        # using it, before anything is published.
        self._source_packages = {}
        self._holding = {}
        self._workdirs = threading.Lock()
        # A fetch shared by every prepare:<name> task naming it -- see
        # _fetch()/_fetch_upstream()/_prepare_source(). Freed once every
        # sharer has taken its copy ('_shared_taken' == '_shared_wanted').
        self._shared_fetches = {}
        self._shared_wanted = {}
        self._shared_taken = {}
        self._shared_lock = threading.Lock()
        self._built = {}
        self._repository = threading.Lock()
        # Asked for here so a missing signing key stops the build now,
        # not after it has compiled.
        self.signer = signing.signer(options)
        if self.signer is not None:
            self.signer.fingerprint()

    # Cores for one package build: --parallel, or cores divided by how
    # many builds run at once.
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
        # Cross headers read from this build's own repository -- a
        # kernel built here has its source nowhere else.
        if module.is_cross_package(package):
            volumes.append((repository(self.distro), REPOSITORY))
            self.builderImage.exec(
                module._fetch_cross_args(self, package,
                                         self.distro["architecture"]),
                volumes=volumes, workdir=WORKDIR)
            return self._source_dir(package.name, workdir)

        # A uki package fetches nothing; uki.extend() writes the tree.
        if uki.is_uki_package(package):
            sourcedir = os.path.join(workdir, package.name)
            os.makedirs(sourcedir)
            return sourcedir

        ssh_volumes, environment = self._ssh(package)
        args, volumes = self._offline_fetch(
            self._fetch_args(package), package, volumes + ssh_volumes)
        self.builderImage.exec(
            args, volumes=volumes, workdir=WORKDIR, environment=environment)
        if package.scheme == "https":
            self._verify(package, workdir, os.path.basename(package.source),
                         package.sha256, "sha256")
        return self._source_dir(package.source, workdir)

    # Checks what was fetched over http against what the spec expects.
    # Checked here, not in the container: the file is bind-mounted so we
    # can read it directly, and a container verifying itself proves
    # nothing. apt and git already verify themselves.
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

    def _unvouched(self, package, name, found, setting):
        where = package.origin_of(
            "extends.kernel.upstream" if setting == "upstream-sha256"
            else "source")
        print("warning: nothing vouches for '%s'" % name)
        print("  it hashes to %s" % found)
        print("  add '%s: %s'%s" % (
            setting, found, " to %s" % where if where else ""))


    # ssh agent and known_hosts, mounted into the container that clones.
    # Keys stay on the host -- this container runs fetched build scripts
    # with more privileges than the others seine builds.
    def _ssh(self, package):
        if package.parameters.get("protocol") != "ssh":
            return [], None

        sock = os.environ.get("SSH_AUTH_SOCK")
        if sock is None:
            raise ValueError(
                "'%s' is fetched over ssh but SSH_AUTH_SOCK is unset: there "
                "is no agent to authenticate with" % package.source)

        volumes = [(sock, SSH_AUTH_SOCK)]
        # No known_hosts means an unknown host key stops the clone with a
        # prompt nobody can answer -- the right failure, not a bypass.
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
            # -u: a rebuilt or third-party .dsc need not be signed by a
            # key we hold; refusing it would make the scheme useless.
            return ["dget", "-u", package.source]

        # git:// says nothing about the protocol; bitbake spells that
        # ';protocol=', https being the default.
        protocol = package.parameters.get("protocol", "https")
        location = package.source.split("://", 1)[1].split(";")[0]
        url = "%s://%s" % (protocol, location)

        args = ["git", "clone"]
        if "branch" in package.parameters:
            args += ["--branch", package.parameters["branch"]]
        args += [url, package.source_name]
        # 'rev' is what is actually built; the branch only helps find it.
        return ["sh", "-c", "%s && cd %s && git checkout --detach %s" % (
            " ".join(args), package.source_name, package.parameters["rev"])]

    # Under 'apt-pull-mode: offline', the builder image's own
    # sources.list no longer carries this suite, so it is written fresh
    # into this throwaway container, right before the command needs it.
    # https:// and git:// sources never touch apt, so pass through as-is.
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

    # Every scheme leaves one unpacked source tree in workdir, named
    # after the upstream version (apt-get source, dget) not the package.
    # Our own hidden directories there are skipped.
    def _source_dir(self, source, workdir):
        directories = [d for d in sorted(os.listdir(workdir))
                       if os.path.isdir(os.path.join(workdir, d))
                       and not d.startswith(".")]
        if len(directories) != 1:
            raise ValueError(
                "fetching '%s' produced %d source directories, expected one: %s"
                % (source, len(directories), ", ".join(directories)))
        return os.path.join(workdir, directories[0])

    # Where the spec's patches are staged for the container. Hidden so
    # _source_dir() does not mistake it for the unpacked source.
    PATCHES = ".patches"

    # Applies the spec's patches to a fetched source tree.
    #
    # A "3.0 (quilt)" package keeps changes in debian/patches and refuses
    # anything else in the tree, so patches are added to its series. Any
    # other format (native, or a git packaging tree) takes the patch
    # directly -- as a commit in a git tree, dated at SOURCE_DATE_EPOCH
    # with a fixed identity, so the commit hash stays stable across
    # rebuilds.
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
            # No debian/source/format means the old "1.0" format, which
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
        # Missing trailing newline would glue the last existing patch to
        # the first one added here.
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


    # Date the build is pinned to. dpkg-buildpackage would derive one
    # from the changelog itself, but a patch committed to a git tree
    # already needs one before that runs.
    def source_date_epoch(self, package, sourcedir):
        if package.source_date_epoch is not None:
            return package.source_date_epoch
        # A module's tree has no changelog yet -- its revision date is
        # used instead, since that is a property of the source rather
        # than of the machine or day.
        if package.module:
            return self._committed(package, sourcedir)
        # A uki package has no revision either; same fallback as
        # _committed()'s own.
        if uki.is_uki_package(package):
            return FALLBACK_EPOCH
        source = os.path.join(WORKDIR, os.path.basename(sourcedir))
        timestamp = self.builderImage.output(
            ["dpkg-parsechangelog", "-STimestamp"],
            volumes=[(os.path.dirname(sourcedir), WORKDIR)], workdir=source)
        return int(timestamp.strip())

    # When the fetched revision was made -- for a git tree, the one date
    # that is neither the machine's nor today's. Falls back to a fixed
    # epoch, not "now", so the source package stays reproducible.
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


    # Gives the rebuild its own version above the distro's, so what a
    # machine runs can be read off its versions, and apt prefers ours on
    # version alone. Also matters for a kernel: release packaging refuses
    # to disable signed-code checks, so without this the rebuilt kernel
    # would keep a name only a signed build may use and never get
    # installed. Dated at SOURCE_DATE_EPOCH so the entry never changes
    # between rebuilds.
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


    # Cross-compiling is the default whenever the target isn't the host:
    # emulating a whole build is slow. 'cross: false' opts out.
    def cross(self, package, architecture):
        if architecture == HOST_ARCH:
            return False
        if package.cross is not None:
            return package.cross
        return True

    # Chroot architecture: the host's when cross-compiling (it runs the
    # compiler), the package's own when emulating it.
    def chroot_architecture(self, package, architecture):
        if self.cross(package, architecture):
            return HOST_ARCH
        return architecture


    # Makes the cross headers a build is about to need, if not made yet.
    # Once per kernel, not per module: a second module against the same
    # kernel finds the first module's package already in the repository.
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


    # Architectures a package is built for, per 'scope'. Collapsed to one
    # when target == host: building the same source twice would only
    # race with itself.
    def architectures(self, package):
        wanted = []
        if "target" in package.scope:
            wanted.append(self.distro["architecture"])
        if "host" in package.scope:
            wanted.append(HOST_ARCH)
        return sorted(set(wanted))

    # Which of a package's builds produces its 'Architecture: all'
    # binaries. Nominated rather than left to chance: two builds writing
    # them both would share one filename in one repository, and whichever
    # lands last wins. A native build is preferred (sbuild forces '-B' on
    # a cross build anyway, and a cross build cannot run what it just
    # built); between two native builds the host's own architecture wins.
    # When every build is a cross build, the cross build is still asked
    # for arch-all rather than skipping it -- most packaging manages it,
    # and 'cross: false' is the way out for packaging that does not.
    def indep_architecture(self, package):
        architectures = self.architectures(package)
        natives = [a for a in architectures if self.cross(package, a) == False]
        for candidate in [natives, architectures]:
            if HOST_ARCH in candidate:
                return HOST_ARCH
            if len(candidate) > 0:
                return candidate[0]
        return None

    # Step names for a package. Architecture is only added when the
    # package builds for more than one, so 'before'/'after' keep naming
    # the package rather than one particular build of it.
    def label(self, package, architecture):
        if len(self.architectures(package)) > 1:
            return "%s:%s" % (package.name, architecture)
        return package.name

    # Turns the prepared tree back into a source package. Done once per
    # package, not per architecture: a Debian source package describes
    # every architecture the packaging supports, not one of them.
    def source_package(self, package, sourcedir):
        workdir = os.path.dirname(sourcedir)

        # The fetched source's own .dsc is in the way of ours (same
        # directory, different name after the local revision) -- remove
        # it so sbuild cannot be handed the wrong one.
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

    # A source package is the .dsc plus the files it lists -- not
    # everything beside it (the old .debian tarball, a graft's leftover
    # tarball). Taking only what the .dsc names matches what
    # apt-ftparchive checks against.
    def source_files(self, workdir, dsc):
        files = [dsc]
        with open(os.path.join(workdir, dsc), "r") as f:
            listing = False
            for line in f:
                if line.startswith(" ") == False:
                    # 'Files' and the Checksums fields all list the same
                    # files, so the first one seen is enough.
                    listing = line.startswith("Files:")
                    continue
                if listing == False:
                    continue
                named = line.split()
                if len(named) == 3 and named[2] not in files:
                    files.append(named[2])
        return files

    # Builds one prepared source package for one architecture, leaving
    # the .debs/.changes/.buildinfo in that architecture's repository.
    def build(self, package, workdir, dsc, epoch, architecture, output):
        volumes = [(workdir, WORKDIR), (self.repository(), REPOSITORY),
                   (output, OUTPUT)]

        # sbuild forwards SOURCE_DATE_EPOCH and DEB_BUILD_OPTIONS into
        # the build; dpkg-buildpackage would derive the date itself, but
        # not when the spec pinned a different one.
        environment = {"SOURCE_DATE_EPOCH": epoch}
        if len(package.options) > 0:
            environment["DEB_BUILD_OPTIONS"] = " ".join(package.options)

        args = [
            "sbuild", "--chroot-mode=unshare",
            "--dist=%s" % self.distro["release"],
            # Not seine's business: these check the packaging rather
            # than build it, and need tooling not in the chroot.
            "--no-run-lintian", "--no-run-piuparts", "--no-run-autopkgtest",
        ]
        if self.cross(package, architecture):
            args += ["--build=%s" % HOST_ARCH,
                     "--host=%s" % architecture]
        else:
            args += ["--arch=%s" % architecture]

        # Said explicitly, not left to sbuild's default (build arch-all
        # whenever native): a package built natively for two
        # architectures would otherwise build them twice into one
        # repository under one name.
        args += ["--arch-all" if architecture == self.indep_architecture(package)
                 else "--no-arch-all"]
        # Cores divided across concurrent builds, not handed out whole as
        # sbuild's own default would. 'parallel=1' in options opts a
        # package out if its build breaks under parallelism.
        args += ["--jobs=%d" % self.parallel(package)]

        # dpkg's 'cross' build profile: sbuild sets it automatically only
        # while choosing profiles itself, so naming any profile at all
        # takes that decision over and must add 'cross' back by hand --
        # e.g. Debian's kernel build-depends on the cross compiler only
        # under '<cross>'.
        profiles = list(package.profiles)
        if self.cross(package, architecture) and "cross" not in profiles:
            profiles.append("cross")
        if len(profiles) > 0:
            args += ["--profiles=%s" % ",".join(profiles)]

        # A package may build-depend on one rebuilt before it. The
        # chroot reaches this repository through the builder image's own
        # bind mount, as an ordinary sources.list entry -- one entry
        # covers every architecture, which is what a 'scope: host'
        # rebuild needs (a cross build's chroot wants host-architecture
        # build-deps). The key is read from the mounted repository
        # rather than installed separately, since it is already there.
        signed = "[trusted=yes]"
        if self.signer is not None:
            signed = "[signed-by=%s/%s]" % (REPOSITORY, self.signer.keyring())
        args += ["--extra-repository=deb %s file:%s ./" % (signed, REPOSITORY),
                 "--chroot-setup-commands=%s" % apt_preferences_command()]

        # This package's own apt-preferences, pinning what its build may
        # install without affecting the packages built beside it -- e.g.
        # a rebuilt kernel's linux-libc-dev must not leak into a busybox
        # build compiled against a much older one.
        preferences = package.preferences_for(self.distro["release"])
        if preferences is not None:
            args += ["--chroot-setup-commands=%s"
                     % sbuild_command(package_preferences_command(preferences))]

        args += ["%s/%s" % (WORKDIR, dsc)]

        # podman only creates /dev/console when it allocates a terminal;
        # point sbuild's expected device at /dev/null instead of a
        # missing one. Without a terminal libc's stdio block-buffers and
        # the log lags -- 'exec.tty' below asks for a real terminal
        # instead, since sbuild strips LD_PRELOAD from what reaches the
        # chroot so an env-based workaround would not survive there.
        script = "ln -sf /dev/null /dev/console; exec %s" % shlex.join(args)

        # Run from the output directory, so sbuild's own output lands
        # where it belongs to this build alone.
        self.builderImage.exec(
            ["sh", "-c", script],
            architecture=self.chroot_architecture(package, architecture),
            volumes=volumes, workdir=OUTPUT, environment=environment, tty=True)

    def repository(self):
        return repository(self.distro)

    # Turns the directory the builds dropped .debs in into something apt
    # can read: a flat repository with plain and gzipped Packages/Sources
    # indices. apt-ftparchive, not dpkg-scanpackages, because it caches
    # what it already hashed -- re-hashing the whole repository on every
    # package would dwarf the build itself for something like a kernel's
    # debug package. The Sources index is written for anyone inspecting
    # the cache; nothing seine runs reads it back.
    #
    # 'cached' is false when a rebuild replaced a file already in the
    # repository under the same name: apt-ftparchive's own db would then
    # go on describing the old file's size/hash, so the db is dropped for
    # that run rather than served wrong.
    def index(self, cached=True):
        if cached == False:
            stale = os.path.join(self.repository(), INDEX_CACHE)
            if os.path.isfile(stale):
                os.unlink(stale)

        # Release is made fresh every time and removed first, so
        # apt-ftparchive does not hash yesterday's own signature into
        # today's file.
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

        # Signed here, not in the container: gpg runs on this machine,
        # where the agent holding the key is.
        if self.signer is not None:
            self.signer.sign_release(os.path.join(self.repository(), "Release"))

    # A digest naming a rebuild of this package, as a stamp file whose
    # presence means it was already done. Everything the spec says about
    # the package goes in, patches by content so editing one is enough
    # to ask for a rebuild.
    #
    # Deliberately excluded: the version an unpinned apt:// source would
    # resolve to today -- knowing it means fetching, which is the cost
    # this avoids. An unpinned spec keeps its first rebuild until
    # --rebuild or an edit; its .debs are still real and installable.
    #
    # The rootfs 'baseline' is also excluded: packages build against the
    # buildd chroot (from the distro settings), not against the image the
    # rootfs is later composed from.
    def stamp(self, package, architecture=None, depends=None):
        architecture = architecture or self.distro["architecture"]
        digest = hashlib.sha256()
        self._stamp_core(digest, package, architecture)

        # Patches/kernel fragments count by content: editing one without
        # touching the spec must still trigger a rebuild.
        for path in package.referenced_files():
            with open(path, "rb") as f:
                digest.update(f.read())

        self._stamp_kernel_graft(digest, package)
        self._stamp_module(digest, package)
        self._stamp_cross_headers(digest, package)
        self._stamp_uki(digest, package)

        # A package built against another must rebuild when that one
        # changes -- the dependency's digest already carries its own,
        # transitively.
        for name in sorted(depends or {}):
            digest.update(depends[name].encode())

        # Architecture is in the stamp's name, not just its digest, so
        # the amd64 build never mistakes the arm64 build's stamp for its
        # own and deletes its .debs as superseded.
        return os.path.join(self._stamps(), "%s_%s_%s"
                            % (package.name, architecture,
                               digest.hexdigest()[:16]))

    def _stamp_core(self, digest, package, architecture):
        # A set of fields, not order-sensitive: reordering two lines in
        # the spec must not itself trigger a rebuild.
        for part in [str(package.source),
                     ",".join(package.profiles),
                     ",".join(package.options),
                     str(self.cross(package, architecture)),
                     str(package.source_date_epoch),
                     package.revision,
                     str(package.kernel_featureset),
                     str(package.kernel_flavour),
                     # Every base/name pair, so renaming or re-basing one
                     # is a rebuild even if fragment content is unchanged
                     # (content is folded in separately, below).
                     ",".join(sorted("%s/%s" % (base, name)
                             for base, derived in (package.kernel_derived_flavours or {}).items()
                             for name in derived)),
                     # 'kernel_configs' is written straight into the
                     # fragment rather than read from a file, so it must
                     # be hashed by hand here. Not sorted: order decides
                     # which of two edits to the same symbol wins.
                     "\n".join("%s:%s" % (name, "\n".join(lines))
                              for name, lines in package.kernel_configs.items()),
                     str(package.kernel_abi_suffix),
                     str(package.kernel_upstream),
                     str(package.kernel_upstream_sha256),
                     str(package.sha256),
                     # Only this release's pin: another release's pin has
                     # no bearing on what this build produces.
                     str(package.preferences_for(self.distro["release"])),
                     # str(), not join(): 'None' and '[]' must read
                     # differently -- "keeping nothing" vs "keeping all".
                     str(package.kernel_keep_patches
                         if package.kernel_keep_patches is None
                         else sorted(package.kernel_keep_patches)),
                     ",".join(sorted(package.kernel_drop_patches)),
                     ",".join(sorted(package.kernel_build_files)),
                     self.distro["source"],
                     self.distro["release"],
                     architecture,
                     # 'apt-pull-mode' flips fetch() between network and
                     # local vendor repo -- a rebuild under one must not
                     # be mistaken for one done under the other.
                     "\n".join(apt_sources(self.distro, sources=True,
                                           offline=len(offline_suites(self.distro)) > 0)),
                     self.chroot_architecture(package, architecture),
                     # Who signed it: the .dsc/.changes carry the
                     # signature inside, so a different key (or none)
                     # means different files, however identical the .debs
                     # look. A cache built by another key is thus
                     # rebuilt, not adopted -- their signature is not
                     # ours to publish.
                     str(self.signer.fingerprint()
                         if self.signer is not None else None),
                     # Whether this build makes the arch-all binaries,
                     # which depends on what the *other* builds are:
                     # widening 'scope' can move that job elsewhere.
                     str(architecture == self.indep_architecture(package)),
                     # Kernels this module is built against, as named --
                     # adding/removing one changes the binaries produced.
                     # For a kernel built by this spec, what actually
                     # matters is its ABI, which is not knowable here;
                     # that is carried instead by the dependency digest
                     # below, since a module is built after its kernels.
                     ",".join(sorted(package.module_kernels.get(architecture, []))),
                     # What a moving-target reference (e.g.
                     # 'linux-headers-amd64') actually resolved to -- a
                     # security update can move this without the spec
                     # changing at all.
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

    def _stamp_kernel_graft(self, digest, package):
        # A grafted kernel is built by these rules, so they decide the
        # output as much as a fragment does -- and only for a graft.
        if package.kernel_upstream is not None:
            digest.update(kernel.kernel_rules().content)
            digest.update(str(kernel.GRAFT_VERSION).encode())

    def _stamp_module(self, digest, package):
        # A module is built by the packaging seine writes for it -- that
        # decides the output too, so it is hashed by content.
        if package.module:
            digest.update(module.module_packaging()[1])

    def _stamp_cross_headers(self, digest, package):
        # A cross headers package belongs to one kernel and is made up
        # rather than described by the settings above -- its kernel's
        # release changes whenever that kernel does.
        if module.is_cross_package(package):
            digest.update(package.cross_kernel.release.encode())
            digest.update(package.cross_kernel.headers.encode())
            digest.update(module.cross_packaging()[1])

    def _stamp_uki(self, digest, package):
        # A uki package is built from these settings plus the named
        # 'initrd:' artifact's own bytes -- neither is caught above.
        if package.uki:
            digest.update(package.uki_tool.encode())
            digest.update(package.uki_linux_image.encode())
            digest.update(package.uki_cmdline.encode())
            initrd = uki.initrd_path(self.distro, package.uki_initrd)
            # Digests are computed for the whole task graph up front, so
            # an 'after:'-ordered initrd may not be built yet. A missing
            # file can never match a real hash, forcing one rebuild
            # instead of a false cache hit.
            if os.path.isfile(initrd):
                with open(initrd, "rb") as f:
                    digest.update(f.read())
            else:
                digest.update(b"<initrd not yet built>")

    # A hashed file's path, written the way the spec wrote it (relative
    # to the file that declared it) rather than the absolute path
    # 'referenced_files()' uses -- a stamp travels between machines, so
    # an absolute path would only be true on the one that wrote it.
    def _portable_path(self, package, setting, path):
        origin = package.origin_of(setting)
        if origin is None:
            return path
        return os.path.relpath(path, os.path.dirname(origin))

    # The spec content behind one build, redacted and with paths made
    # portable -- what 'cache' shows beside a stamp, so someone can tell
    # what a cached build contains without re-reading the live spec.
    # Not every field 'stamp()' hashes: environment (release,
    # architecture, signer...) is left out, only spec content is here.
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
            # Already absolute/normalised at load time, so no second
            # pass through '_files()' is needed here.
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

    # Where an excerpt lives: same basename as its stamp, but in the
    # sibling STAMPS_SPEC directory rather than beside it.
    def _excerpt_path(self, stamp):
        return os.path.join(self._stamps_spec(),
                            "%s.spec" % os.path.basename(stamp))

    def _record_excerpt(self, stamp, package):
        with open(self._excerpt_path(stamp), "w") as f:
            yaml.dump(self.digest_excerpt(package), f, sort_keys=False)

    # Every package's stamp, in build order, each folding in the stamps
    # of what it is built after -- possible because dependencies are
    # given their digest first. Followed within one architecture only:
    # a host build has no bearing on a target build of the same package.
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

    # What a previous build of this source package left in the
    # repository: each stamp lists its own produced files, so an old
    # build can be undone without guessing which .debs came from where.
    def _previous(self, package, architecture):
        previous = {}
        for stamp in sorted(os.listdir(self._stamps())):
            if stamp.startswith("%s_%s_" % (package.name, architecture)) == False:
                continue
            path = os.path.join(self._stamps(), stamp)
            with open(path, "r") as f:
                previous[path] = [line.strip() for line in f if len(line.strip()) > 0]
        return previous

    # Drops what an earlier build of the same source left behind, but
    # keeps whatever the current build just produced under the same
    # name. Without this the repository would keep every version ever
    # built, and apt would install the highest one offered -- undoing
    # a downgrade that was meant to happen.
    # 'produced' covers every architecture published in this round, not
    # only this one: arch-all binaries can move between architectures
    # when 'scope' changes.
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

    # What a build left in its output directory -- entirely its own.
    def _produced(self, output):
        return sorted(name for name in os.listdir(output)
                      if os.path.isfile(os.path.join(output, name))
                      or os.path.islink(os.path.join(output, name)))

    def _record(self, stamp, produced):
        with open(stamp, "w") as f:
            for name in produced:
                f.write("%s\n" % name)


    # Rebuilds every package the spec asked for. Each gets its own
    # working directory, thrown away after unless --keep was given --
    # what is worth keeping (the .debs) is in the repository by then.
    # One task per package so independent ones build side by side, plus
    # a 'packages' barrier the rest of the build can name regardless of
    # which packages a spec actually has.
    # Built in a chroot of the build architecture, so this needs the
    # host bootstrap, whichever architecture the image itself is for.
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
        # Every build here touches the builder image's own apt, so
        # 'packages-prepare' also waits on a running 'vendor' task
        # (named by Image.shared_tasks() under offline apt-pull-mode),
        # same as it already waits on the host bootstrap.
        needs_vendor = [vendor_task] if vendor_task is not None else []
        # Asked now, not when the graph was built: by the barrier this
        # build has made the rest of the packages, and one just made is
        # not one that was reused.
        reused = self.current(packages)
        if len(pending) == 0:
            # Nothing to build, but maybe something to index: a machine
            # that imported a repository has .debs with no index yet.
            first = []
            if self._indexable():
                first = [Task("packages-prepare",
                              functools.partial(self._prepare, hostBootstrap),
                              needs=["bootstrap-host"] + needs_vendor)]
            # No 'packages-prepare' to carry the vendor wait here, so
            # 'packages' waits on it directly -- otherwise a rerun with
            # nothing pending and nothing to reindex drops the wait, and
            # 'rootfs' (which only waits on 'packages') races it.
            needs = ["bootstrap-host"] + [t.name for t in first]
            if len(first) == 0:
                needs += needs_vendor
            return first + [Task("packages",
                                 functools.partial(self._reused, reused),
                                 needs=needs)]

        tasks = [Task("packages-prepare",
                      functools.partial(self._prepare, hostBootstrap),
                      needs=["bootstrap-host"] + needs_vendor)]

        # 'before'/'after' become task dependencies here, so everything
        # else can build concurrently. A dependent waits on the step that
        # publishes a package, not the one that built it.
        names = {package.name: "deploy:%s" % package.name
                 for package, _, _ in pending}

        # A package's steps declared together: one prepare, one build
        # per architecture, one step publishing them all.
        grouped = collections.OrderedDict()
        for package, architecture, stamp in pending:
            grouped.setdefault(package.name, (package, []))[1].append(
                (architecture, stamp))

        # One fetch task per distinct source, shared by every package
        # naming it -- see _fetch()/_fetch_upstream(). Named
        # 'fetch:<source>', digest-suffixed only if a label collision
        # would otherwise merge two different keys.
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
        # now for the whole group -- see _taken_shared().
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
            # A module is the exception to preparing early: its
            # packaging depends on the ABI of the kernel it names, which
            # does not exist until that kernel is built.
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

            # One publishing step per package, covering every
            # architecture at once: an arch-all binary belongs in every
            # repository but is built by only one architecture, so this
            # step is what holds the whole picture and decides what an
            # earlier build should be forgotten.
            tasks.append(Task(names[name],
                              functools.partial(self._deploy, package,
                                                [a for a, _ in builds]),
                              needs=built))

        return tasks + [Task("packages",
                             functools.partial(self._reused, reused),
                             needs=[t.name for t in tasks])]


    # Packages this build did not have to build, recorded as reused at
    # the barrier -- also reached by --dry-run, which should say so too.
    def _reused(self, current):
        for package, architecture, stamp in current:
            key = self.key(package, architecture)
            entry = Index().hit(PACKAGE, key)
            say(self.options, "package %s reused, made %s"
                              % (key, since(entry.get("made"))))

    # Cache-index key: release/architecture/name, since one source built
    # for two releases (or architectures) is two different sets of .debs.
    def key(self, package, architecture=None):
        return "%s/%s/%s" % (self.distro["release"],
                             architecture or self.distro["architecture"],
                             package.name)

    # Packages this build would leave alone: a stamp exists means the
    # .debs already in the repository were made from these exact inputs.
    def current(self, packages):
        rebuild = self.options.get("rebuild", False)
        if rebuild:
            return []
        return [(p, a, s) for p, a, s in self.stamps(packages)
                if os.path.isfile(s)]

    # Which packages actually need rebuilding, decided before anything
    # is created: reading a stamp is cheap, so a build with nothing
    # pending should not even make a builder image.
    def _pending(self, packages):
        if len(packages) == 0:
            return []
        rebuild = self.options.get("rebuild", False)
        return [(p, a, s) for p, a, s in self.stamps(packages)
                if rebuild or os.path.isfile(s) == False]

    # Whether the repository holds .debs its index does not describe
    # yet -- the index is rebuilt from the directory, not carried
    # between machines, so an imported cache with nothing to rebuild
    # still needs one pass of apt-ftparchive.
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
        # Written before the .debs it describes -- what an import leaves
        # behind when it brings in newer ones.
        described = os.path.getmtime(index)
        return any(os.path.getmtime(os.path.join(repository, name)) > described
                   for name in debs)

    # Every build has the repository in its sources.list, including the
    # very first, when it holds no index yet -- apt still needs one to
    # read, even an empty one, or the build fails before it starts.
    def _prepare(self, hostBootstrap):
        self.builderImage.create(hostBootstrap)
        with locked(self.repository()):
            self._publish_key()
            self.index()

    # The signing key's public half, kept in the repository it signs, so
    # anything mounting the repository can find it without being told
    # where -- and it travels with a cache copied to another machine.
    def _publish_key(self):
        for name in sorted(os.listdir(self.repository())):
            # A key left by a build signed with another key, or none --
            # what answers for this repository is whatever signed it last.
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

    # What fetch() would run, as a key two packages fetching the same
    # bytes can share. None for a cross-headers package (no source of
    # its own; fetch() already takes a different path for it).
    def _fetch_key(self, package):
        if module.is_cross_package(package):
            return None
        # Never shared: a uki package fetches nothing.
        if uki.is_uki_package(package):
            return ("uki", package.name)
        return tuple(self._fetch_args(package))

    # As _fetch_key(), for kernel.fetch_upstream()'s own download -- the
    # tree a grafted kernel is built from.
    def _upstream_key(self, package):
        if package.kernel_upstream is None:
            return None
        return tuple(kernel._upstream_args(package.kernel_upstream))

    # One task name per key: 'prefix:label' normally, digest-suffixed
    # only when a label is already claimed by a different key.
    def _task_name(self, prefix, label, key, used):
        name = "%s:%s" % (prefix, label)
        claimed = used.get(name)
        if claimed is None:
            used[name] = key
            return name
        if claimed == key:
            return name
        return "%s-%s" % (name, hashlib.sha256(repr(key).encode()).hexdigest()[:6])

    # Task body for a shared source fetch. Populates the canonical copy
    # every prepare:<name> task naming this key will copy from --
    # tasks() makes exactly one such task per key, so no lock is needed.
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

    # Copies 'source's contents into existing 'dest'; leaves 'source' for
    # the caller to remove. Symlinks are copied as symlinks, never
    # followed -- a kernel tree is full of them, and dpkg-source cannot
    # represent one turned into a plain file as a patch.
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

    # Gives up 'key''s claim on its canonical fetch, called by every
    # prepare:<name> task that copied from it. The copy is removed once
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

    # Fetch and prepare in one step -- used only for a cross-headers
    # package, which _fetch_key() names no key for and so never goes
    # through tasks()'s fetch:/prepare: split; called directly from
    # _cross_headers_built() instead.
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

    # What tasks()'s prepare:<name> task runs: a copy of the shared
    # fetch(es) named by 'fetch_key'/'upstream_key', turned into a
    # source package the same way _fetched() does.
    def _prepare_source(self, package, fetch_key, upstream_key):
        workdir = tempfile.mkdtemp(dir=ContainerEngine.scratch(),
                                   prefix="source-")
        try:
            self._copy_into(self._shared_fetches[fetch_key], workdir)
            sourcedir = self._source_dir(package.source or package.name, workdir)
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
            # Builds still to be handed this source: with two
            # architectures reading one tree, the first to finish must
            # not be the one that throws it away.
            self._holding[package.name] = len(self.architectures(package))
        return source

    # Turns a fetched tree into the source package builds are handed,
    # plus the date they are all pinned to.
    def _prepared(self, package, workdir, sourcedir):
        # Taken before the graft replaces the changelog it reads from.
        epoch = self.source_date_epoch(package, sourcedir)
        # A cross headers package isn't the kernel it's built from: no
        # graft, no patches, no local changelog entry on someone else's
        # source.
        if module.is_cross_package(package):
            module.extend_cross_headers(
                self, package, sourcedir, epoch,
                os.path.join(os.path.dirname(sourcedir), module.CROSS_FETCHED))
            dsc = self.source_package(package, sourcedir)
            self._stage_source(package, os.path.dirname(sourcedir), dsc)
            return dsc, epoch

        if package.kernel_upstream is not None:
            sourcedir = kernel.graft(self, package, workdir, sourcedir, epoch)
        # Before patches/local changelog: both need a debian/ directory,
        # which for a module or uki wrapper is what this step creates.
        module.extend(self, package, sourcedir, epoch)
        uki.extend(self, package, sourcedir, epoch)
        self.patch(package, sourcedir, epoch)
        # After them, since the series may already cover it.
        if package.kernel_upstream is not None:
            kernel.module_lds_patch(package, sourcedir)
        self.local_release(package, sourcedir, epoch)
        kernel.extend(self, package, sourcedir, self.architectures(package))
        dsc = self.source_package(package, sourcedir)
        # Signed before staging or building: a .dsc carries its
        # signature inside, so signing here is what the repository (and
        # any machine given the cache) ends up serving.
        if self.signer is not None:
            self.signer.clearsign(os.path.join(os.path.dirname(sourcedir), dsc))
        self._stage_source(package, os.path.dirname(sourcedir), dsc)
        return dsc, epoch

    # Puts the source package where publishing will find it -- not where
    # it was built, since the working directory dies with the last build
    # using it, before publishing runs. Once per package, not per build:
    # a source package is not built for one architecture.
    def _stage_source(self, package, workdir, dsc):
        staged = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="source-")
        for name in self.source_files(workdir, dsc):
            shutil.copy(os.path.join(workdir, name), staged)
        self._source_packages[package.name] = staged

    # Gives up one build's claim on a fetched source; the last to do so
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

    # Publishes what was built: the repository, and its apt index.
    # Both only after a successful build -- a stamp for a failed build
    # would skip it next time, and dropping the old build too early
    # would leave the repository with neither. One at a time across the
    # whole machine, since an index read mid-rewrite by another build's
    # apt fails in a confusing way, much later.
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

        # Everything all these builds made, so one architecture's step
        # cannot delete another's files as superseded.
        everything = set()
        for _, _, produced in built.values():
            everything.update(produced)

        # The source package these builds were handed, published once
        # regardless of how many builds there were, but recorded in
        # every build's stamp so it is retired with the last of them.
        staged = self._source_packages.pop(package.name, None)
        sources = [] if staged is None else sorted(os.listdir(staged))
        everything.update(sources)

        # The .changes describes what was produced and its hashes, so
        # it is what gets signed; signed before anything is moved.
        if self.signer is not None:
            for _, output, produced in built.values():
                for name in produced:
                    if name.endswith(".changes"):
                        self.signer.clearsign(os.path.join(output, name))

        # Whether this replaced a file already in the repository --
        # decides whether the index cache can be trusted; see index().
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
                    # Removed first: a plain file would be replaced by
                    # the move anyway, but sbuild leaves a symlink beside
                    # every build log pointing at the newest one, and a
                    # move cannot overwrite that.
                    if os.path.lexists(destination):
                        os.remove(destination)
                        replaced = True
                    shutil.move(os.path.join(output, name), destination)
                shutil.rmtree(output, ignore_errors=True)
                self._forget(package, architecture, everything)
                self._record(stamp, sorted(set(produced) | set(sources)))
                self._record_excerpt(stamp, package)
            # Once, at the end: an index made while a build is half
            # moved in describes neither what was there nor what is.
            self.index(cached=replaced == False)

        for architecture in sorted(built):
            key = self.key(package, architecture)
            Index().made(PACKAGE, key)
            say(self.options, "package %s made" % key)

    # One package: patched, built, and recorded as built. The chroot is
    # shared with whatever else is building at the same time.
    def _rebuild(self, package, architecture, stamp):
        # A kernel built by this spec only knows its own ABI once its
        # source is prepared, so cross headers are made here rather than
        # planned earlier -- 'after' guarantees the kernel is done by now.
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

        # Where this build writes; nowhere anything else does.
        output = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="built-")
        try:
            print("rebuilding '%s' for %s" % (package.source, architecture))
            self.build(package, workdir, dsc, epoch, architecture, output)

            # Handed to the step that publishes it, since a dependent
            # package needs this one's .deb in the repository to build
            # against -- a later moment than this build finishing.
            self._built[(package.name, architecture)] = (stamp, output)
        except:
            # Left in place and announced: this is where sbuild's own
            # build log (and whatever the chroot installed) can be read.
            # Lives in scratch space, cleared by 'seine cache clear
            # scratch', not tidied away here.
            print("keeping '%s': what the failed build of '%s' wrote, its "
                  "build log included" % (output, package.name))
            raise
        finally:
            self._release(package)



# Makes rebuilt packages visible to apt everywhere it runs: the build
# chroot, the rootfs container, the imager's own containers.
#
# The repository is trusted (unsigned, built locally moments ago) and
# preferred over the distro's, matching on an empty origin -- which is
# what a file:// repository has.
#
# 900, not 1001: above 1000 apt will *downgrade* an already-installed
# package to match a lower version in the repository, breaking any
# chroot that already has a newer one. 900 is also no longer needed for
# its original purpose, since every rebuilt package's local revision
# already sorts above the distro's -- it only breaks ties between
# origins and never touches an installed package.
SOURCES_LIST = "/etc/apt/sources.list.d/seine-packages.list"
PREFERENCES  = "/etc/apt/preferences.d/seine-packages"

def apt_preferences_command():
    return " && ".join([
        "echo 'Package: *' > %s" % PREFERENCES,
        "echo 'Pin: origin \"\"' >> %s" % PREFERENCES,
        "echo 'Pin-Priority: 900' >> %s" % PREFERENCES,
    ])

# A package's own preferences, in a fragment of their own beside
# seine's -- not named 'seine-package', which would be a confusing
# prefix of the file above it.
PACKAGE_PREFERENCES = "/etc/apt/preferences.d/seine-build"

# sbuild expands percent escapes in the commands it runs ('%s' the
# interactive shell, '%%' a literal percent) before any shell sees them,
# so a command meant to arrive intact needs its percents doubled here.
def sbuild_command(command):
    return command.replace("%", "%%")

# printf, not echo: the preferences text is multi-line, and quote()
# is what makes it survive the shell unaltered.
def package_preferences_command(preferences):
    if preferences.endswith("\n") == False:
        preferences += "\n"
    return "printf '%%s' %s > %s" % (shlex.quote(preferences),
                                     PACKAGE_PREFERENCES)

# Where a repository's key is installed for whatever reads from it.
# Under apt's own keyrings directory, not pointed at the mounted
# repository, since it must stay in the image after the repository
# itself is gone from sources.list.
KEYRINGS = "/etc/apt/keyrings"

def apt_configuration(*mountpoints, keyring=None):
    # Unsigned is trusted (built here a moment ago); signed is verified.
    if keyring is None:
        options = "[trusted=yes]"
        install = ""
    else:
        options = "[signed-by=%s/%s]" % (KEYRINGS, keyring)
        install = "install -D -m 0644 %s/%s %s/%s && " % (
            mountpoints[0], keyring, KEYRINGS, keyring)
    # deb-src beside deb: a kernel built here is the only place its
    # source can be fetched from, needed for headers built on another
    # architecture from that same source.
    lines = " && ".join(
        "echo 'deb%s %s file:%s ./' %s %s"
        % (kind, options, mountpoint,
           ">" if (index == 0 and kind == "") else ">>", SOURCES_LIST)
        for index, mountpoint in enumerate(mountpoints)
        for kind in ["", "-src"])
    return "%s%s && %s" % (install, lines, apt_preferences_command())

# The key a repository carries, if it is actually signed -- both must be
# true: a cache carried to another machine brings only the public key,
# and configuring 'signed-by' on that alone (with no signature to check)
# is a repository apt refuses to read.
def keyring(distro):
    where = repository(distro)
    if os.path.isfile(os.path.join(where, "InRelease")) == False:
        return None
    for name in sorted(os.listdir(where)):
        if name.endswith(".gpg") and name.startswith("Release") == False:
            return name
    return None

# sources.list and the pin are removed, since the repository lives on
# the build machine only. The keyring stays: it says which key answers
# for packages that an image updated later, from a repository signed
# with the same key, still needs to trust.
def apt_deconfiguration():
    return "rm -f %s %s" % (SOURCES_LIST, PREFERENCES)

# The same apt configuration for a Dockerfile-built image: a layer
# setting it up, and the bind mounts making the repositories readable
# during that build. Both empty when the spec rebuilt nothing.
def apt_setup_layer(distro):
    if has_packages(distro) == False:
        return ""
    return "RUN %s\n" % apt_configuration(REPOSITORY, keyring=keyring(distro))

def build_volumes(distro):
    if has_packages(distro) == False:
        return []
    return ["-v", "%s:%s:ro" % (repository(distro), REPOSITORY)]

# Where a spec's rebuilt packages live: one flat repository per release,
# holding every architecture built for, like a distro's own archive.
def repository(distro):
    return ContainerEngine.packages(distro["release"])

# Whether there is anything to install: a spec with no 'packages'
# section leaves this directory without an index.
def has_packages(distro):
    return os.path.isfile(os.path.join(repository(distro), "Packages"))

# What answers for a source's integrity, if anything does --
# --require-hashes asks this to be non-None for every package, and is
# told before a byte is fetched rather than after.
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

# Every source nothing vouches for, named with the file that wrote it.
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

def parse(spec, check_uki=True):
    packages = spec.get("packages", [])
    if type(packages) != type([]):
        raise ValueError("'packages' shall be a list of source packages!")

    parsed = [Package(p, i + 1) for i, p in enumerate(packages)]
    # A package with no 'source' describes nothing to build -- that
    # belongs under 'defaults' instead. A uki package is the exception:
    # it generates its own source.
    for package in parsed:
        if package.source is None and uki.is_uki_package(package) == False:
            raise ValueError(
                "package '%s' has no 'source' to build from. An entry under "
                "'packages' asks for a package to be built; one that only "
                "describes a package goes under 'defaults'." % package.name)
    # Before ordering, so a bad kernel reference is reported here rather
    # than by 'after' failing to find it.
    module.check_references(parsed)
    module.depend_on_kernels(parsed)
    ordered = propagate(order(parsed))
    module.check_kernels(ordered, spec)
    # Skipped for a 'multiconfig:' group whose predecessor is declared
    # via 'after:' elsewhere -- uki.extend() still checks for real once
    # the package actually builds.
    if check_uki:
        uki.check_initrds(ordered, spec)
    return ordered

# Carries a package's scope down to what it is built after: a dependency
# built for the host must itself be built for the host, or the
# dependent would link against an architecture it never asked to build.
# An explicit scope on the dependency is never widened -- that is an
# error instead, since silently building for an unwanted architecture is
# worse than failing loudly. Walked in reverse build order, so one pass
# carries a role the length of a chain.
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

# Orders packages for building. 'priority' is a preference; 'before' and
# 'after' are hard constraints, naming another package by name -- and
# constraints always win over priority.
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

    # Kahn's algorithm: among packages whose predecessors are all built,
    # take the highest priority, then the earliest listed.
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
        # Direct predecessors only: each already carries its own
        # dependencies' digests, so reachability need not be computed.
        packages[chosen].depends = [packages[i] for i in predecessors[chosen]]
        ordered.append(packages[chosen])
        remaining.discard(chosen)
    return ordered

# A 'before'/'after' entry must name a real package in this
# specification -- a typo here should fail loudly, not silently build
# in the wrong order.
def _referenced(indexes, package, name, setting):
    if name not in indexes:
        raise package._error(
            "'%s' names '%s', which no package in this specification builds"
            % (setting, name))
    others = [i for i in indexes[name] if i != package.index - 1]
    if len(others) == 0:
        raise package._error("'%s' names the package itself" % setting)
    return others
