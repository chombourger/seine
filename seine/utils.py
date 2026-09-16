# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import base64
import contextlib
import fcntl
import functools
import hashlib
import os
import platform
import re
import shlex
import subprocess

# Debian arch of the machine seine runs on. Anything else is a cross build.
HOST_MACHINE_TO_ARCH = {
    "x86_64":  "amd64",
    "aarch64": "arm64",
    "armv7l":  "armhf",
    "i686":    "i386",
}
HOST_ARCH = HOST_MACHINE_TO_ARCH.get(platform.machine(), platform.machine())

# Host dir bind-mounted into the builder container, where package sources
# are fetched and built. Kept here (not packages.py) since kernel/module
# code shares this same dir and can't import packages.py.
WORKDIR = "/src"

# Fixed identity for patch commits, so their hash is reproducible by anyone.
GIT_NAME  = "seine"
GIT_EMAIL = "seine@localhost"

# Defaults/validates the 'distribution' section once, shared by
# Image.parse() and VendorCmd.
def distribution(spec):
    distro = spec["distribution"] if "distribution" in spec else {}
    if "source" not in distro:
        distro["source"] = "debian"
    # 'bookworm': current oldstable, still served. 'buster' was tried before
    # but went EOL and dropped off deb.debian.org, breaking builds silently.
    if "release" not in distro:
        distro["release"] = "bookworm"
    if "architecture" not in distro:
        distro["architecture"] = "amd64"
    if "uri" not in distro:
        distro["uri"] = "http://ftp.debian.org/debian"
    # DEB_BUILD_PROFILES / DEB_BUILD_OPTIONS for Build-Depends. Optional.
    for key in ("build-options", "build-profiles"):
        if key in distro:
            val = distro[key]
            if isinstance(val, str):
                val = [val]
            if not isinstance(val, list) or any(not isinstance(v, str) for v in val):
                raise ValueError("'%s' shall be a string or list of strings" % key)
            distro[key] = val
    spec["distribution"] = distro

    # Validate now, so a bad feed is reported against the spec, not
    # halfway through a build.
    feeds(distro)
    return distro

# The apt feeds a system is built from, listed explicitly rather than
# guessed (suites like -updates/-security differ per release). No
# 'feeds:' means the release itself is the only feed.
#
# 'sources: false' is for vendor feeds with binaries only. 'valid-until:
# false' is for archives meant to stay expired (snapshot.debian.org,
# frozen mirrors). 'release:' groups a pocket (e.g. 'bookworm-backports')
# with its base release; never guessed from the suite name, since that
# could silently mix releases in a build-dependency closure. 'signed-by'
# is the feed's own trust anchor -- 'vault:<name>' (read-only, verified
# independently of whatever the feed's own server hands out), an armored
# key inline, or a path to one; 'fingerprint' pins it against rotation.
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
                               "uri", "valid-until", "signed-by", "fingerprint"]:
                raise ValueError(
                    "feed #%d ('%s') has no '%s' setting, expected one of "
                    "components, release, sources, suite, uri, valid-until, "
                    "signed-by, fingerprint"
                    % (index + 1, entry["suite"], setting))
        if "fingerprint" in entry and "signed-by" not in entry:
            raise ValueError(
                "feed #%d ('%s') has a 'fingerprint' but no 'signed-by' to "
                "check it against" % (index + 1, entry["suite"]))
        parsed.append({
            "uri":         entry.get("uri", distro["uri"]),
            "suite":       entry["suite"],
            "release":     entry.get("release", entry["suite"]),
            # falls back to the distribution-wide default component(s)
            "components":  entry.get("components",
                                     distro.get("components", "main")),
            "sources":     entry.get("sources", True),
            "valid_until": entry.get("valid-until", True),
            "signed_by":   entry.get("signed-by"),
            "fingerprint": entry.get("fingerprint"),
        })
    return parsed

# The feed for the release itself (not the first one merge order lists),
# which a root file-system bootstraps from.
def base_feed(distro):
    release = distro["release"]
    for feed in feeds(distro):
        if feed["suite"] == release:
            return feed
    raise ValueError("no feed for suite '%s'!" % release)

# Where a feed's own resolved trust anchor lands, installed by
# feed_keyrings_script() before anything reads from it. Named by suite,
# so it never collides with packages.py's own KEYRINGS use of the same
# directory (a rebuilt repository's key, named by fingerprint).
FEED_KEYRINGS = "/etc/apt/keyrings"

# Just the string -- no vault/file I/O here, so a caller that only wants
# apt's own sources.list text (e.g. a rebuild stamp) never pays for or
# depends on reaching a vault. Resolving the bytes behind it is
# feed_keyrings_script()'s job, called separately by whatever actually
# runs a shell.
def _feed_options(feed):
    options = []
    if feed["signed_by"] is not None:
        options.append("signed-by=%s/%s.gpg" % (FEED_KEYRINGS, feed["suite"]))
    if not feed["valid_until"]:
        options.append("check-valid-until=no")
    return "[%s] " % " ".join(options) if options else ""

