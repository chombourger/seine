# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# 'extends: module:' builds an out-of-tree kernel module against a kernel
# this specification builds, or one the distribution ships, with packaging
# seine writes for it. Lives above seine/kernel since a module names and
# depends on a kernel; 'kernel' below is a plain local variable, not the
# imported module.

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
from seine.sbuild import REPOSITORY
from seine.utils  import GIT_EMAIL
from seine.utils  import GIT_NAME
from seine.utils  import HOST_ARCH


SETTINGS = ["build", "build-depends", "make-vars", "modules",
            "runtime-depends", "target"]

# '<architecture>-kernels', e.g. 'amd64-kernels', 'arm64-kernels'. Matched
# rather than listed since architectures are not a fixed set.
MODULE_KERNELS = re.compile(r"^([a-z][a-z0-9]*(-[a-z0-9]+)*)-kernels$")

# A module is built against headers, not the kernel image package.
MODULE_IMAGE_PREFIX = "linux-image-"

# Stripping this prefix off a headers package name leaves
# '<abi>-<flavour>', the kernel's release and modules directory name.
MODULE_HEADERS_PREFIX = "linux-headers-"

# An ABI starts with a digit; that's what tells a real kernel name apart
# from a metapackage that just points at whichever kernel is current.
MODULE_ABI = re.compile(r"^[0-9]")

# Name for headers rebuilt here with tools another architecture can run.
CROSS_SUFFIX = "-cross"

# Where a kernel's headers .debs are staged inside the source package
# that unpacks them (a source package can't reach outside itself).
CROSS_STAGED = "debian/headers"

# Directory a fetch leaves the kernel source under.
CROSS_FETCHED = ".headers"

def cross_headers_name(release):
    return "%s%s%s" % (MODULE_HEADERS_PREFIX, release, CROSS_SUFFIX)

# True for a package seine made up itself (no source URI of its own).
def is_cross_package(package):
    return getattr(package, "cross_kernel", None) is not None

# A resolved kernel reference: 'release' is '<abi>-<flavour>', 'headers'
# is the package to build against, 'flavour' is None when the reference
# was an exact ABI (which can't be split back into abi + flavour).
Kernel = collections.namedtuple("Kernel",
                                ["reference", "headers", "release", "flavour"])

# True if the reference names a package this specification builds
# ('linux'), false for a distro package ('apt://linux-headers-amd64').
def is_built_kernel(reference):
    return "://" not in reference

# True if an 'apt://' reference names whichever kernel is current,
# rather than one exact kernel (which apt doesn't need to resolve).
def is_kernel_metapackage(reference):
    name = reference.partition("://")[2]
    if name.startswith(MODULE_HEADERS_PREFIX) == False:
        return True
    return MODULE_ABI.match(name[len(MODULE_HEADERS_PREFIX):]) is None


# Drops a grafted kernel's flavour metapackage when the exact ABI is also
# named for it: building both would build the modules twice and write
# the flavour package into debian/control twice, which dh refuses.
def supersede_grafted(kernels):
    grafted = set(kernel.flavour for kernel in kernels
                  if is_built_kernel(kernel.reference)
                  and kernel.flavour is not None)
    return [kernel for kernel in kernels
            if is_built_kernel(kernel.reference)
            or kernel.flavour not in grafted]

# Packaging templates for an out-of-tree module, kept as files rather
# than inline so editing them looks like editing what they produce.
MODULE_PACKAGING = os.path.join(os.path.dirname(__file__), "data", "module")
CROSS_PACKAGING = os.path.join(os.path.dirname(__file__), "data", "cross")
MODULE_FILES = ["changelog", "control", "rules"]

@functools.lru_cache(maxsize=None)
def module_packaging():
    return _packaging(MODULE_PACKAGING)

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

# Same '[[ ]]' delimiters as spec rendering, so one notation is used
# throughout. Avoid bash's '[[ ]]' test in rules recipes because of it;
# '[' works the same and dh runs recipes under /bin/sh anyway.
MODULE_TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)


# Adds each named kernel this specification builds to 'after', so a
# module is built once its kernel is, and stamped with the kernel's
# digest (not its ABI, which a grafted kernel can't predict in advance).
def depend_on_kernels(packages):
    for package in packages:
        if package.module == False:
            continue
        for architecture in sorted(package.module_kernels):
            for kernel in package.module_kernels[architecture]:
                if is_built_kernel(kernel) and kernel not in package.after:
                    package.after.append(kernel)

