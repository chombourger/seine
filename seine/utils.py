# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import contextlib
import fcntl
import functools
import hashlib
import os
import platform
import re
import subprocess

# Debian architecture of the machine seine itself runs on. Anything the
# specification asks for that differs from this is a cross build, whether
# that means emulating the target or compiling for it.
HOST_MACHINE_TO_ARCH = {
    "x86_64":  "amd64",
    "aarch64": "arm64",
    "armv7l":  "armhf",
    "i686":    "i386",
}
HOST_ARCH = HOST_MACHINE_TO_ARCH.get(platform.machine(), platform.machine())

# Where the source of a package is fetched and, later, built. Everything
# happens inside the builder container: the tools involved (apt-get source,
# dget, git) are installed there rather than on the machine seine runs on,
# and the working directory is a host-side directory bind-mounted into it so
# the fetched source outlives the container that fetched it.
#
# Here rather than in seine/packages.py because what a kernel or a module
# does to a source happens in the same directory, and neither of those
# modules may import the one that drives them.
WORKDIR = "/src"

# Identity the patch commits are made under. Fixed, like their date: a
# commit made by whoever happens to be running the build is a commit whose
# hash cannot be reproduced by anyone else.
GIT_NAME  = "seine"
GIT_EMAIL = "seine@localhost"

# The 'distribution' section, defaulted and validated once so that two
# callers -- Image.parse() and VendorCmd, which shares nothing else with
# it -- do not each keep their own copy of what a bare 'distribution:'
# means.
def distribution(spec):
    distro = spec["distribution"] if "distribution" in spec else {}
    if "source" not in distro:
        distro["source"] = "debian"
    # 'buster' until 2026-08-30: it went EOL, dropped off
    # deb.debian.org entirely, and a bootstrap naming it as a fallback
    # nobody actually configured now fails outright rather than quietly
    # building something stale. 'bookworm' is oldstable as of this
    # writing -- still served -- and the fallback's whole job is a
    # release that exists, not a particular one.
    if "release" not in distro:
        distro["release"] = "bookworm"
    if "architecture" not in distro:
        distro["architecture"] = "amd64"
    if "uri" not in distro:
        distro["uri"] = "http://ftp.debian.org/debian"
    # Build profiles/options for Build-Depends evaluation (DEB_BUILD_PROFILES
    # / DEB_BUILD_OPTIONS). Optional, defaults to no extra profiles.
    for key in ("build-options", "build-profiles"):
        if key in distro:
            val = distro[key]
            if isinstance(val, str):
                val = [val]
            if not isinstance(val, list) or any(not isinstance(v, str) for v in val):
                raise ValueError("'%s' shall be a string or list of strings" % key)
            distro[key] = val
    spec["distribution"] = distro

    # Validated here rather than when a bootstrap first reads them, so a
    # mistyped feed is reported against the specification instead of
    # halfway through building an image from it.
    feeds(distro)
    return distro

