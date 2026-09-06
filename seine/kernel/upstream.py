# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Fetching the tree named by 'extends: kernel: upstream:', and grafting
# the distribution's own packaging onto it.

import difflib
import fnmatch
import os
import re
import shlex
import shutil

from datetime import datetime
from datetime import timezone
from email.utils import format_datetime

from seine.utils import GIT_EMAIL
from seine.utils import GIT_NAME
from seine.utils import WORKDIR

from . import MODFINAL, MODULE_LDS, MODULE_LDS_FALLBACK, MODULE_LDS_PATCH
from . import build_files


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
    # Qualified: tests replace this by patching 'seine.kernel.kernel_rules'
    # (this package's own re-export), which a bare name here would never see.
    from seine import kernel
    dropped = kernel.kernel_rules().drop_patches + package.kernel_drop_patches
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
