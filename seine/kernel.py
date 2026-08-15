# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: kernel:' means: rebuilding the distribution's kernel with
# a configuration of one's own, and grafting its packaging onto a tree it
# was not written for.
#
# The functions here take the Builder as their first argument where they
# need one at all, rather than living on it: what a kernel is is one
# subject, and seine/packages.py is about source packages in general.

import collections
import difflib
import fnmatch
import functools
import os
import re
import shlex
import shutil
import tomllib
import yaml

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

from seine.utils import GIT_EMAIL
from seine.utils import GIT_NAME
from seine.utils import WORKDIR

# What 'extends: kernel:' takes. A kernel is configured rather than
# patched: Debian builds its kernels from a stack of kconfig files under
# debian/, so a fragment appended to the right one is both easier to write
# and less likely to conflict with the next point release than a patch
# would be.
SETTINGS = ["abi-suffix", "build-files", "config", "drop-patches",
            "featureset", "flavour", "keep-patches", "upstream",
            "upstream-sha256"]

# What the graft makes of a tree, beyond the packaging it copies across.
# The rules seine writes are hashed by content; what seine *does* to a tree
# is code, and code is in no digest -- so bump this when that changes, or a
# kernel grafted before it stays as it was.
GRAFT_VERSION = 1

# Debian generates module.lds during the kernel build, installs it under
# arch/<arch>/, and carries a kbuild patch to look for it there. A tree
# that has moved that rule leaves the patch inapplicable, so the graft
# drops it -- and then nothing links a module at all: the '%.ko' rule wants
# a file no package installs, and make says it has no rule to make it.
#
# The patch is written again for the tree in hand rather than shipped as
# one to rebase. It touches scripts/ alone, which is what seine already
# counts as packaging.
MODFINAL = "scripts/Makefile.modfinal"
MODULE_LDS = re.compile(r"\$\(objtree\)/scripts/module\.lds")
MODULE_LDS_PATCH = "debian/module-lds-under-arch-directory.patch"
MODULE_LDS_FALLBACK = (
    "ARCH_MODULE_LDS := $(word 1,$(wildcard $(objtree)/scripts/module.lds "
    "$(objtree)/arch/$(SRCARCH)/module.lds))")

# What a Debian architecture is called by the kernel, and what uname
# would have called it. Two answers to one question, and a tree wants
# whichever its own build system asks for -- the kernel's for ARCH, and
# uname's for anything that would otherwise have run uname.
#
# Kept here rather than in the packaging that uses them, so that adding
# an architecture is one edit rather than three: the make tables in both
# rules files are rendered from these.
KERNEL_ARCHITECTURES = {
    "amd64":   "x86_64",
    "arm64":   "arm64",
    "armel":   "arm",
    "armhf":   "arm",
    "i386":    "i386",
    "ppc64el": "powerpc",
    "riscv64": "riscv",
    "s390x":   "s390",
}

KERNEL_MACHINES = {
    "amd64":   "x86_64",
    "arm64":   "aarch64",
    "armel":   "armv7l",
    "armhf":   "armv7l",
    "i386":    "i686",
    "ppc64el": "ppc64le",
    "riscv64": "riscv64",
    "s390x":   "s390x",
}

# The kernel's name for an architecture, or nothing if seine has never
# been told. Answered rather than guessed: an empty ARCH handed to a tree
# that falls back to uname is the builder's architecture, and a module
# built for the wrong one is a module that builds.
def kernel_architecture(architecture):
    kernel = KERNEL_ARCHITECTURES.get(architecture)
    if kernel is None:
        raise ValueError(
            "seine has no kernel architecture for '%s'. Add it to "
            "KERNEL_ARCHITECTURES and KERNEL_MACHINES in seine/kernel.py."
            % architecture)
    return kernel

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

