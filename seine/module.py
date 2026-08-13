# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: module:' means: building an out-of-tree kernel module
# against kernels this specification builds, or ones the distribution
# ships, with packaging seine writes for it.
#
# Above seine/kernel.py rather than beside it: a module is built against a
# kernel, names one, and is versioned by the ABI that kernel ended up with.
# The names are imported rather than the module itself -- 'kernel' is what
# a local variable is called all through here.

import collections
import functools
import glob
import jinja2
import os
import re
import shutil

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

from seine.kernel import DEFAULT_FEATURESET
from seine.kernel import KERNEL_ARCHITECTURES
from seine.kernel import KERNEL_MACHINES
from seine.kernel import MIN_TOOLS
from seine.kernel import NO_TOOLS
from seine.kernel import kernel_architecture
from seine.utils  import GIT_EMAIL
from seine.utils  import GIT_NAME
from seine.utils  import HOST_ARCH


# What 'extends: module:' takes, beyond the '<architecture>-kernels'
# below: those are not a fixed list, so they are matched rather than
# listed.
SETTINGS = ["build", "build-depends", "make-vars", "modules",
            "runtime-depends", "target"]

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

# The headers packages a module is built against are named for the kernel
# rather than for the architecture: linux-headers-<abi>-<flavour>, where
# the ABI is what the packaging derived from the version it was built
# from. Stripping this prefix leaves '<abi>-<flavour>' -- which is the
# kernel's release, the name of its modules directory, and what a module
# has to be built against to be loadable by it.
MODULE_HEADERS_PREFIX = "linux-headers-"

# An ABI begins with the kernel's version, so it begins with a digit:
# that is what separates a name for one kernel from the metapackages
# pointing at whichever kernel is current. Nothing else about the two
# tells them apart -- a featureset and a flavour are both words.
MODULE_ABI = re.compile(r"^[0-9]")

# What the headers of a kernel are called once they carry tools another
# architecture can run. Not a name any archive uses: it is built here,
# for the machine doing the building, out of a kernel meant for another.
CROSS_SUFFIX = "-cross"

# Where the kernel's own headers packages are staged inside the source
# package that unpacks them, since a source package cannot reach outside
# itself.
CROSS_STAGED = "debian/headers"

# Where the fetch leaves them beside the source it unpacked. Hidden,
# because what a fetch produced is found by looking for the one directory
# it left behind, and anything seine put there itself has to stay out of
# that count.
CROSS_FETCHED = ".headers"

def cross_headers_name(release):
    return "%s%s%s" % (MODULE_HEADERS_PREFIX, release, CROSS_SUFFIX)

# Whether a package is one seine made up rather than one a specification
# asked for. They have no source URI: what they are built from is worked
# out from the kernel they are of.
def is_cross_package(package):
    return getattr(package, "cross_kernel", None) is not None

# What a module is built against, once the name it was written as has
# been made sense of.
#
# 'release' is the '<abi>-<flavour>' the kernel calls itself: it names the
# binary package the modules are shipped in, the directory they are
# installed into, and the kernel they refuse to load into if it is wrong.
# 'headers' is what has to be installed to build against it.
#
# 'flavour' is what a metapackage over these modules is named for, and is
# None when there is nothing safe to call it. A reference that named a
# flavour gives one; an ABI written out does not, since splitting it back
# into an ABI and a flavour cannot be done from the name. Nothing is lost
# by that: a specification that pinned an exact kernel can name the exact
# package its modules are in.
Kernel = collections.namedtuple("Kernel",
                                ["reference", "headers", "release", "flavour"])

# Whether a kernel reference names a package this specification builds,
# as opposed to one the distribution ships. Written without a scheme:
# 'linux' is a package here, 'apt://linux-headers-amd64' is Debian's.
def is_built_kernel(reference):
    return "://" not in reference