# The apt feeds a system is built from. A specification lists them
# explicitly rather than having suites guessed for it: which of -updates
# and -security a release has, and where they are served from, differs
# between distributions and between a release and its development
# version, and a suite that does not exist fails every build that follows.
#
# With none listed, the release itself is the one feed, which is what
# seine did before feeds could be listed at all.
#
# Each feed is assumed to carry sources as well as binaries, since that
# is what an archive normally serves and what rebuilding a package needs.
# 'sources: false' says otherwise, for the vendor feeds that ship
# binaries alone.
#
# 'valid-until: false' is for archives meant to stay expired -- a
# snapshot.debian.org timestamp, or a frozen mirror -- which apt would
# otherwise refuse.
#
# 'release:' groups a pocket with the release it belongs to -- unset, a
# feed is its own, single-member group. A base feed needs nothing (its
# own 'suite' already names its release); one of its pockets
# ('trixie-security', 'bookworm-backports') names the base explicitly
# (examples/common/debian.yaml's own template does, for every feed but
# the base one). Never guessed from the suite's name -- 'oldstable-
# security' or a snapshot-tagged suite would defeat any naming
# convention, and a wrong guess would silently leak one release's
# packages into another's build-dependency closure (seine vendor's own
# feeds_for_suite() is what this exists for -- see its own comment).
def feeds(distro):
    entries = distro.get("feeds")
    if entries is None:
        entries = [{"suite": distro["release"]}]
    if type(entries) != type([]):
        raise ValueError("'feeds' shall be a list of apt feeds!")

    parsed = []
    for index, entry in enumerate(entries):
        if type(entry) != type({}):
            raise ValueError("feed #%d is not a dictionary!" % (index + 1))
        if "suite" not in entry:
            raise ValueError("feed #%d has no 'suite' specified!" % (index + 1))
        for setting in entry:
            if setting not in ["components", "release", "sources", "suite",
                               "uri", "valid-until"]:
                raise ValueError(
                    "feed #%d ('%s') has no '%s' setting, expected one of "
                    "components, release, sources, suite, uri, valid-until"
                    % (index + 1, entry["suite"], setting))
        parsed.append({
            "uri":         entry.get("uri", distro["uri"]),
            "suite":       entry["suite"],
            "release":     entry.get("release", entry["suite"]),
            # A distribution-wide default, so a file needing a component --
            # firmware for a board, say -- says so once without naming the
            # suites. A feed saying otherwise still decides for itself.
            "components":  entry.get("components",
                                     distro.get("components", "main")),
            "sources":     entry.get("sources", True),
            "valid_until": entry.get("valid-until", True),
        })
    return parsed

# The feed a root file-system bootstraps from: the one for the release
# itself, not whichever feed a fragment happened to list first -- merge
# order puts 'requires'-added feeds (backports, say) ahead of it.
def base_feed(distro):
    release = distro["release"]
    for feed in feeds(distro):
        if feed["suite"] == release:
            return feed
    raise ValueError("no feed for suite '%s'!" % release)

# Those feeds as apt would write them down. 'sources' adds a deb-src line
# for every feed that has said it carries any. 'entries' overrides which
# feeds to use (base_feed()'s alone, say); unset is every feed, as before.
#
# An expired feed says so in the entry rather than in an apt.conf.d
# fragment: these lines reach every container a build talks to an archive
# from, and the option stays scoped to the feed that asked for it.
#
# 'offline' is an explicit opt-in, never inferred from
# 'distro.get("apt-pull-mode")' here: this one function backs every
# container that ever calls apt -- the host/target bootstraps, the
# sbuild-capable builder 'packages:' rebuilds in, and that same builder
# reused by 'seine vendor' itself to *populate* the local repository this
# would otherwise point a resolve/fetch container back at. Only
# seine/ansible_runner.py's own feed configuration for the running target
# container -- what 'apt-pull-mode: offline' actually means, installing
# what the specification asks for without reaching the network -- passes
# it.
def apt_sources(distro, sources=False, entries=None, offline=False):
    lines = []
    for feed in entries if entries is not None else feeds(distro):
        if offline:
            lines += _offline_feed(feed, sources)
            continue
        feed = dict(feed, options="" if feed["valid_until"]
                                    else "[check-valid-until=no] ")
        lines.append("deb %(options)s%(uri)s %(suite)s %(components)s" % feed)
        if sources and feed["sources"]:
            lines.append("deb-src %(options)s%(uri)s %(suite)s %(components)s" % feed)
    return lines

# Where an image built by a Dockerfile RUN instruction writes the feeds
# it bakes in -- kept apart from FEEDS_LIST (ansible_runner.py), which
# names the same thing for a container reconfigured after it is already
# running, not while it is being built.
DOCKERFILE_SOURCES_LIST = "/etc/apt/sources.list.d/seine.list"

# apt's package lists: stale as soon as the next 'apt-get update' runs,
# never needed once an image is built. Safe everywhere, even on an image
# that becomes part of what a build ships.
APT_LISTS_CLEANUP = "rm -rf /var/lib/apt/lists/*"

# Doc/info/man pages a human would read, on top of APT_LISTS_CLEANUP.
# Only for a tooling image seine itself uses and never ships -- a
# TargetBootstrap-derived image (TransportBootstrap, say) keeps them:
# whatever it carries there is part of what a build hands the user, not
# seine's own tooling to trim.
APT_CLEANUP = "rm -rf /usr/share/doc /usr/share/info /usr/share/man && " \
             + APT_LISTS_CLEANUP

