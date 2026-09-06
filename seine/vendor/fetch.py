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
# top of the suite's repository directory -- not sorted into a
# 'pool/<component>' of their own. A fetched file is named by apt after
# the package and version alone (component is not part of a Debian
# filename), so it stays valid regardless of which spec, or which run's
# closure, called it 'main' or 'extra' -- classification is decided fresh
# by index() out of the manifest every time (see its own comment), not
# baked into where a fetch put the bytes. This is also the layout
# CacheCmd._evict() (seine/cache.py) already assumes for a vendor
# artifact.
#
# 'apt-get download'/'apt-get source --download-only' take no archive
# lock of their own -- unlike 'apt-get install', which is why
# seine/ansible_runner.py seeds a container-local archives directory
# rather than bind-mounting one shared between builds -- so every fetch
# writes straight into the repository, each under its own filename, and
# apt itself skips a file already there with the right hash. The lock is
# only against index()'s own exclusive one: several fetches run beside
# each other, and none may run while the index is being (re)built.
# ---------------------------------------------------------------------

# Resolver fetches run as mapped root; _apt cannot write the host bind-mount -> unsandboxed warning. Disable sandbox per-command.
_APT_SANDBOX_OPTS = ["-o", "APT::Sandbox::User=root"]

def _artifact_key(suite, source, name, arch, version):
    return "%s_%s_%s_%s_%s" % (suite, source, name, arch or "-", version)

# Whether 'binpkg' is already fetched for 'arch' -- checked against both
# its own filename and the 'all' one: an 'Architecture: all' package is
# still resolved and fetched per requested arch (RESOLVE_SCRIPT asks apt
# for it qualified 'binpkg:arch', the same as fetch_binary() does), but
# apt names the file it writes after the package's own architecture, not
# the qualifier used to select it -- so 'binpkg_version_all.deb' is what
# is actually on disk for one of these, never 'binpkg_version_amd64.deb'.
# Each arch candidate is itself tried under both epoch spellings (see
# _binary_filename()/_binary_filename_legacy() above) -- a version with no
# ':' at all makes the two identical, so this never doubles the real work.
def _binary_already_fetched(where, binpkg, arch, version):
    for candidate in (arch, "all"):
        for name in (_binary_filename(binpkg, candidate, version),
                     _binary_filename_legacy(binpkg, candidate, version)):
            if os.path.isfile(os.path.join(where, name)):
                return True
    return False

# Every (binpkg, arch, version) a source's own 'binaries' names, sorted
# for the same stable-task-order reason tasks.py's own 'ordered()'
# already cares about, and skipping any (binpkg, arch) already in
# 'seen' -- the same pair can legitimately be reachable from more than
# one source's own entry in one suite (build-dep closures overlap; a
# real example, found live: 'ecj', reachable from more than one source
# in the same suite, crashed fetch_tasks()'s and _enrich_for_lock()'s
# own task lists with "duplicate task" before each grew this same
# guard by hand). 'seen' is mutated as it goes -- the one set threaded
# through every source in a suite's own manifest, by the caller.
# 'archs', when given, narrows which architectures are even considered
# *before* marking one seen, so a source visited again for an
# architecture this call was not asked about still gets a chance at it.
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

# The suite's own persisted apt lists, bind-mounted read-only: the
# resolve step's 'apt-get update' (inside RESOLVE_SCRIPT) already
# populated them, and every fetch, in its own throwaway container, needs
# them to turn 'name=version' back into a URI and a hash.
def _lists_volume(suite):
    return (ContainerEngine.downloads_lists(suite), "/var/lib/apt/lists")