# Whether an 'apt://' reference names one kernel or whichever kernel is
# current. Only the first can be made sense of without asking apt.
def is_kernel_metapackage(reference):
    name = reference.partition("://")[2]
    if name.startswith(MODULE_HEADERS_PREFIX) == False:
        return True
    return MODULE_ABI.match(name[len(MODULE_HEADERS_PREFIX):]) is None


# A kernel built here is published under the distribution's own names, so
# a metapackage naming that flavour names the graft once the image is
# composed. Both resolving builds the modules twice -- once for a kernel
# the image does not carry -- and writes the flavour metapackage into
# debian/control twice, which dh refuses. An ABI written out is not
# superseded: it names one kernel and has no flavour to clash on.
def supersede_grafted(kernels):
    grafted = set(kernel.flavour for kernel in kernels
                  if is_built_kernel(kernel.reference)
                  and kernel.flavour is not None)
    return [kernel for kernel in kernels
            if is_built_kernel(kernel.reference)
            or kernel.flavour not in grafted]

# The packaging seine writes for an out-of-tree module, kept beside the
# code rather than in it, as the kernel rules above are: what a
# debian/rules has to say is Debian's to define, and following it should
# be an edit to a file that looks like what it produces.
#
# Read as bytes as well as rendered: those bytes are part of what decides
# whether a module needs building again, so editing the packaging is
# enough to ask for a rebuild -- without it a module would go on being
# the one yesterday's rules produced.
MODULE_PACKAGING = os.path.join(os.path.dirname(__file__), "data", "module")
CROSS_PACKAGING = os.path.join(os.path.dirname(__file__), "data", "cross")
MODULE_FILES = ["changelog", "control", "rules"]

@functools.lru_cache(maxsize=None)
def module_packaging():
    return _packaging(MODULE_PACKAGING)

# The packaging for the headers of a kernel meant for one architecture,
# carrying the kbuild tools of another. Kept beside the module packaging
# and read the same way, so that both are edited as what they produce and
# both decide by their content whether a rebuild is needed.
@functools.lru_cache(maxsize=None)
def cross_packaging():
    return _packaging(CROSS_PACKAGING)

def _packaging(directory):
    templates = {}
    content = b""
    for name in MODULE_FILES:
        with open(os.path.join(directory, name), "rb") as f:
            raw = f.read()
        content += raw
        templates[name] = raw.decode()
    return templates, content

# The same delimiters a specification is rendered with, so that one
# notation is seine's throughout: somebody editing this packaging already
# knows '[[ ]]' from the yaml. Spelt out here rather than shared with
# build.py, which cannot be imported from this module -- it reaches this
# one through seine.image.
#
# The one thing to keep out of a template because of it is bash's '[[ ]]'
# test in a rules recipe; '[' does the same job and dh runs recipes under
# /bin/sh anyway.
MODULE_TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)


# A module is built after the kernels it names, without anybody writing it
# down twice.
#
# Ordering is the obvious half: a module built against a kernel this
# specification also builds needs that kernel's headers, which do not
# exist until it has been built.
#
# The digest is the half that matters more. What a module has to be named
# for is the kernel's ABI, and an ABI is not knowable until the kernel's
# source has been prepared -- a grafted 6.18 calls itself
# '6.18+unreleased', which nothing can predict and which changes with the
# tree it was grafted onto. So the module's stamp does not try to carry
# it. It carries the kernel's digest instead, which 'after' is what folds
# in, and which changes whenever anything about that kernel does. A
# kernel that moved rebuilds the modules on it, whatever moved.
def depend_on_kernels(packages):
    for package in packages:
        if package.module == False:
            continue
        for architecture in sorted(package.module_kernels):
            for kernel in package.module_kernels[architecture]:
                if is_built_kernel(kernel) and kernel not in package.after:
                    package.after.append(kernel)

