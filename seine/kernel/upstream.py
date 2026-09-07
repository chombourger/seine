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


def _verify_upstream(builder, package, staging):
    upstream = package.kernel_upstream
    if upstream is None or upstream.scheme != "https":
        return
    builder._verify(package, staging, os.path.basename(upstream.uri),
                    package.kernel_upstream_sha256, "upstream-sha256")

# Hidden directory so _source_dir() can still find the unpacked
# distribution source while both trees are on disk.
UPSTREAM = ".upstream"

# Fetched alongside the distro source rather than at graft time: it's the
# largest download seine makes, no reason to make it wait for a build slot.
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

# Puts the distribution's packaging on a kernel tree it wasn't written
# for, and returns the grafted tree.
#
# debian/ moves across whole (package names, ABI naming, headers split,
# maintainer scripts all live there), the patch series is cut to what
# still applies, and the changelog gets the version of the tree being
# built. The tree itself is repackaged as the orig tarball -- tree and
# orig are identical outside debian/, which is what makes this a
# "3.0 (quilt)" source at all.
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

    # Leftover files from the distribution's own source describe a
    # version we're not building; dpkg-source would pick the wrong orig
    # tarball out of them.
    for name in sorted(os.listdir(workdir)):
        path = os.path.join(workdir, name)
        if os.path.isfile(path):
            os.unlink(path)

    # gzip, not Debian's xz: this tarball is written once and read once
    # by dpkg-source. .git is excluded so the tree and orig stay
    # identical. Packed from the parent so the tarball has the single
    # top-level directory an orig tarball is expected to have.
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

# The source package name, which the tree's own directory name isn't:
# Debian's kernel tree unpacks as linux-<version> but the source is
# 'linux'.
def _source_name(sourcedir):
    with open(os.path.join(sourcedir, "debian", "changelog"), "r") as f:
        heading = re.match(r"^(\S+) ", f.readline())
    if heading is None:
        raise ValueError("debian/changelog does not start with a source name")
    return heading.group(1)

# Read from the Makefile. EXTRAVERSION is left out on purpose: a BSP
# commonly puts its own name there, with nowhere for a Debian version to
# put a '-rc1' that wouldn't be read as the Debian revision.
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

# Cuts the distribution's patch series down to what's being kept. A glob
# matching nothing is an error, not a no-op: the series is restructured
# release to release, and a stale glob would otherwise silently build a
# kernel missing patches it meant to keep.
def _filter_series(package, sourcedir):
    path = os.path.join(sourcedir, "debian", "patches", "series")
    if os.path.isfile(path) == False:
        return

    with open(path, "r") as f:
        names = [line.strip() for line in f]

    patches = os.path.join(sourcedir, "debian", "patches")
    # Qualified: tests patch 'seine.kernel.kernel_rules' (this package's
    # own re-export), which a bare name here would never see.
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

    # Only the globs the spec itself wrote are checked here -- the rules
    # file's own 'drop-patches' isn't, since a source with no DFSG
    # exclusions is normal, not a sign the spec has gone stale.
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

# Checks the kept patches apply before dpkg-source does, since dpkg-source
# stops at the first one that doesn't -- answering one patch per build.
# quilt (not plain patch) is used because push order matters and quilt
# can restore the tree afterwards; fuzz 0 matches what dpkg-source allows.
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
    # quilt names a patch by its path from the source tree; reported as
    # the series writes it, since that's what 'drop-patches' matches against.
    failed = [n.strip().removeprefix("debian/patches/")
              for n in output.decode().split("\n") if len(n.strip()) > 0]
    if len(failed) == 0:
        return

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

# Whether a patch is packaging, decided by what it touches: one that
# touches only build files is needed just to build; one reaching into C
# source is changing the kernel itself (kept if not asked for). Only
# debian/ is checked -- bugfix/ and features/ are backports a newer tree
# is expected to already have.
def _packaging_patch(package, name, path):
    if name.startswith("debian/") == False:
        return False
    touched = _touches(path)
    matches = build_files(tuple(package.kernel_build_files))
    return len(touched) > 0 and all(matches.search(f) for f in touched)

# Files a patch changes, read off its '+++' lines. The leading path
# component ('a/', 'b/', or a tree name depending on how the patch was
# made) is stripped since patches apply with -p1.
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

# Records which tree was built and gives the package that tree's version
# instead of the packaging's -- otherwise the .debs would claim the
# distribution's kernel version while holding a different kernel.
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

# Whether the kept patch series already looks for module.lds where Debian
# puts it. Checked against the patches (the tree itself stays unpatched
# here; dpkg-source applies them later), which is why this runs after the
# series is finalized.
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

# Writes the same fix Debian carries, but for the tree in hand, only when
# nothing else already provides it and the tree actually needs it.
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
    # In front of the first rule that reads it, same place Debian puts it.
    anchor = "quiet_cmd_ld_ko_o"
    if anchor not in after:
        raise package._error(
            "'%s' asks for module.lds but has no '%s' to define it "
            "beside: the tree's kbuild is not shaped the way this "
            "knows how to patch." % (MODFINAL, anchor))
    after = after.replace(anchor, "%s\n\n%s" % (MODULE_LDS_FALLBACK, anchor), 1)

    # No dates in the diff, so the same tree produces the same source
    # package on a second run.
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