# A kernel named without a scheme must be one this specification builds;
# naming anything else is a typo, not a constraint to ignore. Names
# dropped from 'defaults' when nothing built them never reach here.
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

# Every module needs kernels named for each architecture it builds for.
# Checked at parse time (no fetch needed) so a mismatched architecture
# fails fast, with every offending package named in one error.
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
    # Subdirectory holding the module's own makefile, e.g. NVIDIA's
    # kernel-open. Defaults to the tree's root.
    package.module_build = settings.get("build", ".")
    if type(package.module_build) != type(""):
        raise package._error("'extends: module: build' shall be a string")
    # Make target to build. 'modules' is kbuild's own default but not
    # every out-of-tree tree follows it (some use 'all' or 'default').
    package.module_target = settings.get("target", "modules")
    if type(package.module_target) != type(""):
        raise package._error("'extends: module: target' shall be a string")
    # .ko files the build must produce, named rather than discovered so
    # a build producing none (or only some) doesn't silently pass.
    package.module_modules = package._parse_list(settings, "modules")
    for name in package.module_modules:
        if type(name) != type(""):
            raise package._error(
                "'extends: module: modules' shall be a list of module "
                "names")
    # Extra Build-Depends the tree needs, taken as-is (Debian's syntax).
    package.module_build_depends = package._parse_list(settings,
                                                       "build-depends")
    # Extra runtime dependencies; the kernel built against is added
    # automatically since seine already knows that relationship.
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

# Extra make variables, e.g. NVIDIA's SYSSRC. Taken as written.
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
        # Values are shell-quoted in the generated rules so
        # $KERNEL_SRC etc. still expand; block anything that could
        # run a command instead.
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

# Kernels this module is built against, per architecture. Each is named
# by an 'apt://' headers package or by a kernel this spec builds.
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

# Rejects naming a kernel image package instead of its headers.
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

# Fetches the cross headers' source package from apt: the distribution's
# feed for a kernel it ships, seine's own repository for one built here.
# The source package name/version is read off the headers package
# itself, not guessed (guessing 'linux' would be wrong for non-Debian
# kernels).
def _fetch_cross_args(builder, package, architecture):
    headers = package.cross_kernel.headers
    return ["sh", "-c",
            "set -e; "
            "echo 'deb [trusted=yes] file:%(repository)s ./' "
            "  > /etc/apt/sources.list.d/seine-packages.list; "
            "echo 'deb-src [trusted=yes] file:%(repository)s ./' "
            "  >> /etc/apt/sources.list.d/seine-packages.list; "
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
            % {"headers": headers, "architecture": architecture,
               "repository": REPOSITORY}]

# Writes packaging for an out-of-tree module into its source tree,
# replacing whatever came with it (usually dkms, which builds on the
# install machine -- the opposite of what 'extends: module' wants).
# Run before the local changelog entry is added, since this writes the
# changelog that entry reads.
def extend(builder, package, sourcedir, epoch):
    if package.module == False:
        return

    debian = os.path.join(sourcedir, "debian")
    if os.path.isdir(debian):
        shutil.rmtree(debian)
    os.makedirs(os.path.join(debian, "source"), exist_ok=True)

    # Every architecture's kernels are described, since one source
    # package is published for all of them and control has to name
    # each with build-dependencies qualified by architecture.
    builds = []
    described = {}
    for architecture in sorted(package.module_kernels):
        kernels = resolved_kernels(builder, package, architecture,
                                   builder.packages)
        _describe_once(package, described, architecture, kernels)
        builds.append({
            "architecture": architecture,
            "qualifier": "[%s]" % architecture,
            "kernels": [{"release": kernel.release,
                         "headers": kernel.headers,
                         "cross_headers":
                             cross_headers_name(kernel.release),
                         "flavour": kernel.flavour,
                         "package": "%s-modules-%s"
                                    % (package.name, kernel.release)}
                        for kernel in kernels]})

    # Native: a tree with no upstream tarball to diff against.
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
        # Double-quoted (not shlex.quote) so $KERNEL_SRC etc. still
        # expand; every '$' doubled since make reads this before the
        # shell does.
        "make_vars": " ".join(
            '%s="%s"' % (name, package.module_make_vars[name]
                         .replace('"', '\\"').replace("$", "$$"))
            for name in sorted(package.module_make_vars)),
    }
    for name in MODULE_FILES:
        _write(os.path.join(debian, name),
               MODULE_TEMPLATE.from_string(templates[name]).render(context),
               mode=0o755 if name == "rules" else None)