# A kernel named without a scheme is one this specification builds, and
# naming one it does not build is a typo rather than a constraint worth
# ignoring -- the same answer 'before' and 'after' already give.
#
# A description under 'defaults' is the exception, and it never reaches
# here: what it named that nothing builds was dropped as the defaults
# were folded in, which is how an architecture file says "modules for our
# kernel too, if there is one" without every image of that architecture
# having to build one.
def check_references(packages):
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
def check_kernels(packages, spec):
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

# Reads 'extends: module:' onto the package it was written on, the way
# kernel.parse() does for a kernel.
def parse(package, extends):
    settings = extends.get("module", {})
    package.module = "module" in extends
    # Where the module's own makefile is, for a tree that keeps it in
    # a subdirectory -- NVIDIA's is under kernel-open. The tree's root
    # when nothing says otherwise.
    package.module_build = settings.get("build", ".")
    if type(package.module_build) != type(""):
        raise package._error("'extends: module: build' shall be a string")
    # What to ask that makefile for. 'modules' is what kbuild calls
    # it and what nearly every out-of-tree tree follows, but not all:
    # some call it 'all', some 'default'.
    package.module_target = settings.get("target", "modules")
    if type(package.module_target) != type(""):
        raise package._error("'extends: module: target' shall be a string")
    # Which .ko files the build is expected to produce. Named rather
    # than found: a build that quietly produced none, or produced one
    # of two, would otherwise make a package that installs nothing and
    # looks exactly as it should.
    package.module_modules = package._parse_list(settings, "modules")
    for name in package.module_modules:
        if type(name) != type(""):
            raise package._error(
                "'extends: module: modules' shall be a list of module "
                "names")
    # What this tree needs to build that seine cannot know about: a
    # conftest that runs python, a driver that links against a
    # library. Taken verbatim -- what may be said in a Build-Depends
    # is Debian's to define, and a setting that parsed it would be a
    # second, smaller language to keep up to date.
    package.module_build_depends = package._parse_list(settings,
                                                       "build-depends")
    # And what the modules need once installed, which seine knows
    # less about still: firmware the driver asks the kernel to load,
    # a userspace half that has to match. The kernel they were built
    # for is added to these; it is the one relationship seine can
    # work out for itself.
    package.module_runtime_depends = package._parse_list(settings,
                                                         "runtime-depends")
    for setting, listed in [("build-depends", package.module_build_depends),
                            ("runtime-depends", package.module_runtime_depends)]:
        for depends in listed:
            if type(depends) != type("") or "\n" in depends:
                raise package._error(
                    "'extends: module: %s' shall be a list of package "
                    "relationships, one per entry, as debian/control "
                    "writes them" % setting)
    package.module_make_vars = _parse_make_vars(package, settings)
    package.module_kernels = _parse_module_kernels(package, settings)

# What to put on the make command line, for a tree that needs telling
# where things are -- NVIDIA's wants SYSSRC. Taken as written: what a
# module's makefile accepts is its own business.
def _parse_make_vars(package, settings):
    variables = settings.get("make-vars", {})
    if type(variables) != type({}):
        raise package._error(
            "'extends: module: make-vars' shall be a dictionary of "
            "variables to pass to make")
    for name, value in variables.items():
        if type(value) not in [type(""), type(0)]:
            raise package._error(
                "'extends: module: make-vars' has '%s', whose value is "
                "neither a string nor a number" % name)
        # A value reaches a shell in the generated rules, where it is
        # quoted so that '$KERNEL_SRC' and the rest expand. That is
        # the whole of what may expand: a value that could run
        # something would be running it as part of a build somebody
        # else's specification asked for.
        for forbidden in ["`", "$(", ";", "&", "|", "\n"]:
            if forbidden in str(value):
                raise package._error(
                    "'extends: module: make-vars' has '%s', whose value "
                    "contains '%s'. A value may name the variables the "
                    "rules set -- $KERNEL_SRC, $KERNEL_OBJ, "
                    "$KERNEL_RELEASE, $KERNEL_ARCH, $KERNEL_MACHINE -- "
                    "and nothing that runs a command."
                    % (name, forbidden.strip()))
    return {name: str(value) for name, value in variables.items()}