# Reads 'extends: kernel:' onto the package it was written on. The package
# is handed over whole rather than a dictionary handed back: what a
# setting is called in the yaml and what it is called on the package are
# one subject, and the error messages come from the package that is being
# parsed.
def parse(package, extends):
    settings = extends.get("kernel", {})
    package.kernel = "kernel" in extends
    package.kernel_config = package._parse_list(settings, "config")
    package.kernel_flavour = settings.get("flavour")
    package.kernel_featureset = settings.get("featureset", DEFAULT_FEATURESET)
    package.kernel_upstream = None
    if "upstream" in settings:
        if package.scheme != "apt":
            raise package._error(
                "'extends: kernel: upstream' grafts the distribution's "
                "debian/ onto another tree, so the package it is set on "
                "has to be the distribution's own kernel source")
        package.kernel_upstream = Upstream(settings["upstream"], package._error)
    package.kernel_upstream_sha256 = package._parse_digest(settings,
                                                           "upstream-sha256")
    # What a graft's ABI carries instead of Debian's own '+unreleased'.
    # None leaves it alone.
    package.kernel_abi_suffix = settings.get("abi-suffix")
    if package.kernel_abi_suffix is not None:
        if type(package.kernel_abi_suffix) != type(""):
            raise package._error("'extends: kernel: abi-suffix' shall be a string")
        if "'" in package.kernel_abi_suffix:
            raise package._error(
                "'extends: kernel: abi-suffix' cannot hold a single quote: "
                "it is written into a TOML string wrapped in one")
        if package.kernel_upstream is None:
            raise package._error(
                "'extends: kernel: abi-suffix' only means something for a "
                "graft: it is what a grafted kernel's ABI carries in "
                "place of '+unreleased', and there is no such ABI to "
                "rename without 'upstream'")
    # None, not a list of globs: with nothing said, which patches are
    # the packaging is decided by what they touch rather than by their
    # names. An explicit empty list is a different answer -- "keep none
    # of them" -- so presence decides, not emptiness.
    package.kernel_keep_patches = None
    if "keep-patches" in settings:
        package.kernel_keep_patches = package._parse_list(settings,
                                                          "keep-patches")
    # Subtracted from what 'keep-patches' selected, since that only
    # adds: taking one patch out of 'debian/*' would otherwise mean
    # writing out the thirty-odd being kept.
    package.kernel_drop_patches = package._parse_list(settings, "drop-patches")
    # Added to the patterns seine ships, for a packaging that builds
    # through files Debian's does not touch. Checked here rather than
    # where the series is read: a bad expression should be reported by
    # the specification that wrote it, not by the patch it first fails
    # to match.
    package.kernel_build_files = package._parse_list(settings, "build-files")
    for pattern in package.kernel_build_files:
        try:
            re.compile(pattern)
        except re.error as e:
            raise package._error(
                "'extends: kernel: build-files' has '%s', which is not a "
                "regular expression: %s" % (pattern, e))
    for setting, value in [("flavour", package.kernel_flavour),
                           ("featureset", package.kernel_featureset)]:
        if value is not None and type(value) != type(""):
            raise package._error(
                "'extends: kernel: %s' shall be a string" % setting)

# The tarball a kernel is grafted onto, which is fetched on its own and
# has a hash of its own to be checked against.
def _verify_upstream(builder, package, staging):
    upstream = package.kernel_upstream
    if upstream is None or upstream.scheme != "https":
        return
    builder._verify(package, staging, os.path.basename(upstream.uri),
                    package.kernel_upstream_sha256, "upstream-sha256")

# Where the tree named by 'upstream' is unpacked. Hidden, so the
# unpacked distribution source stays the only thing _source_dir()
# can find while both are on disk.
UPSTREAM = ".upstream"