# A kernel built here is for one architecture only; if two architecture
# lists resolve to the same release, that's a mistake to report.
def _describe_once(package, described, architecture, kernels):
    for kernel in kernels:
        seen = described.get(kernel.release)
        if seen is not None:
            raise package._error(
                "'extends: module: %s-kernels' and '%s-kernels' both name "
                "'%s', which is one kernel: %s. A kernel built by this "
                "specification is built for one architecture. Name that "
                "architecture's own kernel, or take the other list off."
                % (architecture, seen[0], seen[1], kernel.release))
        described[kernel.release] = (architecture, kernel.reference)

def _write(path, content, mode=None):
    with open(path, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)

# Writes packaging for a cross headers package into the kernel source
# tree it's built from. 'debs' holds the kernel's own staged headers
# .debs, needed since Module.symvers can't be regenerated otherwise.
def extend_cross_headers(builder, package, sourcedir, epoch, debs):
    kernel = package.cross_kernel
    debian = os.path.join(sourcedir, "debian")
    # Replace the kernel source's own debian/: this builds headers and
    # tools, not the kernel.
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

    # Staged inside the source tree, since a source package can't
    # reach outside itself.
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

# Versioned by the release it carries, not the kernel's own version
# (which would misleadingly claim to be that kernel).
def cross_version(kernel):
    # Dots instead of '-': this is a native package, and dpkg would
    # otherwise read the text after the last '-' as a Debian revision.
    return "%s+cross1" % kernel.release.replace("+", ".").replace("-", ".")

# Headers packages a cross build needs, one per kernel. A module
# cross-compiled for another architecture can't use that architecture's
# own linux-kbuild (its fixdep/modpost binaries can't run here), so
# seine builds matching headers with tools this machine can run.
# Deduplicated per kernel release since many modules can share one.
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

def _cross_package(kernel, index):
    # Imported here, not at the top: seine/packages.py imports this
    # module.
    from seine.packages import Package
    package = Package({"name": cross_headers_name(kernel.release),
                       "scope": ["host"]}, index)
    package.cross_kernel = kernel
    return package

# Resolves each kernel reference for one architecture. An 'apt://'
# headers package with an ABI, or a kernel this spec builds, resolve
# without asking anything; a metapackage must already be resolved by
# resolve_kernels() before this runs.
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
                raise package._error(
                    "'%s' was never resolved to a kernel for %s. A "
                    "metapackage names whichever kernel is current, "
                    "which has to be asked of apt before anything can "
                    "be named after it." % (reference, architecture))
            headers = headers or named
            flavour = None
            if is_kernel_metapackage(reference):
                flavour = named[len(MODULE_HEADERS_PREFIX):]
            kernels.append(Kernel(
                reference, headers,
                headers[len(MODULE_HEADERS_PREFIX):], flavour))
    return supersede_grafted(kernels)

# Resolves each metapackage to its current headers package via apt,
# before anything is stamped -- Debian moves the ABI in security
# updates, so this must run every time or modules would keep being
# built for a kernel that no longer exists.
def resolve_kernels(builder, packages, hostBootstrap):
    builder.packages = list(packages)
    wanted = {}
    for package in packages:
        if package.module == False:
            continue
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

    # Build the host bootstrap and builder image first: this runs
    # while the graph is still being built, before the normal step
    # that would do it.
    hostBootstrap.create()
    builder.builderImage.create(hostBootstrap)
    # Every wanted architecture's apt index is needed, not only the
    # one being built for, since control names them all.
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

# The one headers package a metapackage depends on, picked out of
# 'apt-cache depends' output by its ABI (skipping the '-common' one).
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

# Resolves a kernel this specification builds, reading back the ABI
# seine settled on (from the upstream version, debian/config's abiname,
# and the changelog distribution). Only known once that kernel's source
# has been prepared, which a module is always built after.
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
    # 'none' featureset goes unsaid in the flavour name.
    flavour = kernel.kernel_flavour
    if kernel.kernel_featureset not in [None, DEFAULT_FEATURESET]:
        flavour = "%s-%s" % (kernel.kernel_featureset, flavour)
    release = "%s-%s" % (abi, flavour)
    return Kernel(reference, MODULE_HEADERS_PREFIX + release, release,
                  flavour)

# ABI of a kernel this build isn't rebuilding, read off an earlier
# build's stamp (only a rebuild records the ABI itself).
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

# 'pkg.linux.notools' builds a kernel with no linux-kbuild package, so
# modules against a kernel built here (no archive has one either)
# can't install their build-dependencies. Checked here, before sbuild
# wastes time building the kernel first.
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