# The kernels this module is built against, per architecture.
#
# Each is named by its headers package -- 'apt://linux-headers-amd64',
# 'apt://linux-headers-6.12.101+deb13-amd64' -- or by the name of a
# kernel this specification builds, whose headers seine knows how to
# name for itself.
def _parse_module_kernels(package, settings):
    kernels = {}
    for setting, listed in settings.items():
        architecture = MODULE_KERNELS.match(setting)
        if architecture is None:
            continue
        if type(listed) != type([]):
            raise package._error("'extends: module: %s' shall be a list of "
                                 "kernels" % setting)
        for kernel in listed:
            if type(kernel) != type(""):
                raise package._error(
                    "'extends: module: %s' shall be a list of kernels, "
                    "named as strings" % setting)
            _check_module_kernel(package, setting, kernel)
        kernels[architecture.group(1)] = list(listed)
    return kernels

# A kernel is named by what a module is built against, which is a
# headers package. The image package beside it holds a compiled
# kernel and nothing to compile against, so naming it is a mistake
# worth spelling out rather than a name to quietly translate: which
# headers package an image package belongs to is a question with more
# than one answer where featuresets are involved.
def _check_module_kernel(package, setting, kernel):
    name = kernel
    if "://" in kernel:
        scheme, _, name = kernel.partition("://")
        if scheme != "apt":
            raise package._error(
                "'extends: module: %s' names '%s': a kernel is named by "
                "an 'apt://' headers package, or by a kernel this "
                "specification builds" % (setting, kernel))
    if name.startswith(MODULE_IMAGE_PREFIX):
        raise package._error(
            "'extends: module: %s' names '%s', which is a kernel image. "
            "A module is built against headers: name "
            "'linux-headers-%s' instead."
            % (setting, kernel, name[len(MODULE_IMAGE_PREFIX):]))

# A cross headers package is fetched rather than described: what it
# is built from is the kernel's own source, and what it carries is
# the headers that kernel's build produced. Both are asked of apt,
# which is the one thing that knows where either lives -- the
# distribution's for a kernel it ships, seine's own repository for a
# kernel built here, and the same two commands for both.
#
# The source package is not named by the specification: it is read
# off the headers package, which says which source it came from and
# at what version. Guessing 'linux' would be right for Debian's
# kernel and wrong for anybody else's.
def _fetch_cross_args(builder, package, architecture):
    headers = package.cross_kernel.headers
    return ["sh", "-c",
            "set -e; "
            "dpkg --add-architecture %(architecture)s; "
            "apt-get update -qq; "
            "mkdir -p .headers; "
            "cd .headers; "
            "apt-get download %(headers)s:%(architecture)s "
            "  $(apt-cache depends %(headers)s:%(architecture)s "
            "    | sed -n 's/.*[<]\\?\\(linux-headers-[^ :<>]*-common[^ :<>]*\\).*/\\1/p' "
            "    | head -1); "
            "cd ..; "
            "source=$(apt-cache show %(headers)s:%(architecture)s "
            "  | sed -n 's/^Source: \\([^ ]*\\).*/\\1/p' | head -1); "
            "version=$(apt-cache show %(headers)s:%(architecture)s "
            "  | sed -n 's/^Version: //p' | head -1); "
            "test -n \"$source\" || source=linux; "
            "apt-get source $source=$version"
            % {"headers": headers, "architecture": architecture}]