# The tree a kernel is grafted onto, fetched with the source it will be
# grafted into rather than when the graft happens: it is the largest
# download seine makes, and there is no reason for it to wait for a
# build slot.
def fetch_upstream(builder, package, workdir):
    upstream = package.kernel_upstream
    if upstream is None:
        return
    staging = os.path.join(workdir, UPSTREAM)
    os.makedirs(staging, exist_ok=True)
    print("fetching '%s'" % upstream)
    builder.builderImage.exec(
        _upstream_args(upstream), volumes=[(staging, WORKDIR)],
        workdir=WORKDIR)
    _verify_upstream(builder, package, staging)

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
def graft(builder, package, workdir, sourcedir, epoch):
    upstream = package.kernel_upstream
    staging = os.path.join(workdir, UPSTREAM)
    if os.path.isdir(staging) == False:
        fetch_upstream(builder, package, workdir)

    print("grafting '%s' onto '%s'" % (package.source, upstream))
    tree = builder._source_dir(str(upstream), staging)

    source = _source_name(sourcedir)
    version = _kernel_version(tree)
    grafted = os.path.join(workdir, "%s-%s" % (source, version))

    shutil.move(os.path.join(sourcedir, "debian"),
                os.path.join(tree, "debian"))
    shutil.rmtree(sourcedir)
    shutil.move(tree, grafted)
    shutil.rmtree(staging, ignore_errors=True)

    _filter_series(package, grafted)
    _check_series(builder, package, grafted)
    _graft_release(package, grafted, source, version, epoch)

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
    builder.builderImage.exec(
        ["sh", "-c", "tar --exclude=%s/debian --exclude=%s/.git "
                     "--sort=name --mtime=@%d --owner=0 --group=0 "
                     "--numeric-owner -czf %s %s"
                     % (tree, tree, epoch, tarball, tree)],
        volumes=[(workdir, WORKDIR)], workdir=WORKDIR)
    return grafted

def _upstream_args(upstream):
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
def _source_name(sourcedir):
    with open(os.path.join(sourcedir, "debian", "changelog"), "r") as f:
        heading = re.match(r"^(\S+) ", f.readline())
    if heading is None:
        raise ValueError("debian/changelog does not start with a source name")
    return heading.group(1)

# The version of a kernel tree, read from its Makefile. EXTRAVERSION is
# deliberately left out: a BSP commonly puts its own name there, and a
# Debian upstream version has nowhere to put a '-rc1' that would not be
# read back as the Debian revision.
def _kernel_version(tree):
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
def _filter_series(package, sourcedir):
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
        if _matches(name, dropped):
            continue
        if package.kernel_keep_patches is None:
            if _packaging_patch(package, name,
                                os.path.join(patches, name)):
                kept.append(name)
        elif _matches(name, package.kernel_keep_patches):
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