# A file downloaded from a recorded snapshot sha1 must turn out to be
# exactly the bytes the lock already expects (its own 'file_hashes'/
# 'binary_hashes'), or this refuses it outright and removes what was
# written -- a mismatch here means either snapshot.debian.org served
# something other than what '--refresh' verified, or the lock itself
# was hand-edited, and either way trusting the file silently would
# defeat the entire reason a hash was recorded in the first place. No
# retry, no falling back to apt: 'snapshot_hashes'/'expected_hash' are
# only ever set from a trusted lock (see fetch_tasks()'s own comment),
# so apt was never going to have this exact version anyway.
def _snapshot_fetch(sess, url, dest, expected):
    digest = snapshot.download(sess, url, dest)
    if expected is not None and digest != expected:
        os.remove(dest)
        raise ValueError(
            "vendor: '%s' from snapshot.debian.org does not match the "
            "lock's own sha256 -- refusing it" % os.path.basename(dest))

# 'snapshot_hashes' ({filename: sha1}) is set only for a source whose
# entry (in a trusted lock) already recorded one -- '--refresh' put it
# there because the live feed no longer served this exact version (see
# _enrich_for_lock()). When set, every one of the source's own files is
# downloaded directly, apt never touched at all: there would be nothing
# for apt to resolve the pinned version against anyway (see
# VendorCmd._run()'s own comment on why a locked suite is never
# resolved), and a plain HTTPS download needs neither a builder
# container nor a populated apt index. The sha1 (not a url -- see
# _enrich_for_lock()'s own comment on why the lock never stores one)
# and the filename already in hand are all snapshot.file_url() needs.
# 'expected_hashes' is the lock's own 'file_hashes', checked file by
# file.
def fetch_source(builder, suite, source, version, snapshot_hashes=None,
                 expected_hashes=None, options=None):
    # 'options' defaults to the real builder's own -- only a pure
    # snapshot.debian.org fetch (see fetch_tasks()'s own comment) ever
    # passes 'builder=None', and then only because it already has
    # 'options' of its own to give: a plain HTTPS download needs no
    # container, so nothing ever built one for it.
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
        # 'src:', not a bare name: apt-get prefers a *binary* package of
        # the same name over a source one when both exist -- see
        # build_deps()'s own comment, in resolve.py's RESOLVE_SCRIPT, for
        # a concrete case. Bookworm's apt-get source (2.6) does not understand
        # src: and treats it as package:arch (hence 'Can not find a package
        # for architecture ...'), while trixie+ does. Try src: first, fall
        # back to plain name on that specific error.
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

# The same, for one binary -- 'snapshot_sha1'/'expected_hash' are the
# lock's own 'binary_snapshot'/'binary_hashes' entries for this
# binpkg/arch, singular rather than a dict: a binary is one file, never
# several the way a source's own '.dsc'/'.orig.tar.*'/'.debian.tar.*'
# are. The url snapshot.file_url() builds from the sha1 needs the
# binary's own canonical filename, which _binary_filename() already
# knows how to spell (the modern epoch escaping apt itself writes --
# see its own comment).
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

# The key a suite's *delivered* repository carries, if it carries one and
# is actually signed -- the same idea as packages.py's own keyring(), so a
# caller reading it back offline (utils.py's apt_sources()) can ask apt to
# verify it with 'signed-by' rather than trust it unconditionally.
def keyring(suite):
    where = deploy_repository(suite)
    if os.path.isfile(os.path.join(where, "InRelease")) == False:
        return None
    for name in sorted(os.listdir(where)):
        if name.endswith(".gpg") and name.startswith("Release") == False:
            return name
    return None

# Idempotent, not just retried: two callers asking for the same 'dst'
# (a source and one of its own binaries sharing a name -- common,
# 'abi-compliance-checker' names both) means this is asked twice over
# the very same file, and finding it already linked is success, not a
# reason to fall back to copying a file onto itself.
def _hardlink(src, dst):
    if os.path.exists(dst):
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)