# The packaging for an out-of-tree module, written into the tree that
# carries none.
#
# Everything here is generated from what the specification said, so
# the source package is a function of the specification and the tree,
# and two builds of it are the same source package. It is written
# before the local changelog entry is added, since that reads the
# changelog this puts there.
def extend(builder, package, sourcedir, epoch):
    if package.module == False:
        return

    debian = os.path.join(sourcedir, "debian")
    if os.path.isdir(debian):
        raise ValueError(
            "package '%s' carries a debian/ directory of its own. seine "
            "writes the packaging for an out-of-tree module, so a tree "
            "that has packaging already is built as an ordinary package "
            "-- take 'extends: module' off it." % package.name)
    os.makedirs(os.path.join(debian, "source"), exist_ok=True)

    # Every architecture's kernels, not only the one being built for.
    # The source package is published once however many architectures
    # are built from it, so the control file has to describe all of
    # them or two builds would write one filename with two contents.
    # Which of them a build makes is decided by the Architecture
    # field, and the build-dependencies are qualified the same way.
    builds = []
    for architecture in sorted(package.module_kernels):
        kernels = resolved_kernels(builder, package, architecture,
                                   builder.packages)
        builds.append({
            "architecture": architecture,
            # Written here rather than in the template: '[[[ x ]]]'
            # reads as a list literal inside a substitution, not as
            # brackets around one.
            "qualifier": "[%s]" % architecture,
            "kernels": [{"release": kernel.release,
                         "headers": kernel.headers,
                         # What the same headers are called once they
                         # carry tools the builder can run. Which of
                         # the two a build installs is decided by the
                         # 'cross' build profile rather than here:
                         # one source package is built both ways, on
                         # machines of either architecture.
                         "cross_headers":
                             cross_headers_name(kernel.release),
                         "flavour": kernel.flavour,
                         "package": "%s-modules-%s"
                                    % (package.name, kernel.release)}
                        for kernel in kernels]})

    # A native source package: what is built is a tree, with no
    # upstream tarball beside it to be a delta against. Inventing one
    # would only say that the packaging and the source were published
    # apart, which they were not.
    _write(os.path.join(debian, "source", "format"), "3.0 (native)\n")

    templates, _ = module_packaging()
    context = {
        "name": package.name,
        "version": package.upstream_version,
        "source": package.source,
        "maintainer": GIT_NAME,
        "email": GIT_EMAIL,
        "date": format_datetime(datetime.fromtimestamp(epoch, timezone.utc)),
        "builds": builds,
        "build_dir": package.module_build,
        "target": package.module_target,
        "build_depends": package.module_build_depends,
        "runtime_depends": package.module_runtime_depends,
        "kernel_architectures": sorted(KERNEL_ARCHITECTURES.items()),
        "kernel_machines": sorted(KERNEL_MACHINES.items()),
        "modules": " ".join(sorted(package.module_modules)),
        # Double quotes, not shlex.quote: the point of a value is
        # often to name one of the variables the rules set per
        # kernel, and single quotes would pass '$KERNEL_SRC' to make
        # as those nine characters. Quoted all the same, so a path
        # with a space in it stays one argument.
        #
        # And every '$' doubled, because what reads this first is
        # make: '$KERNEL_ARCH' is the variable K followed by the word
        # ERNEL_ARCH to make, and the shell that was meant to expand
        # it never sees a dollar at all.
        "make_vars": " ".join(
            '%s="%s"' % (name, package.module_make_vars[name]
                         .replace('"', '\\"').replace("$", "$$"))
            for name in sorted(package.module_make_vars)),
    }
    for name in MODULE_FILES:
        _write(os.path.join(debian, name),
               MODULE_TEMPLATE.from_string(templates[name]).render(context),
               mode=0o755 if name == "rules" else None)

def _write(path, content, mode=None):
    with open(path, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)