def _check_series(builder, package, sourcedir):
    workdir = os.path.dirname(sourcedir)
    output = builder.builderImage.output(
        ["sh", "-c", SERIES_CHECK], volumes=[(workdir, WORKDIR)],
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
        for touched in sorted(_touches(os.path.join(patches, name))):
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
def _packaging_patch(package, name, path):
    if name.startswith("debian/") == False:
        return False
    touched = _touches(path)
    matches = build_files(tuple(package.kernel_build_files))
    return len(touched) > 0 and all(matches.search(f) for f in touched)

# The files a patch changes, as it names them on its '+++' lines. The
# leading component goes: these apply with -p1, and what is in front of
# the path is 'a/', 'b/' or the name of a tree, depending on how the
# patch was made.
def _touches(path):
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

def _matches(name, globs):
    return any(fnmatch.fnmatch(name, glob) for glob in globs)

# Says in the changelog which tree was built, and gives the package the
# version of that tree rather than of the packaging it borrowed. Without
# it the .debs would claim to be the distribution's kernel at the
# distribution's version while holding another one entirely.
# local_release() runs after this and adds the local revision on top.
def _graft_release(package, sourcedir, source, version, epoch):
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

# Whether anything in the series already looks for module.lds where
# Debian puts it. Asked of the patches rather than of the tree, since
# the tree is unpatched here and stays that way: dpkg-source applies
# them. Debian's own patch answers when it was kept, and so does one a
# specification rebased and listed under 'patches' -- which is why this
# runs after both are in the series rather than during the graft.
def _modfinal_is_patched(sourcedir):
    patches = os.path.join(sourcedir, "debian", "patches")
    series = os.path.join(patches, "series")
    if os.path.isfile(series) == False:
        return False
    with open(series, "r") as f:
        names = [line.strip() for line in f
                 if len(line.strip()) > 0 and line.strip().startswith("#") == False]
    for name in names:
        path = os.path.join(patches, name)
        if os.path.isfile(path) and MODFINAL in _touches(path):
            return True
    return False

# The patch Debian carries, written for the tree that was grafted.
#
# Only when nothing else answers for it, and only for a tree that asks
# for module.lds somewhere no package installs it. What comes out is
# the same idea as Debian's: a name that is whichever of the two
# places has the file, used everywhere the rule named one.
def module_lds_patch(package, sourcedir):
    path = os.path.join(sourcedir, MODFINAL)
    if os.path.isfile(path) == False:
        return
    if _modfinal_is_patched(sourcedir):
        return

    with open(path, "r") as f:
        before = f.read()
    if MODULE_LDS.search(before) is None:
        return

    after = MODULE_LDS.sub("$(ARCH_MODULE_LDS)", before)
    # In front of the first rule that reads it, which is where Debian
    # puts it too.
    anchor = "quiet_cmd_ld_ko_o"
    if anchor not in after:
        raise package._error(
            "'%s' asks for module.lds but has no '%s' to define it "
            "beside: the tree's kbuild is not shaped the way this "
            "knows how to patch." % (MODFINAL, anchor))
    after = after.replace(anchor, "%s\n\n%s" % (MODULE_LDS_FALLBACK, anchor), 1)

    # Written with no dates in it, so the same tree makes the same
    # source package twice running.
    diff = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="a/" + MODFINAL, tofile="b/" + MODFINAL, n=3)
    patches = os.path.join(sourcedir, "debian", "patches")
    os.makedirs(os.path.join(patches, os.path.dirname(MODULE_LDS_PATCH)),
                exist_ok=True)
    with open(os.path.join(patches, MODULE_LDS_PATCH), "w") as f:
        f.write("Subject: look for module.lds under the arch directory too\n"
                "\n"
                "Debian's packaging installs the module.lds its build\n"
                "generated under arch/<arch>/, and this tree looks for it\n"
                "under scripts/ alone -- so an out-of-tree module has no\n"
                "rule to link. Written by seine for this tree.\n"
                "---\n")
        f.writelines(diff)
    series = os.path.join(patches, "series")
    existing = ""
    if os.path.isfile(series):
        with open(series, "r") as f:
            existing = f.read()
    if len(existing) > 0 and existing.endswith("\n") == False:
        existing += "\n"
    with open(series, "w") as f:
        f.write("%s%s\n" % (existing, MODULE_LDS_PATCH))
    print("wrote '%s' for %s" % (MODULE_LDS_PATCH, MODFINAL))

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
def extend(builder, package, sourcedir, architectures):
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
            _restrict_flavour(package, sourcedir, architecture)
        if package.kernel_upstream is not None:
            _disable_signed(package, sourcedir, architecture)

    # One setting in the top-level defines.toml, unlike the edits above:
    # no per-architecture loop needed.
    if package.kernel_abi_suffix is not None:
        _set_abi_suffix(package, sourcedir)

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
    builder.builderImage.exec(
        ["debian/rules", "debian/control"],
        volumes=[(os.path.dirname(sourcedir), WORKDIR)],
        workdir="%s/%s" % (WORKDIR, os.path.basename(sourcedir)),
        environment={"PYTHONDONTWRITEBYTECODE": "1",
                     "DEBIAN_KERNEL_DISABLE_SIGNED": "1"},
        check=False)

    # What this kernel ended up calling itself, kept for the modules
    # built against it. Read here rather than worked out later: it is
    # in the control file that was just regenerated, and the module
    # that needs it is built after this.
    _record_abiname(builder, package, sourcedir)

    if package.kernel_flavour is not None:
        for architecture in architectures:
            _check_flavour(package, sourcedir, architecture)

