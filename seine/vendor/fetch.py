# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Fetching one source/binary package into the local vendor repository,
# and building it into a real, signed apt repository for a build to
# use (index()).

import collections
import os
import shutil
import subprocess

from seine.cache_index import VENDOR, Index, say
from seine.container import ContainerEngine
from seine.utils import locked
from seine import snapshot

from .manifest import (_binary_file_path, _binary_filename,
                       _binary_filename_legacy, deploy_repository, repository)


# ---------------------------------------------------------------------
# Fetching: turning a frozen manifest entry into real files, flat at the
# top of the suite's repository directory, not sorted into 'pool/
# <component>'. Component (main/extra) is decided fresh by index() every
# time, never baked into where a fetch put the bytes -- also the layout
# CacheCmd._evict() assumes.
#
# 'apt-get download'/'source --download-only' take no archive lock of
# their own, so every fetch writes straight into the repository under
# its own filename, and apt skips a file already there with the right
# hash. The lock here is only against index()'s own exclusive one: fetches
# run beside each other, but never while the index is being rebuilt.
# ---------------------------------------------------------------------

# Resolver fetches run as mapped root; _apt cannot write the host bind-mount -> unsandboxed warning. Disable sandbox per-command.
_APT_SANDBOX_OPTS = ["-o", "APT::Sandbox::User=root"]

def _artifact_key(suite, source, name, arch, version):
    return "%s_%s_%s_%s_%s" % (suite, source, name, arch or "-", version)

# Whether 'binpkg' is already fetched for 'arch' -- checked against both
# its own filename and 'all', since apt names an 'Architecture: all'
# package's file after 'all', not the arch qualifier it was fetched with.
# Each candidate is also tried under both epoch spellings.
def _binary_already_fetched(where, binpkg, arch, version):
    for candidate in (arch, "all"):
        for name in (_binary_filename(binpkg, candidate, version),
                     _binary_filename_legacy(binpkg, candidate, version)):
            if os.path.isfile(os.path.join(where, name)):
                return True
    return False

# Every (binpkg, arch, version) a source's 'binaries' names, sorted for
# stable task order, skipping any (binpkg, arch) already in 'seen' -- the
# same pair can be reachable from more than one source (build-dep
# closures overlap; 'ecj' did this live, crashing task lists with
# "duplicate task" before this guard existed). 'archs', when given,
# narrows what's considered before marking seen.
def _dedup_binaries(entry, seen, archs=None):
    for binpkg, per_arch in sorted(entry.get("binaries", {}).items()):
        for arch, version in sorted(per_arch.items()):
            if archs is not None and arch not in archs:
                continue
            key = (binpkg, arch)
            if key in seen:
                continue
            seen.add(key)
            yield binpkg, arch, version