# Those feeds as apt would write them down. 'sources' adds deb-src lines
# for feeds that carry them. 'entries' overrides which feeds to use (else
# all of them). 'offline' must be passed explicitly by the one caller that
# means it (ansible_runner.py); every other caller of this function still
# wants the network.
def apt_sources(distro, sources=False, entries=None, offline=False):
    lines = []
    for feed in entries if entries is not None else feeds(distro):
        if offline:
            lines += _offline_feed(feed, sources)
            continue
        feed = dict(feed, options=_feed_options(feed))
        lines.append("deb %(options)s%(uri)s %(suite)s %(components)s" % feed)
        if sources and feed["sources"]:
            lines.append("deb-src %(options)s%(uri)s %(suite)s %(components)s" % feed)
    return lines

# Where a Dockerfile RUN instruction writes its baked-in feeds. Separate
# from FEEDS_LIST (ansible_runner.py), which is for a container already
# running, not being built.
DOCKERFILE_SOURCES_LIST = "/etc/apt/sources.list.d/seine.list"

# apt's package lists, stale right after 'apt-get update' and never
# needed once an image is built. Safe to remove anywhere.
APT_LISTS_CLEANUP = "rm -rf /var/lib/apt/lists/*"

# Doc/info/man pages, on top of APT_LISTS_CLEANUP. Only for seine's own
# tooling images, never for images (TransportBootstrap etc.) that a build
# ships to the user.
APT_CLEANUP = "rm -rf /usr/share/doc /usr/share/info /usr/share/man && " \
             + APT_LISTS_CLEANUP

# Turns a feed's own 'signed-by' into the actual keyring bytes: a vault
# read (the whole point -- verified independently of whatever the feed's
# own https server hands out), an inline armored block, or a path to
# one. Always returns a binary (dearmored) keyring, the form apt's
# 'signed-by' wants -- gpg's own '--export' shape, not the ASCII-armored
# one a vault or a pasted block naturally comes in.
def _resolve_signed_by(value):
    from seine.signing import _dearmor
    if value.startswith("vault:"):
        from seine import vault
        name = value[len("vault:"):]
        provider = vault.for_build()
        try:
            armored = provider.pgp_public_key(name)
        except vault.VaultError as e:
            raise ValueError(
                "feed signed-by: no vault pgp key '%s': %s" % (name, e)) from e
        return _dearmor(armored)
    if value.lstrip().startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        return _dearmor(value)
    try:
        with open(value, "rb") as f:
            data = f.read()
    except OSError as e:
        raise ValueError("feed signed-by: cannot read '%s': %s" % (value, e)) from e
    if data.lstrip().startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        return _dearmor(data.decode())
    return data

# The fingerprint of an already-resolved keyring, without ever writing it
# to a real gpg keyring -- 'import-show' only parses and reports, never
# imports. Used for the path/inline forms; the vault form asks the vault
# itself instead (_verify_fingerprint below) -- stronger, since it is
# independent of the exported bytes and catches a vault-side key swap the
# export alone would not.
def _gpg_fingerprint_of(data):
    from seine.signing import _first_fingerprint
    try:
        out = subprocess.check_output(
            ["gpg", "--batch", "--with-colons", "--dry-run",
             "--import-options", "import-show", "--import"],
            input=data, stderr=subprocess.PIPE)
    except FileNotFoundError:
        raise ValueError(
            "feed signed-by: checking a 'fingerprint' needs gpg on this machine")
    except subprocess.CalledProcessError as e:
        raise ValueError("feed signed-by: gpg could not read the key: %s"
                         % (e.stderr or b"").decode(errors="replace").strip())
    fingerprint = _first_fingerprint(out)
    if fingerprint is None:
        raise ValueError("feed signed-by: gpg did not report a fingerprint for the key")
    return fingerprint

def _verify_fingerprint(value, data, expected):
    if value.startswith("vault:"):
        from seine import vault
        actual = vault.for_build().pgp_fingerprint(value[len("vault:"):])
    else:
        actual = _gpg_fingerprint_of(data)
    if actual.replace(" ", "").upper() != expected.replace(" ", "").upper():
        raise ValueError(
            "feed signed-by: fingerprint mismatch -- expected %s, got %s"
            % (expected, actual))