# The ABI the packaging gave this kernel, off the control file it just
# generated. Absent rather than fatal when it cannot be read: a kernel
# nothing builds modules against does not need one, and the module
# that does need it says so itself, naming the kernel.
def _record_abiname(builder, package, sourcedir):
    control = os.path.join(sourcedir, "debian", "control")
    if os.path.isfile(control) == False:
        return
    with open(control, "r") as f:
        stanzas = f.read().split("\n\n")
    packages = []
    for stanza in stanzas:
        name = re.search(r"^Package:\s*(\S+)$", stanza, re.MULTILINE)
        if name:
            packages.append((name.group(1), []))
    try:
        builder.abinames[package.name] = _abiname(packages)
    except ValueError:
        pass

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
def _check_flavour(package, sourcedir, architecture):
    control = os.path.join(sourcedir, "debian", "control")
    if os.path.isfile(control) == False:
        raise ValueError(
            "package '%s': restricting the kernel left no debian/control "
            "behind" % package.source)

    kernels = _kernel_packages(control, architecture)
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
def _kernel_packages(control, architecture):
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
    prefix = "linux-image-%s-" % _abiname(packages)
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
def _abiname(packages):
    for name, _ in packages:
        abi = re.match(r"^linux-headers-(.+)-common$", name)
        if abi:
            return abi.group(1)
    raise ValueError(
        "debian/control has no 'linux-headers-<abi>-common' package to "
        "read the kernel's ABI name from")

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
def _disable_signed(package, sourcedir, architecture):
    defines = os.path.join(sourcedir, "debian", "config", architecture,
                           "defines.toml")
    if os.path.isfile(defines):
        _toml_set(defines, "build", "enable_signed", "false")

# gencontrol.py picks the '[[debianrelease]]' whose 'name_regex' matches
# the changelog distribution (which stays UNRELEASED) and takes its
# 'abi_suffix' as the kernel's ABI. Rewriting that value, not the match,
# is enough: debian/control, the maintainer scripts and the ABINAME the
# build compiles modules under all read it back from the same place.
def _set_abi_suffix(package, sourcedir):
    path = os.path.join(sourcedir, "debian", "config", "defines.toml")
    with open(path, "r") as f:
        lines = f.readlines()

    blocks = _toml_blocks(lines)
    for position in range(len(blocks) - 1):
        kind, start = blocks[position]
        if kind != "debianrelease":
            continue
        end = blocks[position + 1][1]
        if _toml_value(lines, start, end, "name_regex") == "UNRELEASED":
            break
    else:
        raise ValueError(
            "package '%s': debian/config/defines.toml has no "
            "[[debianrelease]] for 'UNRELEASED' to give 'abi-suffix' to"
            % package.source)

    value = "'%s'" % package.kernel_abi_suffix
    existing = _toml_line(lines, start, end, "abi_suffix")
    if existing is not None:
        lines[existing] = "abi_suffix = %s\n" % value
    else:
        lines.insert(start + 1, "abi_suffix = %s\n" % value)

    with open(path, "w") as f:
        f.writelines(lines)

# Cuts the build down to the one kernel asked for. Debian builds every
# featureset and flavour an architecture has, and for the kernel each
# of those is a full build -- on amd64, a cloud flavour and a realtime
# kernel besides the one an appliance wants.
#
# Debian described its kernels in an ini-like debian/config/*/defines
# and moved to a defines.toml. Both are still in the archive at once,
# so which one the source carries decides, not the release built for.
def _restrict_flavour(package, sourcedir, architecture):
    config = os.path.join(sourcedir, "debian", "config")
    defines = os.path.join(config, architecture, "defines.toml")
    if os.path.isfile(defines) == False:
        return _restrict_flavour_ini(package, sourcedir, architecture)

    _restrict_flavour_toml(package, defines, architecture,
                           ["flavour", "featureset"])
    # The featuresets again, where they are declared for every
    # architecture at once. Disabling one for amd64 alone leaves the
    # packages that do not depend on an architecture -- the headers
    # every flavour of a kernel shares -- still being built for it,
    # and a featureset whose patches the graft dropped cannot be.
    _restrict_flavour_toml(package, os.path.join(config, "defines.toml"),
                           architecture, ["featureset"])