def _deb_has_gocode(deb_path):
    try:
        result = subprocess.run(
            ["sh", "-c", "dpkg-deb -c \"$1\" 2>/dev/null | grep -q 'usr/share/gocode/src/'", "_", deb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return result.returncode == 0
    except Exception:
        return False

def _binary_has_gocode(where, binpkg, arch, version):
    deb = _binary_file_path(where, binpkg, arch, version)
    return deb is not None and _deb_has_gocode(deb)

def _index_has_gocode(bin_key):
    entry = Index().get(VENDOR, bin_key)
    if entry is None or "has_gocode" not in entry:
        return None
    return bool(entry["has_gocode"])

# The suite's persisted apt lists, populated by the resolve step's own
# 'apt-get update' -- every fetch needs them to turn 'name=version' back
# into a URI and a hash.
def _lists_volume(suite):
    return (ContainerEngine.downloads_lists(suite), "/var/lib/apt/lists")

# A file fetched from a recorded snapshot sha1 must match the lock's own
# hash exactly, or this refuses it and removes what was written -- a
# mismatch means either the mirror served something else or the lock
# was hand-edited. No retry, no falling back to apt.
def _snapshot_fetch(sess, url, dest, expected):
    digest = snapshot.download(sess, url, dest)
    if expected is not None and digest != expected:
        os.remove(dest)
        raise ValueError(
            "vendor: '%s' from snapshot.debian.org does not match the "
            "lock's own sha256 -- refusing it" % os.path.basename(dest))

# 'snapshot_hashes' ({filename: sha1}) is set only when a trusted lock
# already recorded one -- '--refresh' put it there because the live feed
# no longer serves this exact version. When set, files come straight
# from snapshot.debian.org, apt untouched. 'expected_hashes' is the
# lock's own 'file_hashes', checked file by file.
def fetch_source(builder, suite, source, version, snapshot_hashes=None,
                 expected_hashes=None, options=None):
    # Only a pure snapshot.debian.org fetch ever passes 'builder=None' --
    # a plain HTTPS download needs no container, so none was built.
    options = options if options is not None else builder.options
    where = repository(suite)
    if snapshot_hashes:
        with locked(where, shared=True):
            sess = snapshot.session()
            for fname, sha1 in snapshot_hashes.items():
                url = snapshot.file_url(sha1, fname)
                _snapshot_fetch(sess, url, os.path.join(where, fname),
                               (expected_hashes or {}).get(fname))
        key = _artifact_key(suite, source, "source", None, version)
        Index().made(VENDOR, key)
        say(options, "vendor source %s made (snapshot.debian.org)" % key)
        return
    with locked(where, shared=True):
        # 'src:', not a bare name: apt-get otherwise prefers a binary
        # package of the same name over the source one. Bookworm's
        # apt-get (2.6) doesn't understand 'src:' though (treats it as
        # package:arch); trixie+ does. Try 'src:' first, fall back on
        # that specific error.
        try:
            builder.exec(["apt-get"] + _APT_SANDBOX_OPTS + ["source", "--download-only", "-qq",
                          "src:%s=%s" % (source, version)],
                        workdir="/vendor-repo",
                        volumes=[(where, "/vendor-repo"), _lists_volume(suite)])
        except subprocess.CalledProcessError as e:
            msg = e.output or ""
            if "Can not find a package for architecture" in msg or "Unable to find a source package for src:" in msg:
                builder.exec(["apt-get"] + _APT_SANDBOX_OPTS + ["source", "--download-only", "-qq",
                              "%s=%s" % (source, version)],
                            workdir="/vendor-repo",
                            volumes=[(where, "/vendor-repo"), _lists_volume(suite)])
            else:
                raise
    key = _artifact_key(suite, source, "source", None, version)
    Index().made(VENDOR, key)
    say(options, "vendor source %s made" % key)

# Same as fetch_source(), for one binary -- singular hash/sha1, not a
# dict, since a binary is always one file, unlike a source's several.
def fetch_binary(builder, suite, binpkg, arch, version, snapshot_sha1=None,
                 expected_hash=None, options=None):
    options = options if options is not None else builder.options
    where = repository(suite)
    if snapshot_sha1:
        with locked(where, shared=True):
            fname = _binary_filename(binpkg, arch, version)
            dest = os.path.join(where, fname)
            url = snapshot.file_url(snapshot_sha1, fname)
            _snapshot_fetch(snapshot.session(), url, dest, expected_hash)
        key = _artifact_key(suite, binpkg, binpkg, arch, version)
        has = _binary_has_gocode(where, binpkg, arch, version)
        Index().made(VENDOR, key, metadata={"has_gocode": has})
        say(options, "vendor binary %s made (snapshot.debian.org)" % key)
        return
    with locked(where, shared=True):
        builder.exec(["apt-get", "-o", "APT::Architectures::=%s" % arch] + _APT_SANDBOX_OPTS +
                     ["download", "-qq", "%s:%s=%s" % (binpkg, arch, version)],
                    workdir="/vendor-repo",
                    volumes=[(where, "/vendor-repo"), _lists_volume(suite)])
    key = _artifact_key(suite, binpkg, binpkg, arch, version)
    has = _binary_has_gocode(where, binpkg, arch, version)
    Index().made(VENDOR, key, metadata={"has_gocode": has})
    say(options, "vendor binary %s made" % key)

# ---------------------------------------------------------------------
# Indexing and signing: identical in shape to Builder.index()
# (seine/packages.py), against the vendor's own repository and its own,
# independent signing key.
# ---------------------------------------------------------------------

# The signing key a suite's delivered repository carries, if signed --
# lets a reader (utils.py's apt_sources()) verify with 'signed-by' rather
# than trust it unconditionally.
def keyring(suite):
    where = deploy_repository(suite)
    if os.path.isfile(os.path.join(where, "InRelease")) == False:
        return None
    for name in sorted(os.listdir(where)):
        if name.endswith(".gpg") and name.startswith("Release") == False:
            return name
    return None

# Idempotent: a source and one of its binaries can share a name (e.g.
# 'abi-compliance-checker'), so the same 'dst' can be linked twice --
# already-linked is success, not a copy-onto-itself error.
def _hardlink(src, dst):
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)

# Hardlinks every flat file belonging to 'name' from 'fetched' (the
# cache) into 'where/pool/<component>/'. Hardlinked, not copied, so the
# durable cache copy and the delivered view never cost extra disk; falls
# back to a copy if the filesystem can't link (cache/vendor roots can be
# on different devices).
#
# '<name>_' identifies which files are this package's own -- '_', never
# '-', since dpkg always separates name from version with one. Matching
# a trailing '-' too (an earlier bug) made 'foo' swallow 'foo-dev's file.
def _link_fetched(fetched, where, component, name):
    dest = os.path.join(where, "pool", component)
    prefix = name + "_"
    for fname in sorted(os.listdir(fetched)):
        if not fname.startswith(prefix):
            continue
        src = os.path.join(fetched, fname)
        if os.path.isdir(src):
            continue
        _hardlink(src, os.path.join(dest, fname))

# Builds the delivered repository (pool/, dists/, flat Packages/Sources,
# Release+signature) under deploy_repository(suite), from what's already
# fetched into repository(suite) plus the resolved 'sources'. Never reads
# or writes the cache beyond that.
#
# 'sources'/'direct' are passed in rather than read off disk, so a suite
# served entirely from a committed lock (no local cache manifest) still
# indexes correctly.
#
# Rebuilt from scratch every call: main/extra classification is decided
# fresh each time from 'entries' and the gocode BFS, never migrated or
# persisted back onto 'sources' -- cheap since this does no network I/O,
# only hardlinking and apt-ftparchive/gpg.
def index(builder, suite, signer, sources, direct):
    fetched = repository(suite)
    where = deploy_repository(suite)
    with locked(fetched):
        shutil.rmtree(where, ignore_errors=True)
        os.makedirs(where, exist_ok=True)

        # Always create pool/main, pool/extra and dists/<suite>/main, dists/<suite>/extra
        # even if one is empty -- main/extra are always present.
        for comp in ["main", "extra"]:
            os.makedirs(os.path.join(where, "pool", comp), exist_ok=True)
            os.makedirs(os.path.join(where, "dists", suite, comp, "binary-amd64"), exist_ok=True)
            os.makedirs(os.path.join(where, "dists", suite, comp, "source"), exist_ok=True)

        # Build binpkg -> (owning source, per_arch version dict)
        bin_owner = {}
        for src, ent in sources.items():
            for binpkg, per_arch in ent.get("binaries", {}).items():
                bin_owner[binpkg] = (src, per_arch)

        # Cache has_gocode from Index metadata, advisory only
        has_map = {}
        for kind, key, entry in Index().entries():
            if kind != VENDOR:
                continue
            if "has_gocode" in entry:
                has_map[key] = bool(entry["has_gocode"])

        def bin_has_gocode(binpkg):
            src, per_arch = bin_owner.get(binpkg, (None, {}))
            if src is None:
                return False
            for arch, ver in per_arch.items():
                key = _artifact_key(suite, binpkg, binpkg, arch, ver)
                cached = has_map.get(key)
                if cached is not None:
                    if cached:
                        return True
                    continue
                found = _binary_has_gocode(fetched, binpkg, arch, ver)
                has_map[key] = found
                # backfill Index for next rebuild
                try:
                    Index().patch(VENDOR, key, {"has_gocode": found})
                except Exception:
                    pass
                if found:
                    return True
            return False

        # BFS over build_dep_bins seeded from the directly-asked names.
        direct_names = set(direct) & set(sources)
        promoted = set()
        queue = collections.deque(direct_names)
        seen_queue = set(queue)
        while queue:
            src = queue.popleft()
            for binpkg in sources[src].get("build_dep_bins", []):
                if not bin_has_gocode(binpkg):
                    continue
                target, _ = bin_owner.get(binpkg, (None, None))
                if (target is None or target in promoted or
                        target in direct_names):
                    continue
                promoted.add(target)
                if target not in seen_queue:
                    seen_queue.add(target)
                    queue.append(target)

        main_names = direct_names | promoted
        for source, entry in sorted(sources.items()):
            component = "main" if source in main_names else "extra"
            _link_fetched(fetched, where, component, source)
            for binpkg in entry.get("binaries", {}):
                _link_fetched(fetched, where, component, binpkg)

        script = ""
        for comp in ["main", "extra"]:
            script += ("apt-ftparchive packages pool/{comp} > dists/{suite}/{comp}/binary-amd64/Packages && "
                       "gzip -9 -c dists/{suite}/{comp}/binary-amd64/Packages > dists/{suite}/{comp}/binary-amd64/Packages.gz && "
                       "apt-ftparchive sources pool/{comp} > dists/{suite}/{comp}/source/Sources && "
                       "gzip -9 -c dists/{suite}/{comp}/source/Sources > dists/{suite}/{comp}/source/Sources.gz && ").format(
                           comp=comp, suite=suite)
        # Flat Packages/Sources over 'pool' itself (recurses both
        # components) for backward compat -- never 'pool/main pool/extra':
        # apt-ftparchive reads a second positional arg as an override
        # file, not a second directory, and used to silently drop 'extra'.
        #
        # No '--db' cache: 'where' is wiped and rebuilt every call, so it
        # would never pay for itself.
        script += ("apt-ftparchive packages pool > Packages; "
                   "gzip -9 -c Packages > Packages.gz; "
                   "apt-ftparchive sources pool > Sources; "
                   "gzip -9 -c Sources > Sources.gz")
        if signer is not None:
            script += " && apt-ftparchive release . > Release"
        builder.exec(["sh", "-c", script], volumes=[(where, "/vendor-repo")],
                    workdir="/vendor-repo")

        if signer is not None:
            signer.export(os.path.join(where, signer.keyring()))
            signer.sign_release(os.path.join(where, "Release"))