# Hardlinks every flat file fetch_source()/fetch_binary() left under
# 'fetched' (repository(suite), the cache) that belongs to 'name' -- a
# source's .dsc/.orig.tar.*/.debian.tar.*, or a binary's .deb -- into
# 'where/pool/<component>/' (where is deploy_repository(suite)).
# Hardlinked, not copied, so a fetched file existing in two places (the
# flat, durable cache copy and this run's delivered view of it) never
# costs extra disk; falls back to a copy only if the filesystem itself
# refuses the link (cache and vendor can be moved to different roots of
# their own -- SEINE_CACHE_DIR/SEINE_VENDOR_DIR -- so unlike a fetch
# failing halfway, which must not take the rest of an index down with
# it, this one really can cross devices).
#
# Debian doesn't put a component in a filename, so '<name>_' is what
# identifies which flat files are this package's own -- the same match
# CacheCmd._evict() (seine/cache.py) already uses to find them by key.
# '_', never '-': dpkg's own naming always separates a package's name
# from its version with an underscore, and only that -- matching a
# trailing '-' too (as an earlier version of this function, and the
# promotion code it replaced, both did) makes 'foo' swallow 'foo-dev's
# own file as well, wherever one package's name is a hyphenated prefix
# of another's, which linking the same file twice over then trips on.
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

# Builds the *delivered* repository -- pool/, dists/, the flat compat
# Packages/Sources, Release and its signature -- entirely under
# deploy_repository(suite), out of what fetch_source()/fetch_binary()
# already fetched into repository(suite) (the cache) and the resolved
# 'sources' handed in. Nothing is read from, or written to, the cache
# beyond that: cache holds only ever the flat, durable fetched files,
# never a pool/dists view of its own -- see repository()'s and
# deploy_repository()'s own comments for why the two are kept apart.
#
# 'sources'/'direct' are the caller's own -- VendorCmd._run()'s
# 'manifests[suite]' (whether freshly resolved, read from the cache
# manifest, or trusted outright from a committed lock) and the names
# 'entries_for(entries, suite)' asks for directly. Not read off disk
# here: an earlier version called load_manifest(suite) itself, which
# silently indexed nothing for a suite served entirely from a lock with
# no cache manifest of its own on this machine (a fresh checkout, say)
# -- fetch_tasks() already took its manifest as an argument; index()
# now does too, for the same reason.
#
# Rebuilt from scratch every call, never incrementally: a source or
# binary's main-vs-extra classification is decided here, fresh, every
# time -- not migrated file-by-file from wherever a previous run put
# it, and never persisted back onto 'sources' either (an earlier
# version wrote a promoted source's own "direct: true" back into the
# cache manifest -- redundant: 'direct' is nothing a source's own
# resolve couldn't already tell you from 'entries' plus which of its
# build-deps carry gocode, both read fresh below, so keeping a copy of
# the answer in every serialized manifest/lock only gave a hand-edited
# file something to disagree with). That is also what lets
# classification change freely between two runs (a different spec, or
# the closure resolving differently) without a "promotion" step to
# patch up afterwards, and it is cheap enough to always do outright: no
# network call anywhere in this function, only hardlinking files
# already on disk and running apt-ftparchive/gpg -- called by
# index_tasks() as part of a build's own 'vendor' task, which 'rootfs'
# waits on (image.py's own task graph) rather than reindexing again
# itself.
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

        # BFS closure over build_dep_bins seeded from the directly-asked
        # names -- 'direct' is 'entries_for(entries, suite)' names, not a
        # stored flag (see this function's own comment above).
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
        # Keep flat Packages/Sources for backward compat (packages in
        # pool/main+extra) -- one directory, 'pool' itself, which
        # apt-ftparchive recurses into on its own, picking up both
        # components. Never two directories named on one 'packages'/
        # 'sources' line -- apt-ftparchive reads a second positional
        # argument as an override *file*, not a second directory, so
        # 'pool/main pool/extra' silently dropped everything under
        # 'extra' (and errored trying to fgets() a directory as one)
        # every time this used to run that way.
        #
        # No '--db' cache: that only ever paid for itself indexing an
        # otherwise-unchanged tree a second time, which never happens --
        # 'where' is wiped and rebuilt from nothing on every call.
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
