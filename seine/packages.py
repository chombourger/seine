# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import collections
import fnmatch
import functools
import hashlib
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
import tomllib
import yaml

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

from seine        import signing
from seine.cache_index import PACKAGE, Index, say, since
from seine.sbuild import BuilderImage
from seine.tasks  import Task
from seine.sbuild import OUTPUT
from seine.sbuild import REPOSITORY
from seine.sbuild import SbuildChroot
from seine.utils  import apt_sources
from seine.utils  import locked
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
# A kernel is configured rather than patched: Debian builds its kernels
# from a stack of kconfig files under debian/, so a fragment appended to
# the right one is both easier to write and less likely to conflict with
# the next point release than a patch would be.
EXTENSIONS = {
    "kernel": ["build-files", "config", "drop-patches", "featureset",
               "flavour", "keep-patches", "upstream", "upstream-sha256"],
    "module": ["build", "make-vars", "modules"],
}

# Which kernels a module is built against, said once per architecture:
# 'amd64-kernels', 'arm64-kernels'. Keyed by architecture rather than
# listed flat so that a specification building for one architecture never
# has to resolve another's kernels -- a name it would have to reach into
# a foreign apt index to make sense of, for a build it is not doing.
#
# The shape is checked, not the architecture: seine takes the
# 'distribution' architecture as written and holds no list of Debian's. A
# misspelt one is caught all the same, by the build finding no kernels for
# the architecture it is building, in a message naming what was found.
MODULE_KERNELS = re.compile(r"^([a-z][a-z0-9]*(-[a-z0-9]+)*)-kernels$")

# What a module is built against, named by its headers package rather
# than by the image package beside it: the headers are what a module
# compiles and links against, and naming the image would be naming a
# thing the build never opens.
MODULE_IMAGE_PREFIX = "linux-image-"

# Where a kernel tree comes from when it is not the one the distribution
# packages: 'extends: kernel: upstream:'. The distribution's debian/ is
# kept and grafted onto that tree, so what comes out carries Debian's
# package names, maintainer scripts and headers layout -- a replacement
# for the distribution's kernel rather than a parallel one beside it.
#
# The same notation a package's 'source' uses, minus apt://, which names
# a source package rather than a tree:
#
#   https://cdn.kernel.org/.../linux-<version>.tar.xz  a release tarball
#   git://host/bsp.git;rev=<commit>                    a tree, BSP or not
UPSTREAM_SCHEMES = ["git", "https"]
TARBALL_SUFFIXES = [".tar.xz", ".tar.gz", ".tar.bz2"]

# What seine knows about Debian's kernel patches -- which of them are
# packaging, and which are never kept -- lives beside the code in a data
# file, so that moving with the distribution is an edit rather than a
# patch. What each setting means is written down there.
#
# Everything outside debian/ is dropped rather than fought with, which
# needs no data to say: bugfix/* against a newer tree is a backport it
# already has, and features/* is keyed to config symbols that oldconfig
# drops along with the patch.
KERNEL_RULES = os.path.join(os.path.dirname(__file__), "data", "kernel.yml")

KernelRules = collections.namedtuple("KernelRules",
                                     ["build_files", "drop_patches", "content"])

# Read once, and kept with the bytes it was read from: those bytes are
# part of what decides whether a grafted kernel needs rebuilding, so a
# change to the rules is a change to the kernel they produce.
@functools.lru_cache(maxsize=None)
def kernel_rules():
    with open(KERNEL_RULES, "rb") as f:
        content = f.read()
    rules = yaml.safe_load(content) or {}
    for setting in ["build-files", "drop-patches"]:
        if type(rules.get(setting)) != type([]):
            raise ValueError("%s: '%s' shall be a list"
                             % (KERNEL_RULES, setting))
    return KernelRules(rules["build-files"], rules["drop-patches"], content)

# The rules' patterns and whatever the specification added to them, as one
# expression. Added rather than replacing: what makes a kernel build is
# the same wherever the tree came from, and a packaging reaching
# somewhere else reaches there as well as here, not instead of it.
#
# 'extra' is a tuple so that this can be cached: the expression is the
# same for every patch in the series.
@functools.lru_cache(maxsize=None)
def build_files(extra=()):
    return re.compile("|".join(list(kernel_rules().build_files) + list(extra)))

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

# Debian's build profiles for a kernel's tools. The first drops
# linux-kbuild with them, which a module built against that kernel needs;
# the second drops the same tools and keeps it.
NO_TOOLS = "pkg.linux.notools"
MIN_TOOLS = "pkg.linux.mintools"