# The packaging for a cross headers package, written into the kernel
# source tree it is built from.
#
# 'debs' is where seine staged the kernel's own headers packages: they
# are what the build unpacks, since Module.symvers cannot be made
# without building every module of that kernel, and it is what decides
# whether a module will load at all.
def extend_cross_headers(builder, package, sourcedir, epoch, debs):
    kernel = package.cross_kernel
    debian = os.path.join(sourcedir, "debian")
    # A kernel source package carries a debian/ of its own, and what
    # is wanted here is not it: this builds headers and tools, not a
    # kernel.
    if os.path.isdir(debian):
        shutil.rmtree(debian)
    os.makedirs(os.path.join(debian, "source"), exist_ok=True)

    _write(os.path.join(debian, "source", "format"), "3.0 (native)\n")
    templates, _ = cross_packaging()
    context = {
        "name": package.name,
        "version": cross_version(kernel),
        "release": kernel.release,
        "architecture": HOST_ARCH,
        "kernel_arch": kernel_architecture(builder.distro["architecture"]),
        "target_architecture": builder.distro["architecture"],
        "debs_dir": CROSS_STAGED,
        "maintainer": GIT_NAME,
        "email": GIT_EMAIL,
        "date": format_datetime(datetime.fromtimestamp(epoch, timezone.utc)),
    }
    for name in MODULE_FILES:
        _write(os.path.join(debian, name),
               MODULE_TEMPLATE.from_string(templates[name]).render(context),
               mode=0o755 if name == "rules" else None)

    # Inside the source tree, since a source package cannot reach
    # outside itself: what the build unpacks has to travel with it.
    staged = os.path.join(sourcedir, CROSS_STAGED)
    os.makedirs(staged, exist_ok=True)
    for name in sorted(os.listdir(debs)):
        if name.endswith(".deb"):
            shutil.copy(os.path.join(debs, name),
                        os.path.join(staged, name))
    if len(glob.glob(os.path.join(staged, "*.deb"))) == 0:
        raise ValueError(
            "package '%s': no kernel headers to build from. The headers "
            "of %s are what carry its .config and Module.symvers, and "
            "neither can be made again without building that kernel."
            % (package.name, kernel.release))

# What the package is versioned as. The kernel's own version would be
# a lie about what this is -- it is not that kernel -- but it has to
# move when that kernel does, or a rebuilt kernel would leave headers
# behind describing the one before it.
def cross_version(kernel):
    # No '-' in it: this is a native source package, and dpkg reads
    # what follows the last '-' as a Debian revision, which a native
    # package may not have. A release is full of them --
    # '6.12.101+deb13-arm64' -- so they become dots, which say the
    # same thing to a reader and nothing to dpkg.
    return "%s+cross1" % kernel.release.replace("+", ".").replace("-", ".")

# The headers packages a cross build needs, one per kernel.
#
# A module cross-compiled for another architecture cannot use that
# architecture's headers as they are: they reach for
# linux-kbuild-<abi>, whose fixdep and modpost are compiled for the
# kernel's architecture and cannot run on the machine doing the
# building. What is needed is the same headers with tools this
# machine can run, built from that kernel's own source -- and for a
# kernel seine grafts, no such package exists in any archive at all.
#
# So seine builds one, and builds it once: it is a property of the
# kernel rather than of the module, so many modules against one kernel
# need one of these. Deduplicated on the release rather than on how a
# specification spelt the kernel, since a headers metapackage and the
# ABI written out are the same kernel named two ways.
#
# Nothing is made for a build that is not crossing: a module built
# for the machine it is building on has tools it can run already.
def cross_headers(builder, packages):
    wanted = {}
    for package in packages:
        if package.module == False:
            continue
        for architecture in builder.architectures(package):
            if builder.cross(package, architecture) == False:
                continue
            for kernel in resolved_kernels(builder, package, architecture,
                                           packages):
                wanted.setdefault(kernel.release, kernel)
    return [_cross_package(wanted[release], index)
            for index, release in enumerate(sorted(wanted), 1)]

# Built for the machine doing the building, which is what 'host'
# says, and named for the kernel it carries so that two kernels do
# not write one package.
def _cross_package(kernel, index):
    # Here rather than at the top: seine/packages.py imports this module,
    # and a package seine made up is still a package it describes.
    from seine.packages import Package
    package = Package({"name": cross_headers_name(kernel.release),
                       "scope": ["host"]}, index)
    # Which kernel it is of, for the packaging that has yet to be
    # written and for the module that will build against it.
    package.cross_kernel = kernel
    return package