# The '&&'-joined shell fragment a Dockerfile RUN instruction chains
# ahead of its own 'apt-get update': writes 'entries' into a fresh
# sources.list.d file so the image reads the specification's own feed
# instead of whatever the base image happened to ship with. 'true' when
# there is nothing to add, so a caller can chain it unconditionally
# rather than special-case an empty list.
def apt_sources_dockerfile(distro, entries, sources=False, offline=False):
    lines = apt_sources(distro, sources=sources, entries=entries, offline=offline)
    if len(lines) == 0:
        return "true"
    return " && ".join("echo '%s' >> %s" % (line, DOCKERFILE_SOURCES_LIST)
                       for line in lines)

# base_feed()'s uri/components, folded into a short tag: TargetBootstrap
# and TransportBootstrap both bake base_feed() into a Dockerfile without
# spelling it out anywhere else in their name, so two specifications
# differing only there would otherwise collide on one image tag.
def feed_digest(distro):
    return hashlib.sha256(
        repr(sorted(base_feed(distro).items())).encode()).hexdigest()[:8]

# Where a suite's vendor repository is reached from inside a container,
# once 'apt-pull-mode: offline' has turned a feed into one -- bind-mounted
# there by whoever calls apt_sources(), the same way 'packages:'s own
# repository is mounted at sbuild.py's REPOSITORY. One mountpoint per
# suite, since two feeds going offline in the same container each need
# their own tree: a single shared path could only ever hold one of them.
VENDOR_MOUNTPOINT = "/vendor-repo"

def vendor_mountpoint(suite):
    return "%s/%s" % (VENDOR_MOUNTPOINT, suite)

# A feed rewritten to read from its own suite's local vendor instead of
# the network. Verified with 'signed-by' when the vendor carries a key --
# 'seine vendor --vendor-sign-key' signed it precisely so a rebuild years
# from now, on another machine, can still tell its packages were not
# tampered with on the way -- and trusted outright, the way 'packages:'s
# own unsigned repository is, only when it was never signed at all.
def _offline_feed(feed, sources):
    from seine import vendor
    where = vendor_mountpoint(feed["suite"])
    keyring = vendor.keyring(feed["suite"])
    options = ("[signed-by=%s/%s]" % (where, keyring) if keyring is not None
              else "[trusted=yes]")
    # main/extra always present, even if one is empty
    lines = ["deb %s file:%s %s main" % (options, where, feed["suite"]),
             "deb %s file:%s %s extra" % (options, where, feed["suite"])]
    if sources:
        lines.append("deb-src %s file:%s %s main" % (options, where, feed["suite"]))
        lines.append("deb-src %s file:%s %s extra" % (options, where, feed["suite"]))
    return lines

# The suites a set of feeds would read from offline -- what a caller
# building the podman command around apt_sources() has to bind-mount,
# without re-deriving it from the same feeds a second time. Empty unless
# 'apt-pull-mode: offline' is actually set, so a caller can bind-mount
# unconditionally on what this returns.
def offline_suites(distro, entries=None):
    if distro.get("apt-pull-mode") != "offline":
        return []
    return sorted({feed["suite"] for feed
                  in (entries if entries is not None else feeds(distro))})

# A shell fragment writing 'entries' into 'target' inside a container --
# shared by ansible_runner.py (the running target container) and sbuild.py
# (a throwaway builder container), the two places that turn a feed list
# into what a container's own apt actually reads. 'offline' is passed
# through to apt_sources() unchanged, same opt-in-only rule as there.
#
# Going offline replaces what is already in /etc/apt rather than adding to
# it: a baked-in entry left standing would still reach for the network
# right beside the one just written for it.
def offline_apt_script(distro, entries, target, offline=False):
    lines = apt_sources(distro, sources=True, entries=entries, offline=offline)
    script = ""
    if offline:
        script += ("rm -f /etc/apt/sources.list "
                  "/etc/apt/sources.list.d/*.sources "
                  "/etc/apt/sources.list.d/*.list; ")
    script += "".join("echo '%s' >> %s; " % (line, target) for line in lines)
    return script