# The tree named by 'extends: kernel: upstream:'. It carries the same
# fields a Package does -- scheme, name, parameters -- so the code that
# fetches one fetches the other.
class Upstream:
    def __init__(self, uri, error):
        if type(uri) != type(""):
            raise error("'extends: kernel: upstream' shall be a string")
        if "://" not in uri:
            raise error(
                "'extends: kernel: upstream' has no URI scheme: expected one "
                "of %s" % ", ".join("%s://" % s for s in UPSTREAM_SCHEMES))

        self.uri = uri
        self.scheme, rest = uri.split("://", 1)
        self.parameters = {}
        if self.scheme not in UPSTREAM_SCHEMES:
            raise error(
                "'extends: kernel: upstream' has unsupported URI scheme "
                "'%s://', expected one of %s"
                % (self.scheme, ", ".join("%s://" % s for s in UPSTREAM_SCHEMES)))

        if self.scheme == "https":
            if not any(rest.endswith(s) for s in TARBALL_SUFFIXES):
                raise error(
                    "'extends: kernel: upstream' over https shall point at a "
                    "source tarball ending in %s" % ", ".join(TARBALL_SUFFIXES))
            self.name = os.path.basename(rest)
        else:
            location, *parameters = rest.split(";")
            for parameter in parameters:
                key, _, value = parameter.partition("=")
                self.parameters[key] = value
            # Same reason a git 'source' is pinned: a branch name moves,
            # and a kernel rebuild is not a thing to repeat by accident,
            # or to skip when it should not have been.
            if len(self.parameters.get("rev", "")) == 0:
                raise error(
                    "'extends: kernel: upstream' git trees shall be pinned "
                    "with ';rev=<commit>'")
            self.name = os.path.basename(location).removesuffix(".git")

    def __str__(self):
        return self.uri

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
        self.revision = spec.get("revision", DEFAULT_REVISION)
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

        # Modules are built natively for now, which off the image's own
        # architecture means emulated. The headers a module builds against
        # pull in linux-kbuild, whose fixdep and modpost are compiled for
        # the kernel's architecture, so a cross chroot has nothing it can
        # run.
        if self.module and self.cross is None:
            self.cross = False

    def _error(self, message):
        return ValueError("package #%d ('%s'): %s" % (self.index, self.source, message))

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
                if kind == "module" and MODULE_KERNELS.match(setting):
                    continue
                expected = ", ".join(sorted(EXTENSIONS[kind]))
                if kind == "module":
                    expected += ", <architecture>-kernels"
                raise self._error(
                    "'extends: %s' has no '%s' setting, expected one of %s"
                    % (kind, setting, expected))

        kernel = extends.get("kernel", {})
        self.kernel_config = self._parse_list(kernel, "config")
        self.kernel_flavour = kernel.get("flavour")
        self.kernel_featureset = kernel.get("featureset", DEFAULT_FEATURESET)
        self.kernel_upstream = None
        if "upstream" in kernel:
            if self.scheme != "apt":
                raise self._error(
                    "'extends: kernel: upstream' grafts the distribution's "
                    "debian/ onto another tree, so the package it is set on "
                    "has to be the distribution's own kernel source")
            self.kernel_upstream = Upstream(kernel["upstream"], self._error)
        self.kernel_upstream_sha256 = self._parse_digest(kernel, "upstream-sha256")
        # None, not a list of globs: with nothing said, which patches are
        # the packaging is decided by what they touch rather than by their
        # names. An explicit empty list is a different answer -- "keep none
        # of them" -- so presence decides, not emptiness.
        self.kernel_keep_patches = None
        if "keep-patches" in kernel:
            self.kernel_keep_patches = self._parse_list(kernel, "keep-patches")
        # Subtracted from what 'keep-patches' selected, since that only
        # adds: taking one patch out of 'debian/*' would otherwise mean
        # writing out the thirty-odd being kept.
        self.kernel_drop_patches = self._parse_list(kernel, "drop-patches")
        # Added to the patterns seine ships, for a packaging that builds
        # through files Debian's does not touch. Checked here rather than
        # where the series is read: a bad expression should be reported by
        # the specification that wrote it, not by the patch it first fails
        # to match.
        self.kernel_build_files = self._parse_list(kernel, "build-files")
        for pattern in self.kernel_build_files:
            try:
                re.compile(pattern)
            except re.error as e:
                raise self._error(
                    "'extends: kernel: build-files' has '%s', which is not a "
                    "regular expression: %s" % (pattern, e))
        for setting, value in [("flavour", self.kernel_flavour),
                               ("featureset", self.kernel_featureset)]:
            if value is not None and type(value) != type(""):
                raise self._error(
                    "'extends: kernel: %s' shall be a string" % setting)
        self.kernel = "kernel" in extends

        module = extends.get("module", {})
        self.module = "module" in extends
        # Where the module's own makefile is, for a tree that keeps it in
        # a subdirectory -- NVIDIA's is under kernel-open. The tree's root
        # when nothing says otherwise.
        self.module_build = module.get("build", ".")
        if type(self.module_build) != type(""):
            raise self._error("'extends: module: build' shall be a string")
        # Which .ko files the build is expected to produce. Named rather
        # than found: a build that quietly produced none, or produced one
        # of two, would otherwise make a package that installs nothing and
        # looks exactly as it should.
        self.module_modules = self._parse_list(module, "modules")
        for name in self.module_modules:
            if type(name) != type(""):
                raise self._error(
                    "'extends: module: modules' shall be a list of module "
                    "names")
        self.module_make_vars = self._parse_make_vars(module)
        self.module_kernels = self._parse_module_kernels(module)
        return extends

    # What to put on the make command line, for a tree that needs telling
    # where things are -- NVIDIA's wants SYSSRC. Taken as written: what a
    # module's makefile accepts is its own business.
    def _parse_make_vars(self, module):
        variables = module.get("make-vars", {})
        if type(variables) != type({}):
            raise self._error(
                "'extends: module: make-vars' shall be a dictionary of "
                "variables to pass to make")
        for name, value in variables.items():
            if type(value) not in [type(""), type(0)]:
                raise self._error(
                    "'extends: module: make-vars' has '%s', whose value is "
                    "neither a string nor a number" % name)
        return {name: str(value) for name, value in variables.items()}

    # The kernels this module is built against, per architecture.
    #
    # Each is named by its headers package -- 'apt://linux-headers-amd64',
    # 'apt://linux-headers-6.12.101+deb13-amd64' -- or by the name of a
    # kernel this specification builds, whose headers seine knows how to
    # name for itself.
    def _parse_module_kernels(self, module):
        kernels = {}
        for setting, listed in module.items():
            architecture = MODULE_KERNELS.match(setting)
            if architecture is None:
                continue
            if type(listed) != type([]):
                raise self._error("'extends: module: %s' shall be a list of "
                                  "kernels" % setting)
            for kernel in listed:
                if type(kernel) != type(""):
                    raise self._error(
                        "'extends: module: %s' shall be a list of kernels, "
                        "named as strings" % setting)
                self._check_module_kernel(setting, kernel)
            kernels[architecture.group(1)] = list(listed)
        return kernels

    # A kernel is named by what a module is built against, which is a
    # headers package. The image package beside it holds a compiled
    # kernel and nothing to compile against, so naming it is a mistake
    # worth spelling out rather than a name to quietly translate: which
    # headers package an image package belongs to is a question with more
    # than one answer where featuresets are involved.
    def _check_module_kernel(self, setting, kernel):
        name = kernel
        if "://" in kernel:
            scheme, _, name = kernel.partition("://")
            if scheme != "apt":
                raise self._error(
                    "'extends: module: %s' names '%s': a kernel is named by "
                    "an 'apt://' headers package, or by a kernel this "
                    "specification builds" % (setting, kernel))
        if name.startswith(MODULE_IMAGE_PREFIX):
            raise self._error(
                "'extends: module: %s' names '%s', which is a kernel image. "
                "A module is built against headers: name "
                "'linux-headers-%s' instead."
                % (setting, kernel, name[len(MODULE_IMAGE_PREFIX):]))

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

    def kernel_config_files(self):
        return self._files(self.kernel_config)

    def _files(self, names):
        return [os.path.normpath(n) for n in names]

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