# What a module is built against for one architecture, with every
# reference made sense of.
#
# Three ways of naming a kernel, and two of them need nothing asked of
# anybody: an 'apt://' headers package carrying an ABI names one
# kernel and says which in its own name, and a kernel this
# specification builds is one seine gave an ABI to itself.
#
# The third -- a metapackage, 'apt://linux-headers-amd64' -- names
# whichever kernel is current, which is a question only apt can
# answer. Those are resolved when the build reaches them; see
# module_kernels().
def resolved_kernels(builder, package, architecture, packages=None):
    kernels = []
    for reference in package.module_kernels.get(architecture, []):
        if is_built_kernel(reference):
            kernels.append(
                _built_kernel(builder, package, reference, packages))
        else:
            named = reference.partition("://")[2]
            headers = builder.metapackages.get((architecture, reference))
            if headers is None and is_kernel_metapackage(reference):
                # Rather than carrying on with the metapackage's own
                # name, which strips to a plausible-looking release
                # -- 'linux-headers-amd64' becomes 'amd64' -- and
                # names the modules after a kernel that does not
                # exist. dpkg noticed only because that collided
                # with the flavour package beside it.
                raise package._error(
                    "'%s' was never resolved to a kernel for %s. A "
                    "metapackage names whichever kernel is current, "
                    "which has to be asked of apt before anything can "
                    "be named after it." % (reference, architecture))
            headers = headers or named
            # A metapackage names a flavour and nothing else, which is
            # what modules built for it can be named after too.
            flavour = None
            if is_kernel_metapackage(reference):
                flavour = named[len(MODULE_HEADERS_PREFIX):]
            kernels.append(Kernel(
                reference, headers,
                headers[len(MODULE_HEADERS_PREFIX):], flavour))
    return supersede_grafted(kernels)

# What the kernels named as metapackages actually are, asked of apt
# before anything is stamped.
#
# A metapackage names whichever kernel is current, so what it resolves
# to is part of what a module is built from -- and therefore part of
# what decides whether it needs building again. Debian moves the ABI
# in a security update; without this the modules would go on being the
# ones built for the ABI before it, and the image would fail to
# compose against a kernel that no longer exists.
#
# It costs a builder image, and only for a specification that names a
# metapackage: naming a moving target is what buys the query. One that
# writes its ABIs down, or builds its own kernels, still decides what
# to rebuild without creating anything.
def resolve_kernels(builder, packages, hostBootstrap):
    builder.packages = list(packages)
    wanted = {}
    for package in packages:
        if package.module == False:
            continue
        # Every architecture the module names, not only the ones
        # being built for. The control file describes all of them --
        # that is what keeps one .dsc honest across architectures --
        # so a name it has to write has to be resolved, whichever
        # machine is doing the writing.
        for architecture in sorted(package.module_kernels):
            for reference in package.module_kernels.get(architecture, []):
                if is_built_kernel(reference):
                    continue
                if is_kernel_metapackage(reference) == False:
                    continue
                if (architecture, reference) in builder.metapackages:
                    continue
                wanted.setdefault(architecture, set()).add(reference)
    if len(wanted) == 0:
        return

    # The host bootstrap first: this runs while the graph is being made,
    # so the step that would otherwise build it has not run yet, and the
    # builder image is built FROM it. A cache holding one is what hid
    # that; with the images cleared, the builder build stops at 'FROM
    # bootstrap/... did not resolve'. This is what the step does, and
    # it returns without building when the image is current.
    hostBootstrap.create()
    builder.builderImage.create(hostBootstrap)
    # The indexes of every architecture asked about, which for the one
    # being built is already there and for any other is not. A module
    # for another architecture is named in debian/control whichever
    # architecture is building, so its kernel has to be resolved here
    # too -- and its name only means something against its own index.
    foreign = [a for a in sorted(wanted) if a != HOST_ARCH]
    setup = ["dpkg --add-architecture %s" % a for a in foreign]
    setup.append("apt-get update -qq")
    for architecture in sorted(wanted):
        for reference in sorted(wanted[architecture]):
            name = reference.partition("://")[2]
            out = builder.builderImage.output(
                ["sh", "-c", " && ".join(
                    setup + ["apt-cache depends %s:%s" % (name, architecture)])])
            builder.metapackages[(architecture, reference)] = \
                _resolved_headers(reference, architecture, out)