# Shell fragment installing every 'entries' feed's own resolved keyring
# under FEED_KEYRINGS, to run before anything reads from it. Does real
# I/O (a vault call, a file read) unlike apt_sources() itself -- called
# only by whatever is about to actually run a shell, never by a
# stamp/digest, which must stay offline and deterministic. Offline feeds
# trust the vendor's own key instead (_offline_feed()), so this is a
# no-op for them.
def feed_keyrings_script(entries, offline=False):
    if offline:
        return ""
    commands = []
    for feed in entries:
        signed_by = feed.get("signed_by")
        if signed_by is None:
            continue
        data = _resolve_signed_by(signed_by)
        fingerprint = feed.get("fingerprint")
        if fingerprint is not None:
            _verify_fingerprint(signed_by, data, fingerprint)
        encoded = base64.b64encode(data).decode()
        commands.append(
            "mkdir -p %s && printf '%%s' %s | base64 -d > %s/%s.gpg"
            % (FEED_KEYRINGS, shlex.quote(encoded), FEED_KEYRINGS, feed["suite"]))
    return " && ".join(commands)

# Shell fragment a Dockerfile RUN chains before 'apt-get update': writes
# 'entries' so the image uses the spec's own feeds instead of the base
# image's. 'true' when there's nothing to add, so callers can chain it
# unconditionally.
def apt_sources_dockerfile(distro, entries, sources=False, offline=False):
    lines = apt_sources(distro, sources=sources, entries=entries, offline=offline)
    if len(lines) == 0:
        return "true"
    install = feed_keyrings_script(entries, offline=offline)
    echoes = " && ".join("echo '%s' >> %s" % (line, DOCKERFILE_SOURCES_LIST)
                         for line in lines)
    return " && ".join(part for part in [install, echoes] if part)

# base_feed()'s uri/components as a short tag, so two specs that differ
# only there don't collide on one image tag.
def feed_digest(distro):
    return hashlib.sha256(
        repr(sorted(base_feed(distro).items())).encode()).hexdigest()[:8]

# Where an offline feed's vendor repository is bind-mounted inside a
# container. One mountpoint per suite, since two offline feeds in the
# same container each need their own tree.
VENDOR_MOUNTPOINT = "/vendor-repo"

def vendor_mountpoint(suite):
    return "%s/%s" % (VENDOR_MOUNTPOINT, suite)

# A feed rewritten to read from its suite's local vendor instead of the
# network. Uses 'signed-by' if the vendor was signed
# ('--vendor-sign-key'), else trusts it outright like an unsigned
# 'packages:' repository.
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

# Suites to bind-mount for offline feeds. Empty unless 'apt-pull-mode:
# offline' is set, so callers can bind-mount unconditionally.
def offline_suites(distro, entries=None):
    if distro.get("apt-pull-mode") != "offline":
        return []
    return sorted({feed["suite"] for feed
                  in (entries if entries is not None else feeds(distro))})

# Shell fragment writing 'entries' into 'target' inside a container,
# shared by ansible_runner.py and sbuild.py. Going offline replaces
# /etc/apt's existing sources rather than adding to them, so no baked-in
# entry is left still reaching for the network.
def offline_apt_script(distro, entries, target, offline=False):
    lines = apt_sources(distro, sources=True, entries=entries, offline=offline)
    script = ""
    if offline:
        script += ("rm -f /etc/apt/sources.list "
                  "/etc/apt/sources.list.d/*.sources "
                  "/etc/apt/sources.list.d/*.list; ")
    install = feed_keyrings_script(entries, offline=offline)
    if install:
        script += install + "; "
    script += "".join("echo '%s' >> %s; " % (line, target) for line in lines)
    return script

# One build at a time for a cache two builds share, so two writers don't
# race and leave a half-written file behind. The lock sits beside the
# thing it guards.
#
# 'shared' is for many readers at once but no writer meanwhile (e.g. a
# prune must not run while any build is still tagging images). 'blocking'
# False raises BlockingIOError instead of waiting, for work another
# holder will do anyway.
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

# Digest of a set of spec file names (not contents, which change with
# every edit), used to file things per-specification: last plan, logs.
def digest(files, length=None):
    named = "\0".join(_portable_name(f) for f in files)
    return hashlib.sha256(named.encode()).hexdigest()[:length]

# A file's name independent of where it was checked out (a raw abspath()
# would give the same spec a different digest per clone). Named relative
# to its git remote (or toplevel dir name if no remote); anything outside
# git keeps its absolute path.
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

# 'origin' if there is one, else whichever remote sorts first. Which one
# doesn't matter, only that it's picked the same way everywhere.
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

# git status --porcelain's two-letter code for one file, or None
# outside a repo or when clean. Never cached, unlike the git
# helpers above, since it must reflect live edits.
def git_status(path):
    abspath = os.path.abspath(path)
    toplevel = _git_toplevel(os.path.dirname(abspath))
    if toplevel is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", toplevel, "status", "--porcelain", "--", abspath],
            capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out[:2] if out else None

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