# Where the container that clones finds the ssh agent of the user seine
# runs as, and the hosts that user already trusts. Fixed names rather than
# the paths they have on the host, which nothing here needs to preserve.
SSH_AUTH_SOCK = "/ssh-agent/sock"
SSH_KNOWN_HOSTS = "/root/.ssh/known_hosts"

class Builder:
    def __init__(self, distro, options, builderImage):
        self.builderImage = builderImage
        self.distro = distro
        self.options = options
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
        ssh_volumes, environment = self._ssh(package)
        self.builderImage.exec(
            self._fetch_args(package), volumes=volumes + ssh_volumes,
            workdir=WORKDIR, environment=environment)
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

    # The tarball a kernel is grafted onto, which is fetched on its own and
    # has a hash of its own to be checked against.
    def _verify_upstream(self, package, staging):
        upstream = package.kernel_upstream
        if upstream is None or upstream.scheme != "https":
            return
        self._verify(package, staging, os.path.basename(upstream.uri),
                     package.kernel_upstream_sha256, "upstream-sha256")

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

    # Where the tree named by 'upstream' is unpacked. Hidden, so the
    # unpacked distribution source stays the only thing _source_dir()
    # can find while both are on disk.
    UPSTREAM = ".upstream"

    # The tree a kernel is grafted onto, fetched with the source it will be
    # grafted into rather than when the graft happens: it is the largest
    # download seine makes, and there is no reason for it to wait for a
    # build slot.
    def fetch_upstream(self, package, workdir):
        upstream = package.kernel_upstream
        if upstream is None:
            return
        staging = os.path.join(workdir, Builder.UPSTREAM)
        os.makedirs(staging, exist_ok=True)
        print("fetching '%s'" % upstream)
        self.builderImage.exec(
            self._upstream_args(upstream), volumes=[(staging, WORKDIR)],
            workdir=WORKDIR)
        self._verify_upstream(package, staging)

    # Puts the distribution's packaging on a kernel tree it was not written
    # for, and returns the grafted tree.
    #
    # Everything that makes the resulting .debs true replacements lives in
    # debian/ -- package names, ABI naming, headers split, maintainer
    # scripts -- and none of it is in the tree. So debian/ moves across
    # whole, the series is cut to the patches that touch the build system,
    # and the changelog is given the version of the tree being built.
    #
    # The tree is repackaged as the orig tarball rather than Debian's,
    # which is what makes this a "3.0 (quilt)" source at all: tree and
    # orig are identical outside debian/, so nothing has to be expressed
    # as a patch that is not already one.
    def graft(self, package, workdir, sourcedir, epoch):
        upstream = package.kernel_upstream
        staging = os.path.join(workdir, Builder.UPSTREAM)
        if os.path.isdir(staging) == False:
            self.fetch_upstream(package, workdir)

        print("grafting '%s' onto '%s'" % (package.source, upstream))
        tree = self._source_dir(str(upstream), staging)

        source = self._source_name(sourcedir)
        version = self._kernel_version(tree)
        grafted = os.path.join(workdir, "%s-%s" % (source, version))

        shutil.move(os.path.join(sourcedir, "debian"),
                    os.path.join(tree, "debian"))
        shutil.rmtree(sourcedir)
        shutil.move(tree, grafted)
        shutil.rmtree(staging, ignore_errors=True)

        self._filter_series(package, grafted)
        self._check_series(package, grafted)
        self._graft_release(package, grafted, source, version, epoch)

        # What the distribution's own source left behind describes a
        # version that is no longer being built. dpkg-source would pick
        # the wrong orig tarball out of it, when it did not simply refuse
        # to choose.
        for name in sorted(os.listdir(workdir)):
            path = os.path.join(workdir, name)
            if os.path.isfile(path):
                os.unlink(path)

        # gzip, not the xz Debian ships: this tarball is written once, read
        # once by dpkg-source and thrown away, so xz buys nothing that is
        # ever stored. .git goes too -- dpkg-source ignores it in the tree,
        # so keeping it in the orig would only make the two differ. Packed
        # from the parent, so the tarball holds the one top-level directory
        # an orig tarball is expected to have.
        tarball = "%s_%s.orig.tar.gz" % (source, version)
        tree = os.path.basename(grafted)
        self.builderImage.exec(
            ["sh", "-c", "tar --exclude=%s/debian --exclude=%s/.git "
                         "--sort=name --mtime=@%d --owner=0 --group=0 "
                         "--numeric-owner -czf %s %s"
                         % (tree, tree, epoch, tarball, tree)],
            volumes=[(workdir, WORKDIR)], workdir=WORKDIR)
        return grafted

    def _upstream_args(self, upstream):
        if upstream.scheme == "https":
            return ["sh", "-c", "curl -sSfL -O %s && tar -xf %s"
                    % (shlex.quote(upstream.uri), shlex.quote(upstream.name))]

        protocol = upstream.parameters.get("protocol", "https")
        location = upstream.uri.split("://", 1)[1].split(";")[0]
        args = ["git", "clone"]
        if "branch" in upstream.parameters:
            args += ["--branch", upstream.parameters["branch"]]
        args += ["%s://%s" % (protocol, location), upstream.name]
        return ["sh", "-c", "%s && cd %s && git checkout --detach %s" % (
            " ".join(args), upstream.name, upstream.parameters["rev"])]

    # The source package name, which is not the tree's: Debian's kernel
    # tree unpacks as linux-<version> but the source is 'linux', and the
    # orig tarball and directory both have to be named for the source.
    def _source_name(self, sourcedir):
        with open(os.path.join(sourcedir, "debian", "changelog"), "r") as f:
            heading = re.match(r"^(\S+) ", f.readline())
        if heading is None:
            raise ValueError("debian/changelog does not start with a source name")
        return heading.group(1)

    # The version of a kernel tree, read from its Makefile. EXTRAVERSION is
    # deliberately left out: a BSP commonly puts its own name there, and a
    # Debian upstream version has nowhere to put a '-rc1' that would not be
    # read back as the Debian revision.
    def _kernel_version(self, tree):
        fields = {}
        with open(os.path.join(tree, "Makefile"), "r") as f:
            for line in f:
                field = re.match(r"^(VERSION|PATCHLEVEL|SUBLEVEL)\s*=\s*(\d+)",
                                 line)
                if field:
                    fields[field.group(1)] = field.group(2)
                if len(fields) == 3:
                    break
        if len(fields) != 3:
            raise ValueError(
                "'%s' has no VERSION/PATCHLEVEL/SUBLEVEL in its Makefile, so "
                "it is not a kernel tree" % tree)
        return "%s.%s.%s" % (fields["VERSION"], fields["PATCHLEVEL"],
                             fields["SUBLEVEL"])

    # Cuts the distribution's patch series down to what is being kept.
    #
    # A glob that matches nothing is an error rather than a no-op: the
    # series is restructured from one release to the next, and a
    # specification naming a directory since renamed would otherwise
    # silently build a kernel without the patches it meant to keep.
    def _filter_series(self, package, sourcedir):
        path = os.path.join(sourcedir, "debian", "patches", "series")
        if os.path.isfile(path) == False:
            return

        with open(path, "r") as f:
            names = [line.strip() for line in f]

        patches = os.path.join(sourcedir, "debian", "patches")
        dropped = kernel_rules().drop_patches + package.kernel_drop_patches
        selected = [n for n in names if len(n) > 0 and not n.startswith("#")]

        kept = []
        for name in selected:
            if self._matches(name, dropped):
                continue
            if package.kernel_keep_patches is None:
                if self._packaging_patch(package, name,
                                         os.path.join(patches, name)):
                    kept.append(name)
            elif self._matches(name, package.kernel_keep_patches):
                kept.append(name)

        # Only the globs the specification wrote are checked. The rules'
        # own 'drop-patches' is not one of them: a source that carries no
        # DFSG exclusions is an ordinary thing, not a specification that
        # has gone stale.
        for setting, globs, against in [
                ("keep-patches", package.kernel_keep_patches or [], kept),
                ("drop-patches", package.kernel_drop_patches, selected)]:
            for glob in globs:
                if not any(fnmatch.fnmatch(n, glob) for n in against):
                    raise ValueError(
                        "package '%s': 'extends: kernel: %s' has '%s', which "
                        "matches none of the %d patches '%s' carries"
                        % (package.source, setting, glob, len(selected),
                           package.source))

        print("keeping %d of %d patches" % (len(kept), len(names)))
        with open(path, "w") as f:
            for name in kept:
                f.write("%s\n" % name)

    # Whether the patches being kept apply to this tree, asked before
    # dpkg-source is asked. dpkg-source stops at the first patch that does
    # not, so left to it the question is answered one patch per build.
    #
    # quilt rather than plain patch: a series is cumulative, a patch may
    # depend on the one before it, and quilt is what can put the tree back
    # afterwards. Fuzz is zero because that is what dpkg-source allows, so
    # what is found here is what it would find.
    SERIES_CHECK = """
export QUILT_PATCHES=debian/patches QUILT_PATCH_OPTS='-F 0'
while [ -n "$(quilt next 2>/dev/null)" ]; do
    patch="$(quilt next)"
    quilt push -q >/dev/null 2>&1 && continue
    echo "$patch"
    quilt delete -n >/dev/null 2>&1 || break
done
quilt pop -a -q >/dev/null 2>&1
rm -rf .pc
"""

    def _check_series(self, package, sourcedir):
        workdir = os.path.dirname(sourcedir)
        output = self.builderImage.output(
            ["sh", "-c", Builder.SERIES_CHECK], volumes=[(workdir, WORKDIR)],
            workdir="%s/%s" % (WORKDIR, os.path.basename(sourcedir)))
        # quilt names a patch by its path from the source tree, and the
        # series names it from debian/patches. Reported as the series
        # writes it, since that is what 'drop-patches' is matched against.
        failed = [n.strip().removeprefix("debian/patches/")
                  for n in output.decode().split("\n") if len(n.strip()) > 0]
        if len(failed) == 0:
            return

        # Each one is named with what it was trying to change, since that
        # is what says whether the tree has since got it from upstream --
        # which is the common reason a packaging patch stops applying.
        patches = os.path.join(sourcedir, "debian", "patches")
        report = []
        for name in failed:
            report.append("  %s" % name)
            for touched in sorted(self._touches(os.path.join(patches, name))):
                report.append("      %s" % touched)
        raise ValueError(
            "package '%s': %d packaging patches do not apply to '%s':\n%s\n"
            "Add them to 'extends: kernel: drop-patches' if the tree already "
            "has what they fix."
            % (package.source, len(failed), package.kernel_upstream,
               "\n".join(report)))

    # Whether a patch is part of the packaging, decided by what it changes:
    # a patch touching only build files is what the packaging needs to
    # build at all, and one reaching into C source is changing the kernel
    # itself. A patch touching both counts as the second, since taking it
    # means taking the kernel change too. Only debian/ is looked at:
    # bugfix/ and features/ are backports, and one touching only a
    # makefile is still something a newer tree is expected to have.
    def _packaging_patch(self, package, name, path):
        if name.startswith("debian/") == False:
            return False
        touched = self._touches(path)
        matches = build_files(tuple(package.kernel_build_files))
        return len(touched) > 0 and all(matches.search(f) for f in touched)

    # The files a patch changes, as it names them on its '+++' lines. The
    # leading component goes: these apply with -p1, and what is in front of
    # the path is 'a/', 'b/' or the name of a tree, depending on how the
    # patch was made.
    def _touches(self, path):
        touched = set()
        with open(path, "r", errors="replace") as f:
            for line in f:
                if not line.startswith("+++ "):
                    continue
                name = line[4:].split("\t")[0].strip()
                if name == "/dev/null":
                    continue
                touched.add(name.split("/", 1)[1] if "/" in name else name)
        return touched

    def _matches(self, name, globs):
        return any(fnmatch.fnmatch(name, glob) for glob in globs)

    # Says in the changelog which tree was built, and gives the package the
    # version of that tree rather than of the packaging it borrowed. Without
    # it the .debs would claim to be the distribution's kernel at the
    # distribution's version while holding another one entirely.
    # local_release() runs after this and adds the local revision on top.
    def _graft_release(self, package, sourcedir, source, version, epoch):
        path = os.path.join(sourcedir, "debian", "changelog")
        with open(path, "r") as f:
            changelog = f.read()

        date = format_datetime(datetime.fromtimestamp(epoch, timezone.utc))
        entry = ("%s (%s-1) UNRELEASED; urgency=medium\n\n"
                 "  * Upstream %s, built with the packaging of %s.\n\n"
                 " -- %s <%s>  %s\n\n"
                 % (source, version, package.kernel_upstream, package.source,
                    GIT_NAME, GIT_EMAIL, date))
        with open(path, "w") as f:
            f.write(entry + changelog)

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
    #
    # Per architecture the package is built for, which for a kernel is
    # exactly one: the parser refuses 'scope: both' on a kernel, since a
    # flavour is a name within an architecture and one cannot be right for
    # two of them. The loop is over what the package says it is built for
    # rather than over the image's architecture, so the two cannot drift.
    def extend_kernel(self, package, sourcedir, architectures):
        if package.kernel == False:
            return

        fragments = package.kernel_config_files()
        for fragment in fragments:
            if os.path.isfile(fragment) == False:
                raise ValueError("package '%s': no such kernel configuration "
                                 "fragment: %s" % (package.source, fragment))

        for architecture in architectures:
            config = os.path.join(sourcedir, "debian", "config", architecture,
                                  "config")
            if os.path.isfile(config) == False:
                raise ValueError(
                    "package '%s' is built as a kernel, but its source has no "
                    "debian/config/%s/config to configure"
                    % (package.source, architecture))

            if len(fragments) > 0:
                with open(config, "a") as f:
                    for fragment in fragments:
                        f.write("\n# %s, added by seine\n"
                                % os.path.basename(fragment))
                        with open(fragment, "r") as contents:
                            f.write(contents.read())

            if package.kernel_flavour is not None:
                self._restrict_flavour(package, sourcedir, architecture)
            if package.kernel_upstream is not None:
                self._disable_signed(package, sourcedir, architecture)

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
            for architecture in architectures:
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
                "effect, %d kernels are still built for '%s': %s. The "
                "generator that rewrites debian/control is allowed to fail, "
                "so look above for what it said."
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
        packages = []
        with open(control, "r") as f:
            stanzas = f.read().split("\n\n")

        for stanza in stanzas:
            fields = {}
            for line in stanza.split("\n"):
                field = re.match(r"^([A-Za-z-]+):\s*(.*)$", line)
                if field:
                    fields[field.group(1)] = field.group(2)
            packages.append((fields.get("Package", ""),
                             fields.get("Architecture", "").split()))

        # linux-image-<abi>-<flavour>, the versioned image packages, one
        # per kernel -- as opposed to the metapackage linux-image-<flavour>
        # pointing at one of them, and the debug packages beside them.
        prefix = "linux-image-%s-" % self._abiname(packages)
        return sorted(set(
            name for name, architectures in packages
            if architecture in architectures
            and name.startswith(prefix) and name.endswith("-dbg") == False))

    # The ABI name the packaging gave this kernel, which is the only thing
    # separating a versioned image package from the metapackage pointing
    # at it. It is read back rather than predicted: it is built from the
    # upstream version, the abiname in debian/config and what the
    # changelog's distribution earns it, and the UNRELEASED entry a local
    # rebuild has to carry changes its shape. A pattern written for one
    # shape finds no kernels at all and calls that a failed restriction.
    #
    # Debian builds one headers package shared by every flavour of a
    # kernel and names it for the ABI alone, which is what is read here.
    def _abiname(self, packages):
        for name, _ in packages:
            abi = re.match(r"^linux-headers-(.+)-common$", name)
            if abi:
                return abi.group(1)
        raise ValueError(
            "debian/control has no 'linux-headers-<abi>-common' package to "
            "read the kernel's ABI name from")

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
    # Says in the source that this kernel is not a signed one, which a
    # grafted kernel cannot be. Debian's Secure Boot support is not in its
    # packaging: CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT comes from the
    # features/all/lockdown patches, which change C source and so are not
    # among the patches kept, and the build stops in a check that is right
    # to stop it. DEBIAN_KERNEL_DISABLE_SIGNED does not reach that check
    # -- it is read when debian/control is generated, here, and the check
    # runs later inside the chroot -- so it is said in debian/config,
    # where everything downstream of it agrees.
    #
    # Nothing is lost that was there to lose: the kernel Debian signs is
    # built from a source package of its own, from a key we do not have.
    # An image wanting the lockdown behaviour needs those patches kept
    # and rebased.
    def _disable_signed(self, package, sourcedir, architecture):
        defines = os.path.join(sourcedir, "debian", "config", architecture,
                               "defines.toml")
        if os.path.isfile(defines):
            self._toml_set(defines, "build", "enable_signed", "false")

    # Debian described its kernels in an ini-like debian/config/*/defines
    # and moved to a defines.toml. Both are still in the archive at once,
    # so which one the source carries decides, not the release built for.
    def _restrict_flavour(self, package, sourcedir, architecture):
        config = os.path.join(sourcedir, "debian", "config")
        defines = os.path.join(config, architecture, "defines.toml")
        if os.path.isfile(defines) == False:
            return self._restrict_flavour_ini(package, sourcedir, architecture)

        self._restrict_flavour_toml(package, defines, architecture,
                                    ["flavour", "featureset"])
        # The featuresets again, where they are declared for every
        # architecture at once. Disabling one for amd64 alone leaves the
        # packages that do not depend on an architecture -- the headers
        # every flavour of a kernel shares -- still being built for it,
        # and a featureset whose patches the graft dropped cannot be.
        self._restrict_flavour_toml(package, os.path.join(config, "defines.toml"),
                                    architecture, ["featureset"])

    # The toml describes flavours and featuresets as arrays of tables, each
    # taking an 'enable' that defaults to true, and a kernel is built only
    # when every level of the hierarchy says so. So the entries not wanted
    # are said false rather than removed: deleting table blocks means
    # getting the boundaries of a nested one right or building something
    # else silently.
    def _restrict_flavour_toml(self, package, path, architecture, kinds):
        with open(path, "rb") as f:
            defines = tomllib.load(f)

        wanted = {kind: name for kind, name in
                  [("flavour", package.kernel_flavour),
                   ("featureset", package.kernel_featureset)] if kind in kinds}
        for kind, name in wanted.items():
            names = [entry["name"] for entry in defines.get(kind, [])]
            if name not in names:
                raise ValueError(
                    "package '%s': architecture '%s' has no '%s' kernel %s, "
                    "expected one of %s"
                    % (package.source, architecture, name, kind,
                       ", ".join(sorted(names))))

        with open(path, "r") as f:
            lines = f.readlines()
        blocks = self._toml_blocks(lines)

        # Back to front, so the edits do not move the blocks still to come.
        for position in reversed(range(len(blocks) - 1)):
            kind, start = blocks[position]
            end = blocks[position + 1][1]
            if kind not in wanted:
                continue
            name = self._toml_value(lines, start, end, "name")
            if name is None or name == wanted[kind]:
                continue
            enabled = self._toml_line(lines, start, end, "enable")
            if enabled is not None:
                lines[enabled] = "enable = false\n"
            else:
                lines.insert(start + 1, "enable = false\n")

        with open(path, "w") as f:
            f.writelines(lines)

    # Every table header, with the block it opens. A dotted name --
    # [flavour.defs] under [[flavour]] -- is a table within the block
    # rather than one of its own; an undotted one always starts a block,
    # including the [[flavour]] that follows another [[flavour]]. The last
    # entry has no name and marks where the file ends.
    def _toml_blocks(self, lines):
        blocks = []
        for index, line in enumerate(lines):
            header = re.match(r"^\s*\[\[?([A-Za-z0-9_.-]+)\]?\]\s*$", line)
            if header is None or "." in header.group(1):
                continue
            blocks.append((header.group(1), index))
        blocks.append((None, len(lines)))
        return blocks

    # Sets a key at the top level of a named block, adding the block if the
    # file has none.
    def _toml_set(self, path, block, key, value):
        with open(path, "r") as f:
            lines = f.readlines()

        blocks = self._toml_blocks(lines)
        for position in range(len(blocks) - 1):
            kind, start = blocks[position]
            if kind != block:
                continue
            end = blocks[position + 1][1]
            existing = self._toml_line(lines, start, end, key)
            if existing is not None:
                lines[existing] = "%s = %s\n" % (key, value)
            else:
                lines.insert(start + 1, "%s = %s\n" % (key, value))
            break
        else:
            lines += ["\n[%s]\n" % block, "%s = %s\n" % (key, value)]

        with open(path, "w") as f:
            f.writelines(lines)

    # A key at the top level of a toml block, i.e. before its first nested
    # table. Only the string and boolean scalars this needs are understood.
    def _toml_line(self, lines, start, end, key):
        for index in range(start + 1, end):
            if re.match(r"^\s*\[", lines[index]):
                break
            if re.match(r"^\s*%s\s*=" % key, lines[index]):
                return index
        return None

    def _toml_value(self, lines, start, end, key):
        index = self._toml_line(lines, start, end, key)
        if index is None:
            return None
        value = re.match(r"^\s*%s\s*=\s*['\"](.*)['\"]" % key, lines[index])
        return value.group(1) if value else None

    def _restrict_flavour_ini(self, package, sourcedir, architecture):
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
        script = "ln -sf /dev/null /dev/console; exec %s" % shlex.join(args)

        # Run from the output directory so sbuild drops what it built
        # there, where it is this build's and nobody else's.
        self.builderImage.exec(
            ["sh", "-c", script],
            architecture=self.chroot_architecture(package, architecture),
            volumes=volumes, workdir=OUTPUT, environment=environment)

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
    def index(self):
        # The Release file goes last and is made fresh every time: it
        # holds the hashes of the indices above it, and the signatures
        # beside it are of the file as it was. Removing them first is what
        # stops apt-ftparchive from hashing yesterday's signature into
        # today's Release.
        for name in ["Release", "Release.gpg", "InRelease"]:
            path = os.path.join(self.repository(), name)
            if os.path.isfile(path):
                os.unlink(path)

        script = ("apt-ftparchive --db .packages.db packages . "
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
        for part in [package.source,
                     ",".join(package.profiles),
                     ",".join(package.options),
                     str(self.cross(package, architecture)),
                     str(package.source_date_epoch),
                     package.revision,
                     str(package.kernel_featureset),
                     str(package.kernel_flavour),
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
                     "\n".join(apt_sources(self.distro, sources=True)),
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
                     str(architecture == self.indep_architecture(package))]:
            digest.update(part.encode())
        # Patches and kernel configuration fragments count by content, not
        # by name: editing one without touching the specification has to be
        # enough to ask for a rebuild, all the more for a kernel, where the
        # alternative is silently keeping one built from the fragment as it
        # used to read.
        for path in package.patch_files() + package.kernel_config_files():
            with open(path, "rb") as f:
                digest.update(f.read())

        # A grafted kernel is built from what the rules kept of the
        # distribution's series, so those rules decide what comes out as
        # surely as a fragment does. By content, for the same reason:
        # editing them has to be enough to ask for a rebuild, or the
        # kernel goes on being the one yesterday's rules produced. Only
        # for a graft -- an ordinary rebuild never consults them.
        if package.kernel_upstream is not None:
            digest.update(kernel_rules().content)

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


    # A module is built against its kernel's headers, which depend on the
    # linux-kbuild of the same ABI. Debian builds that one for every
    # profile but 'pkg.linux.notools', and a kernel grafted here has an ABI
    # no archive can answer for -- so the modules fail to install their
    # build dependencies. Refused here rather than by sbuild, which
    # reaches it only after the kernel has been built.
    # 'pkg.linux.mintools' drops the same tools and keeps kbuild.
    def _kernels_keep_their_kbuild(self, packages):
        built = {p.name: p for p in packages if p.kernel}
        for package in packages:
            if package.module == False:
                continue
            for architecture in sorted(package.module_kernels):
                for reference in package.module_kernels[architecture]:
                    kernel = built.get(reference)
                    if kernel is None or NO_TOOLS not in kernel.profiles:
                        continue
                    raise package._error(
                        "it is built against '%s', which is built with '%s' "
                        "-- so no linux-kbuild is made for it, and no archive "
                        "has one for a kernel built here. Use '%s' on '%s' "
                        "instead, which drops the same tools and keeps "
                        "kbuild, or name no tools profile at all."
                        % (reference, NO_TOOLS, MIN_TOOLS, reference))

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
    def tasks(self, packages, hostBootstrap):
        pending = self._pending(packages)
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
                              needs=["bootstrap-host"])]
            return first + [Task("packages",
                                 functools.partial(self._reused, reused),
                                 needs=["bootstrap-host"]
                                       + [t.name for t in first])]

        tasks = [Task("packages-prepare",
                      functools.partial(self._prepare, hostBootstrap),
                      needs=["bootstrap-host"])]

        # A package built against another has to be built after it, which
        # is the same order 'before'/'after' already worked out -- as task
        # dependencies now, so everything else can overlap.
        # What a dependent waits for is the name of the step that puts a
        # package where it can be installed from, not the one that built
        # it.
        names = {package.name: "deploy:%s" % package.name
                 for package, _, _ in pending}

        # A package's builds, gathered so its steps are declared together:
        # one fetch, a build per architecture, and one step publishing
        # them.
        grouped = collections.OrderedDict()
        for package, architecture, stamp in pending:
            grouped.setdefault(package.name, (package, []))[1].append(
                (architecture, stamp))

        for name, (package, builds) in grouped.items():
            # Fetching waits on a server and building waits on the
            # machine, so they are separate: a large download no longer
            # holds a slot that could be compiling something else, and a
            # source that cannot be fetched says so early rather than
            # after everything before it has been built.
            #
            # One fetch for a package built for two architectures. The
            # same source package is what both builds are handed, so two
            # roles are two builds of one source rather than two sources
            # that ought to be the same.
            fetch = "fetch:%s" % name
            tasks.append(Task(fetch,
                              functools.partial(self._fetched, package),
                              needs=["packages-prepare"]))
            waits = [fetch] + [names[d.name]
                               for d in getattr(package, "depends", [])
                               if d.name in names]
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

    # Fetching a source and building it are two different jobs: one waits
    # on a server, the other on the machine. Kept together, a package with
    # a large source holds a build slot while it uses nothing but the
    # network.
    #
    # So what a fetch leaves behind outlives it: the directory belongs to
    # the package rather than to the step, and the build that follows
    # finds it there.
    #
    # Preparing the source is part of it -- grafting, patching, the local
    # changelog entry, the kernel's configuration, and dpkg-source over
    # the result. All of that describes a source package rather than a
    # build of one, and doing it here is what lets a package built for two
    # architectures be built from one .dsc: the two are then demonstrably
    # the same source rather than two trees prepared the same way.
    def _fetched(self, package):
        workdir = tempfile.mkdtemp(dir=ContainerEngine.scratch(),
                                   prefix="source-")
        try:
            print("fetching '%s'" % package.source)
            sourcedir = self.fetch(package, workdir)
            self.fetch_upstream(package, workdir)
            dsc, epoch = self._prepared(package, workdir, sourcedir)
        except:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        source = (workdir, dsc, epoch)
        with self._workdirs:
            self._sources[package.name] = source
            # How many builds are still to be handed this source. The
            # directory outlives the fetch but not the last build that
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
        if package.kernel_upstream is not None:
            sourcedir = self.graft(package, workdir, sourcedir, epoch)
        self.patch(package, sourcedir, epoch)
        self.local_release(package, sourcedir, epoch)
        self.extend_kernel(package, sourcedir, self.architectures(package))
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

        with self._repository, locked(self.repository()):
            for name in sources:
                destination = os.path.join(self.repository(), name)
                if os.path.lexists(destination):
                    os.remove(destination)
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
                    shutil.move(os.path.join(output, name), destination)
                shutil.rmtree(output, ignore_errors=True)
                self._forget(package, architecture, everything)
                self._record(stamp, sorted(set(produced) | set(sources)))
            # Once, at the end: one repository, and an index of it made
            # while a build of it is half moved in describes neither what
            # was there nor what is.
            self.index()

        for architecture in sorted(built):
            key = self.key(package, architecture)
            Index().made(PACKAGE, key)
            say(self.options, "package %s made" % key)

    # One package: patched, built, and recorded as built. The chroot is
    # shared with whatever else is building at the same time, so it is
    # taken one at a time.
    def _rebuild(self, package, architecture, stamp):
        with self._chroots:
            chroot = SbuildChroot(self.distro, self.options,
                                  self.chroot_architecture(package, architecture))
            chroot.create(self.builderImage)

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
    lines = " && ".join(
        "echo 'deb %s file:%s ./' %s %s"
        % (options, mountpoint, ">" if index == 0 else ">>", SOURCES_LIST)
        for index, mountpoint in enumerate(mountpoints))
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
    ordered = propagate(order(parsed))
    _check_module_kernels(ordered, spec)
    _check_module_references(ordered)
    return ordered