# One build at a time for the things two builds share.
#
# seine's caches are keyed by what they are made from, so two builds
# wanting the same one want the same bytes. What they must not do is
# write it at the same time: two mmdebstraps producing one tarball leave
# a file that is neither, and the build that trips over it does so much
# later and for no visible reason. The lock file sits beside the thing it
# guards and is held only while it is written.
#
# 'shared' is for what many may do at once but none may do while one does
# something else: several builds may add images to a storage together, and
# a prune that removes what none of them has tagged yet may not run while
# any of them is.
#
# 'blocking' says what to do when it is not free. Unset, wait; set, raise
# BlockingIOError rather than queue behind something long, for work that
# another holder will end up doing anyway.
@contextlib.contextmanager
def locked(path, shared=False, blocking=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    how = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    with open("%s.lock" % path, "w") as lock:
        fcntl.flock(lock, how if blocking else how | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

# What names a set of specification files, for whatever is filed per
# specification: its last plan, its logs. The names rather than the contents,
# which change with every edit -- the very thing those are compared across.
def digest(files, length=None):
    named = "\0".join(_portable_name(f) for f in files)
    return hashlib.sha256(named.encode()).hexdigest()[:length]

# A file's name, made independent of where its workspace was checked out --
# a raw abspath() would give the same specification a different digest() on
# every machine or clone. A file inside a git repository is named relative
# to that repo's remote (falling back to its toplevel directory's basename
# if it has no remote), so the same checkout content hashes the same way
# everywhere; anything else keeps its absolute path.
def _portable_name(f):
    abspath = os.path.abspath(f)
    toplevel = _git_toplevel(os.path.dirname(abspath))
    if toplevel is None:
        return abspath
    rel = os.path.relpath(abspath, toplevel)
    prefix = _git_remote(toplevel) or os.path.basename(toplevel)
    return "%s/%s" % (prefix, rel)

@functools.lru_cache(maxsize=None)
def _git_toplevel(directory):
    try:
        return subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

# 'origin' if there is one, else whichever remote sorts first -- picking a
# remote deterministically matters more than which one, since it only has
# to agree with itself across machines that cloned the same repository.
@functools.lru_cache(maxsize=None)
def _git_remote(toplevel):
    try:
        names = subprocess.run(
            ["git", "-C", toplevel, "remote"],
            capture_output=True, text=True, check=True).stdout.split()
        if not names:
            return None
        name = "origin" if "origin" in names else sorted(names)[0]
        url = subprocess.run(
            ["git", "-C", toplevel, "remote", "get-url", name],
            capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return _normalize_remote(url)

# Collapses the ssh and https forms of the same remote to one string --
# 'git@github.com:org/repo.git' and 'https://github.com/org/repo.git' both
# become 'github.com/org/repo' -- so cloning method doesn't change digest().
def _normalize_remote(url):
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-len(".git")]
    match = (re.match(r"^[\w.+-]+@([^:/]+):(.+)$", url) or
             re.match(r"^\w+://(?:[^@/]+@)?([^/]+)/(.+)$", url))
    if not match:
        return url
    host, path = match.groups()
    return "%s/%s" % (host.lower(), path.strip("/"))

# The lock file a specification pairs with, kas style: 'foo.yaml' always
# pairs with 'foo.lock.yaml', full stop -- no field asks for it, and a
# file already named '*.lock.yaml' does not get a lock of its own.
# Generic, not vendor-specific: 'BuildCmd.load_all()' auto-loads whatever
# this names if it exists, and any top-level key a lock file carries
# ('vendor:' today) is merged by its own existing per-key merge function.
def lock_sibling(yaml_file):
    base, ext = os.path.splitext(yaml_file)
    if base.endswith(".lock"):
        return None
    return base + ".lock" + (ext or ".yaml")

# What is printed in place of a secret, with a digest of what it stands
# for. A constant would have a plan call a changed password no change at
# all: the baseline it compares against is a dump too, so both sides
# would read the same. The digest changes with the secret and says
# nothing about it.
#
# Kept here rather than on BuildCmd: packages.py wants it too (to redact
# a package's own digest excerpt), and cannot import build.py without a
# cycle (build.py already imports image.py, which imports packages.py).
REDACTED = "<redacted:%s>"

def _redacted_match(match):
    return REDACTED % hashlib.sha256(match.group(0).encode()).hexdigest()[:8]

# The 'redact' section as expressions to match with. Compiled here so
# that a pattern that is not one is reported against the section that
# holds it, rather than as a traceback out of the middle of a dump.
def redactions(spec):
    patterns = []
    for pattern in (spec or {}).get("redact") or []:
        try:
            patterns.append(re.compile(pattern))
        except re.error as e:
            raise ValueError("redact: '%s' is not a pattern: %s"
                             % (pattern, e)) from e
    return patterns

# A value with every match of those patterns replaced. What matches is
# replaced and not the string holding it, so a pattern can name the
# secret inside a larger value -- the password of an ansible task whose
# other arguments are worth reading.
def redact(value, patterns):
    if type(value) == type({}):
        return {k: redact(v, patterns) for k, v in value.items()}
    if type(value) == type([]):
        return [redact(v, patterns) for v in value]
    if type(value) == type(""):
        for pattern in patterns:
            value = pattern.sub(_redacted_match, value)
    return value

# Label carrying the digest of what an image was built from.
INPUTS_LABEL = "seine.inputs"

# Label saying what an image is, which decides whether it is worth carrying
# to another machine. Every image seine builds says so for itself: a label is
# inherited by whatever is built FROM an image, so an image that said nothing
# would answer with whatever its base said -- and the imager's kernel, built
# on the target bootstrap, would call itself a root file-system.
KIND_LABEL = "seine.kind"

# The kinds there are. Two of them can be used by another machine as they
# are; the rest stand on the root file-system, which is not carried, and an
# image whose base is not the same image is rebuilt whatever else happens to
# it.
TOOLING_KIND = "tooling"      # apt and mmdebstrap: what makes the rest
BUILDER_KIND = "builder"      # where packages are built, holding the chroot
ROOTFS_KIND = "rootfs"        # what mmdebstrap made of the archive
IMAGER_KIND = "imager"        # the kernel libguestfs boots, and its appliance
TRANSPORT_KIND = "transport"  # a baseline plus what ansible needs
SOURCE_KIND = "source"        # host-arch, dpkg-dev -- where sources are pulled

# Building an sbuild chroot (mmdebstrap) or a source package inside one
# (sbuild's "unshare" backend, the one Debian's own buildds use) needs no
# schroot, no daemon and no root, just user namespaces -- but nesting one
# inside podman's own needs four things a plain 'podman run' does not
# give us, each found by hitting the failure it causes:
#
#  * the container has to run as root (uid 0, i.e. the unprivileged user
#    seine runs as). A non-root container user cannot use newuidmap at all:
#    every write to uid_map comes back EPERM, even with the setuid bit
#    intact and CAP_SETUID added to the container.
#
#  * /etc/subuid and /etc/subgid have to cover 65534. apt drops privileges
#    to _apt/nobody inside the chroot and setgroups(65534) fails with
#    EINVAL when that id falls outside the mapped range, which shows up as
#    an unexplained 'apt-get update' failure.
#
#  * CAP_SYS_ADMIN, because sbuild-usernsexec calls sethostname() and
#    podman's default capability set does not include it.
#
#  * an unmasked /proc: podman covers several paths under /proc, and the
#    kernel refuses to mount a fresh procfs inside a nested user namespace
#    while the parent's procfs has submounts hiding parts of it.
#
# The result is a container more privileged than the others seine builds,
# though still an unprivileged one in the kernel's eyes -- uid 0 in it is
# the user seine runs as, so what it can reach is that user's own files,
# not the machine's. Used by seine/sbuild.py's BuilderImage (both to make
# a chroot and to build in one) and by seine/vendor's resolver (to make
# the chroot its base_chroot() reads).
PRIVILEGED_RUN_OPTIONS = [
    "--cap-add=sys_admin",
    "--security-opt", "unmask=ALL",
]