# The lock file a spec pairs with, kas style: 'foo.yaml' pairs with
# 'foo.lock.yaml'. A file already named '*.lock.yaml' gets no lock of its
# own. Generic, not vendor-specific: BuildCmd.load_all() auto-loads
# whatever this names if it exists.
def lock_sibling(yaml_file):
    base, ext = os.path.splitext(yaml_file)
    if base.endswith(".lock"):
        return None
    return base + ".lock" + (ext or ".yaml")

# Printed in place of a secret, with a digest of it so a plan can still
# tell a changed secret from an unchanged one. Kept here (not on
# BuildCmd) so packages.py can use it too without a circular import.
REDACTED = "<redacted:%s>"

def _redacted_match(match):
    return REDACTED % hashlib.sha256(match.group(0).encode()).hexdigest()[:8]

def _redacted_value(value):
    return REDACTED % hashlib.sha256(str(value).encode()).hexdigest()[:8]

# Hides a value whole (a path rule), unlike a pattern which only takes
# out what it matches inside it.
def _redact_whole(value):
    if type(value) == type({}):
        return {k: _redact_whole(v) for k, v in value.items()}
    if type(value) == type([]):
        return [_redact_whole(v) for v in value]
    if type(value) == type(""):
        return _redacted_value(value)
    return value

def _redact_path(entry):
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("redact: a path entry needs a non-empty 'path'")
    return tuple(path.split("."))

# Always hidden, even if the spec's own 'redact:' doesn't ask.
ALWAYS_REDACTED_PATHS = [("defaults", "vault")]

# Builds the 'redact' rules: a plain string is a regex (matched anywhere
# in a string), a '{path: ...}' mapping is a dotted location whose value
# is hidden whole. Returns a '(patterns, paths)' pair for redact().
def redactions(spec):
    patterns = []
    paths = list(ALWAYS_REDACTED_PATHS)
    for entry in (spec or {}).get("redact") or []:
        if isinstance(entry, dict):
            paths.append(_redact_path(entry))
            continue
        try:
            patterns.append(re.compile(entry))
        except re.error as e:
            raise ValueError("redact: '%s' is not a pattern: %s"
                             % (entry, e)) from e
    # Vault values redact themselves: the fragment holding a secret is
    # rarely the one that knows it is one.
    try:
        from seine import vault as _vault
        for secret in _vault.secrets():
            if secret and len(secret) >= 4:
                patterns.append(re.compile(re.escape(secret)))
    except ImportError:
        pass
    return patterns, paths

# Applies patterns and path rules to 'value'. 'path' is where 'value'
# sits in the spec -- pass a starting point when walking a subtree.
def redact(value, rules, path=()):
    patterns, paths = rules
    if any(path[:len(p)] == p for p in paths):
        return _redact_whole(value)
    if type(value) == type({}):
        return {k: redact(v, rules, path + (k,)) for k, v in value.items()}
    if type(value) == type([]):
        return [redact(v, rules, path) for v in value]
    if type(value) == type(""):
        for pattern in patterns:
            value = pattern.sub(_redacted_match, value)
    return value

# Label carrying the digest of what an image was built from.
INPUTS_LABEL = "seine.inputs"

# Label saying what an image is. Every image seine builds sets it itself,
# since labels are inherited from the base image otherwise (an imager
# built on a rootfs would else be mislabeled as one).
KIND_LABEL = "seine.kind"

# The kinds there are.
TOOLING_KIND = "tooling"      # apt and mmdebstrap: what makes the rest
BUILDER_KIND = "builder"      # where packages are built, holding the chroot
ROOTFS_KIND = "rootfs"        # what mmdebstrap made of the archive
IMAGER_KIND = "imager"        # the kernel libguestfs boots, and its appliance
TRANSPORT_KIND = "transport"  # a baseline plus what ansible needs
SOURCE_KIND = "source"        # host-arch, dpkg-dev -- where sources are pulled

# sbuild's user-namespace backend needs no root, but nesting it inside
# podman's own user namespace needs extra options a plain 'podman run'
# doesn't give:
#  * root (uid 0) inside the container -- else newuidmap fails on every
#    uid_map write with EPERM.
#  * CAP_SYS_ADMIN, since sbuild-usernsexec calls sethostname().
#  * an unmasked /proc, else the kernel refuses to mount a fresh procfs
#    inside the nested namespace.
# Still unprivileged from the kernel's point of view: uid 0 here is just
# the host user seine runs as. Used by sbuild.py's BuilderImage and by
# seine/vendor's resolver.
PRIVILEGED_RUN_OPTIONS = [
    "--cap-add=sys_admin",
    "--security-opt", "unmask=ALL",
]