# A kernel named without a scheme is one this specification builds, and
# naming one it does not build is a typo rather than a constraint worth
# ignoring -- the same answer 'before' and 'after' already give.
#
# A description under 'defaults' is the exception, and it never reaches
# here: what it named that nothing builds was dropped as the defaults
# were folded in, which is how an architecture file says "modules for our
# kernel too, if there is one" without every image of that architecture
# having to build one.
def _check_module_references(packages):
    built = {package.name for package in packages}
    for package in packages:
        if package.module == False:
            continue
        for architecture in sorted(package.module_kernels):
            for kernel in package.module_kernels[architecture]:
                if "://" in kernel or kernel in built:
                    continue
                raise package._error(
                    "'extends: module: %s-kernels' names '%s', which no "
                    "package in this specification builds. Name a kernel "
                    "this specification builds, or the headers package of "
                    "one the distribution ships, as 'apt://linux-headers-%s'."
                    % (architecture, kernel, architecture))

# Every module has to say which kernels it is built against for each
# architecture it is built for. Answered when the specification is
# parsed: the architectures are known without fetching anything, so an
# arm64 build of a specification that names only amd64 kernels fails in a
# second rather than after cloning somebody's tree.
#
# Every package at fault is named in one message. A specification with
# three modules and one architecture missing from all of them is three
# lines of one error rather than three builds, each finding the next.
#
# Not a skip: an entry under 'packages' asks for a build, and a module
# built against no kernels produces nothing. The image would come out,
# boot, carry none of the modules asked for, and look exactly as it
# should.
def _check_module_kernels(packages, spec):
    target = (spec.get("distribution") or {}).get("architecture")
    missing = []
    for package in packages:
        if package.module == False:
            continue
        wanted = []
        if "target" in package.scope and target is not None:
            wanted.append(target)
        if "host" in package.scope:
            wanted.append(HOST_ARCH)
        for architecture in sorted(set(wanted)):
            if len(package.module_kernels.get(architecture, [])) > 0:
                continue
            named = sorted(package.module_kernels)
            missing.append(
                "package '%s' builds no kernel modules for %s: it names "
                "kernels for %s. Add '%s-kernels' to its 'extends: module', "
                "or take the package out of a specification building for %s."
                % (package.name, architecture,
                   ", ".join(named) if len(named) > 0 else "no architecture",
                   architecture, architecture))
    if len(missing) > 0:
        raise ValueError("\n".join(missing))

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