# The one headers package a metapackage depends on, out of what
# 'apt-cache depends' printed. Recognised by carrying an ABI, which is
# what separates it from the metapackage that was asked about and from
# the '-common' half beside it.
def _resolved_headers(reference, architecture, output):
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    for found in re.findall(r"linux-headers-[0-9][^\s:]*", output):
        if found.endswith("-common"):
            continue
        return found
    raise ValueError(
        "'%s' names no kernel headers package for %s. It was resolved "
        "against the feeds this specification builds from, which may "
        "not carry a kernel for that architecture; naming the headers "
        "package and its ABI outright says which kernel is meant."
        % (reference, architecture))

# A kernel this specification builds, whose ABI seine settled itself.
#
# The ABI is read back rather than predicted -- it is built from the
# upstream version, the abiname in debian/config and what the
# changelog's distribution earns it, and a local rebuild is
# UNRELEASED, which turns a 6.1.0-53 into a 6.18+unreleased. It is
# known once that kernel's source has been prepared, which the module
# is built after, so by the time this is asked for the answer is
# there.
def _built_kernel(builder, package, reference, packages):
    kernel = None
    for other in packages or []:
        if other.name == reference:
            kernel = other
    if kernel is None:
        raise package._error(
            "'extends: module' names the kernel '%s', which is not "
            "among the packages being built" % reference)
    abi = builder.abinames.get(kernel.name)
    if abi is None:
        abi = _abiname_built_earlier(builder, kernel)
    if abi is None:
        raise package._error(
            "the kernel '%s' has not been prepared yet, so what its "
            "modules will have to be built against is not known. A "
            "module is built after the kernels it names." % reference)
    # A featureset is part of what a kernel is called: amd64's
    # realtime kernel and its ordinary one are both the 'amd64'
    # flavour, of the 'rt' and 'none' featuresets, and 'none' is the
    # one that goes unsaid.
    flavour = kernel.kernel_flavour
    if kernel.kernel_featureset not in [None, DEFAULT_FEATURESET]:
        flavour = "%s-%s" % (kernel.kernel_featureset, flavour)
    release = "%s-%s" % (abi, flavour)
    return Kernel(reference, MODULE_HEADERS_PREFIX + release, release,
                  flavour)

# The ABI of a kernel this build is not rebuilding, read off the stamp
# of the build that did. Only a rebuild records one as it goes, so
# without this a kernel that is already current -- a second build, or a
# machine that imported a cache -- leaves every module naming it
# unbuildable. The headers package every flavour shares is named for
# the ABI alone, which is what makes it readable here.
def _abiname_built_earlier(builder, kernel):
    for architecture in builder.architectures(kernel):
        stamp = builder.stamp(kernel, architecture)
        if os.path.isfile(stamp) == False:
            continue
        with open(stamp, "r") as f:
            for name in f:
                abi = re.match(r"^%s(.+)-common_"
                               % MODULE_HEADERS_PREFIX, name.strip())
                if abi:
                    builder.abinames[kernel.name] = abi.group(1)
                    return abi.group(1)
    return None

# A module is built against its kernel's headers, which depend on the
# linux-kbuild of the same ABI. Debian builds that one for every
# profile but 'pkg.linux.notools', and a kernel grafted here has an ABI
# no archive can answer for -- so the modules fail to install their
# build dependencies. Refused here rather than by sbuild, which
# reaches it only after the kernel has been built.
# 'pkg.linux.mintools' drops the same tools and keeps kbuild.
def check_kbuild(packages):
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