# The toml describes flavours and featuresets as arrays of tables, each
# taking an 'enable' that defaults to true, and a kernel is built only
# when every level of the hierarchy says so. So the entries not wanted
# are said false rather than removed: deleting table blocks means
# getting the boundaries of a nested one right or building something
# else silently.
def _restrict_flavour_toml(package, path, architecture, kinds):
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
    blocks = _toml_blocks(lines)

    # Back to front, so the edits do not move the blocks still to come.
    for position in reversed(range(len(blocks) - 1)):
        kind, start = blocks[position]
        end = blocks[position + 1][1]
        if kind not in wanted:
            continue
        name = _toml_value(lines, start, end, "name")
        if name is None or name == wanted[kind]:
            continue
        enabled = _toml_line(lines, start, end, "enable")
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
def _toml_blocks(lines):
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
def _toml_set(path, block, key, value):
    with open(path, "r") as f:
        lines = f.readlines()

    blocks = _toml_blocks(lines)
    for position in range(len(blocks) - 1):
        kind, start = blocks[position]
        if kind != block:
            continue
        end = blocks[position + 1][1]
        existing = _toml_line(lines, start, end, key)
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
def _toml_line(lines, start, end, key):
    for index in range(start + 1, end):
        if re.match(r"^\s*\[", lines[index]):
            break
        if re.match(r"^\s*%s\s*=" % key, lines[index]):
            return index
    return None

def _toml_value(lines, start, end, key):
    index = _toml_line(lines, start, end, key)
    if index is None:
        return None
    value = re.match(r"^\s*%s\s*=\s*['\"](.*)['\"]" % key, lines[index])
    return value.group(1) if value else None

def _restrict_flavour_ini(package, sourcedir, architecture):
    root = os.path.join(sourcedir, "debian", "config", architecture)
    featuresets = _defines_list(os.path.join(root, "defines"),
                                "featuresets")
    if package.kernel_featureset not in featuresets:
        raise ValueError(
            "package '%s': architecture '%s' has no '%s' kernel "
            "featureset, expected one of %s"
            % (package.source, architecture, package.kernel_featureset,
               ", ".join(sorted(featuresets))))

    defines = os.path.join(root, package.kernel_featureset, "defines")
    flavours = _defines_list(defines, "flavours")
    if package.kernel_flavour not in flavours:
        raise ValueError(
            "package '%s': the '%s' featureset of architecture '%s' has "
            "no '%s' kernel flavour, expected one of %s"
            % (package.source, package.kernel_featureset, architecture,
               package.kernel_flavour, ", ".join(sorted(flavours))))

    _defines_replace(os.path.join(root, "defines"), "featuresets",
                     [package.kernel_featureset])
    _defines_replace(defines, "flavours", [package.kernel_flavour])
    # Both may name a flavour that has just been removed.
    _defines_set(defines, "default-flavour", package.kernel_flavour)
    _defines_set(defines, "quick-flavour", package.kernel_flavour)

# debian/config/*/defines are ini-like, with list values written one
# per line and indented under their key.
def _defines_list(path, key):
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

def _defines_replace(path, key, values):
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

def _defines_set(path, key, value):
    with open(path, "r") as f:
        lines = f.readlines()
    out = ["%s: %s\n" % (key, value) if line.startswith("%s:" % key) else line
           for line in lines]
    with open(path, "w") as f:
        f.writelines(out)
