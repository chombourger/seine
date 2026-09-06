# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The 'vendor:' spec section (VendorPackage and friends), the on-disk
# repository/lock layout, and the resolved dependency graph's own
# manifest and lock file -- load/save, not how it gets built (that's
# resolve.py) or fetched (fetch.py).

import hashlib
import json
import os
import re
import yaml

from seine.cache_index import VENDOR, Index
from seine.utils import ContainerEngine, feeds, apt_sources


# One entry of the 'vendor:' section: a source package to vendor, plus the
# conditions -- 'suite:'/'arch:'/'version:' -- deciding when it applies.
# Keyed by source package name rather than by binary package name, the way
# 'packages:' is keyed by source: binaries are derived from a source and
# not the other way round.
class VendorPackage:
    def __init__(self, spec, index):
        self.index = index
        if type(spec) != type({}):
            raise ValueError("vendor #%d is not a dictionary!" % index)
        self.spec = spec
        self.name = self._parse_name(spec)
        self.suites = self._parse_list(spec, "suite")
        self.architectures = self._parse_list(spec, "arch")
        self.version = self._parse_version(spec)

    def _error(self, message):
        return ValueError("vendor #%d ('%s'): %s" % (self.index, self.name, message))

    def _parse_name(self, spec):
        name = spec.get("name")
        if type(name) != type(""):
            raise ValueError(
                "vendor #%d has no 'name' -- a source package to vendor" % self.index)
        if re.match(r"^[a-z0-9][a-z0-9+.-]+$", name) is None:
            raise ValueError(
                "vendor #%d: '%s' is not a source package name: those are "
                "lowercase, start with a letter or a digit, are at least "
                "two characters, and hold only letters, digits and '+', "
                "'-' or '.'" % (self.index, name))
        return name

    # 'suite:'/'arch:' each take one name or a list of them; unset means
    # "every suite/architecture this asks for", which is the entry itself
    # applying everywhere rather than nowhere.
    def _parse_list(self, spec, key):
        values = spec.get(key)
        if values is None:
            return None
        if type(values) == type(""):
            values = [values]
        if type(values) != type([]) or any(type(v) != type("") for v in values):
            raise self._error("'%s' shall be a name or a list of them" % key)
        if len(values) == 0:
            raise self._error("'%s' is empty" % key)
        return values

    def _parse_version(self, spec):
        version = spec.get("version")
        if version is not None and type(version) != type(""):
            raise self._error(
                "'version' shall be a string: write it in quotes, since a "
                "version is not a number -- yaml reads 1.10 as 1.1")
        return version

    # Whether this entry applies to 'suite' at all.
    def for_suite(self, suite):
        return self.suites is None or suite in self.suites

    # The architectures this entry asks a vendored suite for, narrowed to
    # 'wanted' -- the union the whole 'vendor:' section (or '--suite')
    # asked for. An entry naming no 'arch:' applies to every one of them.
    def architectures_for(self, wanted):
        if self.architectures is None:
            return list(wanted)
        return [a for a in self.architectures if a in wanted]

# Validates the 'vendor' section and returns it as VendorPackage objects.
def parse(spec):
    entries = spec.get("vendor", [])
    if type(entries) != type([]):
        raise ValueError("'vendor' shall be a list of source packages!")
    return [VendorPackage(e, i + 1) for i, e in enumerate(entries)]

# A source package to exclude from the build-dependency closure -- never
# from an entry named directly under 'vendor:', which is an explicit ask.
# The escape hatch the closure's own dedup against the base chroot cannot
# cover on its own: a build-profile-only dependency (docs, tests) nobody
# wants vendored.
def exclusions(spec):
    excluded = spec.get("vendor-exclude", [])
    if type(excluded) != type([]) or any(type(e) != type("") for e in excluded):
        raise ValueError("'vendor-exclude' shall be a list of source package names!")
    return excluded

# 'distribution: architectures:' -- every architecture a release/project
# supports, beside the one 'distribution: architecture:' (singular) says
# this particular run targets. Without it, the only way to vendor for
# more than that one architecture was to tag some unrelated entry's own
# 'arch:' with the extra name -- a side effect of a per-package field
# never meant to carry a whole run's intent. This is the direct way to
# say it: architectures(), the one place both this and every entry's
# own 'arch:' are folded into the run's actual wanted set, reads it from
# here rather than from any one entry. Merged the same way
# 'vendor-exclude:' is -- additive, deduplicated (docs/merging.md,
# BuildCmd._merge_distro_architectures()) -- unlike every other
# 'distribution:' setting.
def extra_architectures(spec):
    archs = (spec.get("distribution") or {}).get("architectures", [])
    if type(archs) != type([]) or any(type(a) != type("") for a in archs):
        raise ValueError(
            "'distribution: architectures:' shall be a list of architecture names!")
    return archs

# Every suite a 'vendor:' section names: every release 'distribution:
# feeds:' actually configures for an unqualified entry (matching
# entries_for()'s own for_suite(), which an unqualified entry answers
# 'yes' to regardless of which suite is asked), and whatever 'suite:'
# names otherwise -- whether or not it has a configured feed. Never
# distro['release'] alone: a specification naming several releases under
# 'feeds:' (examples/vendor/main.yaml's own bookworm-and-trixie feeds,
# say) has no 'distribution: release:' of its own to prefer one over the
# other, and an unqualified entry there means every one of them, not
# whichever release a merge happened to leave in distro['release'], or
# utils.distribution()'s own generic fallback when nothing set it at
# all. Used both to validate a
# '--suite' name and, by suites() below, as the full set that needs one
# when no '--suite' narrows a run.
def named_suites(entries, distro):
    named = set()
    releases = {feed["release"] for feed in feeds(distro)}
    for entry in entries:
        named.update(entry.suites or releases)
    return sorted(named)

# Which of 'names' has no configured feed to resolve it from.
def unconfigured_suites(names, distro):
    configured = {feed["suite"] for feed in feeds(distro)}
    return sorted(set(names) - configured)

# Every suite a 'vendor:' section asks for, checked against the
# distribution's own configured feeds. A name that is not one of them is
# reported here, at parse time, rather than once a resolve step reaches
# for a feed that is not there. Checks every named suite, not just what a
# '--suite' run narrows to -- see VendorCmd.main() for that scoped check.
def suites(entries, distro):
    named = named_suites(entries, distro)
    unknown = unconfigured_suites(named, distro)
    if len(unknown) > 0:
        raise ValueError(
            "'vendor:' asks for %s, which %s no configured feed -- add it "
            "under 'distribution: feeds:' first"
            % (", ".join(unknown), "has" if len(unknown) == 1 else "have"))
    return named

# Every architecture a 'vendor:' section asks for, across every suite --
# what 'dpkg --add-architecture' would need for the whole run. Per-suite
# scoping (which architectures a given suite actually asks for) is done by
# entries_for()/architectures_for() once resolving that suite. 'extra' is
# extra_architectures()'s own return -- 'distribution: architectures:'
# widening the base set the same way an entry's own 'arch:' already does.
def architectures(entries, distro, extra=()):
    wanted = {distro["architecture"]} | set(extra)
    for entry in entries:
        if entry.architectures is not None:
            wanted.update(entry.architectures)
    return sorted(wanted)

# The entries that apply to one suite.
def entries_for(entries, suite):
    return [e for e in entries if e.for_suite(suite)]

# Where a suite's fetched packages are cached: one flat directory per
# suite, holding every '.deb'/'.dsc'/'.orig.tar.*'/'.debian.tar.*'
# fetch_source()/fetch_binary() ever fetched for it, named by dpkg's own
# convention and nothing else -- no 'pool/', no 'dists/', no signatures.
# Registered in cache.py's CACHES under "vendor". Durable: refetching one
# of these may mean an upstream feed that no longer exists, so nothing
# here is removed except by 'seine cache clear vendor' or an explicit
# eviction (see CacheCmd._evict()'s own VENDOR branch, which already
# expects exactly this flat layout).
#
# What a suite's repository actually looks like, classified into
# main/extra and indexed, is never built here -- see
# deploy_repository()/index() for that.
def repository(suite):
    path = ContainerEngine.cache("vendor", suite)
    os.makedirs(path, exist_ok=True)
    return path

# Where a suite's vendor repository is *delivered*: a plain apt archive
# (pool/, dists/, Release, signatures) under 'deploy/vendor/' by default
# -- ContainerEngine.vendor_root()'s own SEINE_VENDOR_DIR relocates just
# this, independently of the rest of 'deploy/' (a spec's own per-machine
# artifacts, e.g. 'deploy/<release>/', image.py's own builds), since a
# vendor repository is a shared input several machines may want to point
# at the same network mount rather than a build's own local output --
# built by index()
# out of repository(suite)'s flat fetched files and the frozen manifest
# beside them, and nothing else: no cache bookkeeping, no raw fetched
# files loose at top level, just what a plain 'apt' would need to use
# it. This is the one and only place it exists -- unlike an earlier
# version of this split, cache never carries a pool/dists view of its
# own. A build's own 'vendor' task (image.py's own Image._vendor_task())
# builds this before 'rootfs' ever mounts it -- ansible_runner.py no
# longer rebuilds it itself, just-in-time or otherwise, so a
# specification going offline over a suite its own 'vendor:' section
# does not feed still depends on some other 'seine vendor' run having
# left this standing (see docs/specification.md's own
# 'apt-pull-mode: offline').
#
# Also where a person goes looking for a *deliverable*: handed to
# whoever tracks OSS license compliance, archived, shipped -- the reason
# this exists at all rather than serving straight out of cache/, per the
# user's own framing of the ask.
def deploy_repository(suite):
    path = os.path.join(ContainerEngine.vendor_root(), suite)
    os.makedirs(path, exist_ok=True)
    return path

# Whether a release's own vendor repository is already there -- carried
# over from an earlier 'seine build' on this machine, or dropped in from
# another one's 'deploy/vendor/<release>' outright. Trusted outright:
# 'Packages' is only ever written by index() finishing a run, never by
# deploy_repository() making the directory, so its presence is taken to
# mean every '.deb' the manifest names is there and the index describes
# it -- not re-verified against the manifest or reindexed. That is the
# whole point of shipping deploy/ ahead of a build: skipping 'seine
# build's own vendor task finding out again what it already knows (see
# image.py's own Image._vendor_task()).
def is_deployed(release):
    return os.path.isfile(os.path.join(deploy_repository(release), "Packages"))

# The build context name HostBootstrap/TransportBootstrap bind their own
# vendor repository in under, once 'apt-pull-mode: offline' has them
# reading from it: 'podman build' cannot bind-mount an arbitrary host
# path into a RUN instruction the way 'container run -v' can, so it goes
# in as a named '--build-context' instead, read back out with 'RUN
# --mount=type=bind,from=<name>'.
BUILD_CONTEXT = "vendor-repo"

# What a Dockerfile-baked apt-get needs to read 'release's vendor
# repository at build time: raises up front, in plain language, rather
# than letting an empty mount fail obscurely inside the 'apt-get update'
# that would otherwise be the first thing to notice.
def offline_build_context(release):
    if not is_deployed(release):
        raise ValueError(
            "apt-pull-mode: offline needs a vendor repository for '%s' "
            "-- run 'seine vendor' first" % release)
    return deploy_repository(release)

# The digest HostBootstrap/TransportBootstrap fold into their own
# Dockerfile text when going offline -- 'podman build' caches an image
# by that text alone, never by what a bind-mounted directory actually
# holds (see BuilderImage._sources()'s own comment on the same trap), so
# without this a vendor refresh that changes what apt would install
# would never invalidate the cached image. None when 'apt-pull-mode' is
# not offline, so a caller can pass this through unconditionally.
def offline_dockerfile_digest(spec, distro):
    if distro.get("apt-pull-mode") != "offline":
        return None
    release = distro["release"]
    return manifest_digest(distro, parse(spec), exclusions(spec), release)

# The distribution a suite's own container session bootstraps and reads
# apt sources from: the same source/architecture/uri as the specification
# as a whole, but feeds narrowed to this suite's own 'release:' (see
# utils.feeds()'s own comment) -- named for this suite rather than the
# release being built, so two suites' own containers/chroots/results
# never collide.
#
# Every feed of the suite's OWN release stays visible, not narrowed to
# its exact pocket alone: a build-dependency closure needs the same
# consistent picture of that release's own archive a real build's chroot
# already gets (Builder._sources() in packages.py holds back nothing
# either), or a package whose runtime library was bumped by a security
# update, while its own '-dev' headers stay pinned to the base pocket's
# exact version by an '=' build-dep, resolves to nothing apt can install
# -- found by 'seine vendor' actually failing on 'git' this way,
# vendoring nothing but the base 'trixie' pocket.
#
# A DIFFERENT release configured elsewhere in the same 'distribution:
# feeds:' -- 'bookworm', say, alongside a 'trixie' release, there only
# for a handful of entries explicitly asking 'suite: bookworm' -- stays
# out: resolving trixie's own packages has no business quietly picking a
# build-dep out of bookworm just because both happen to be configured in
# one specification. See feeds_for_suite() below.
def _suite_distro(distro, suite):
    if suite not in {feed["suite"] for feed in feeds(distro)}:
        raise ValueError("no feed for suite '%s'!" % suite)
    return dict(distro, release=suite, feeds=feeds_for_suite(distro, suite))

# The feeds a suite's own resolver/fetch session may see: every feed
# declaring the same 'release:' as the one configured for 'suite' itself
# (utils.feeds()'s own comment -- unset, a feed's release is its own
# suite), never a wholly different release also configured under
# 'distribution: feeds:' for some other suite's sake. Filters the raw
# 'feeds:' shape, not feeds()'s parsed-and-resolved return -- the same
# reason _suite_distro() leaves 'feeds' raw (see
# SuiteDistroKeepsEveryConfiguredFeed's own comment): apt_sources() and a
# resolver's dockerfile() both call feeds() themselves on whatever they
# are handed.
def feeds_for_suite(distro, suite):
    entries = distro.get("feeds")
    if entries is None:
        entries = [{"suite": distro["release"]}]
    matching = next((e for e in entries if e["suite"] == suite), None)
    if matching is None:
        return []
    release = matching.get("release", matching["suite"])
    return [e for e in entries if e.get("release", e["suite"]) == release]

# ---------------------------------------------------------------------
# The frozen manifest: what a resolve step decided, kept beside the
# repository it fills so an ordinary re-run does not silently drift to
# whatever apt would resolve today. '--refresh' is what asks for a new
# one.
# ---------------------------------------------------------------------

MANIFEST = ".vendor-manifest.json"

# Bumped whenever the 'graph' field's own shape (edges/reverse/pruned)
# changes -- a reader can tell a manifest's graph apart from one written
# by an older 'seine vendor' without guessing from its shape.
GRAPH_VERSION = 1

def _manifest_path(suite):
    return os.path.join(repository(suite), MANIFEST)

def load_manifest(suite):
    try:
        with open(_manifest_path(suite)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

# Rebuilds a graph's 'reverse' index from its own 'edges' -- the same
# construction RESOLVE_SCRIPT does inside the resolver container, needed
# again on the host once VendorCmd._merge_refresh_graph() combines edges
# from two different runs into one list.
def _reverse_of(edges):
    reverse = {}
    for edge in edges:
        reverse.setdefault(edge["to"], []).append(
            {"parent": edge["from"], "via": edge["via"], "arch": edge["arch"],
             "field": edge["field"], "depth": edge["depth"]})
    for rows in reverse.values():
        rows.sort(key=lambda r: (r["depth"], r["parent"]))
    return reverse

def save_manifest(suite, manifest):
    path = _manifest_path(suite)
    temporary = "%s.new" % path
    with open(temporary, "w") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    os.replace(temporary, path)

# ---------------------------------------------------------------------
# The committed lock file: 'vendor:' as a dict (suite -> {digest,
# sources}) rather than a list of asks -- see seine/build.py's own
# '_merge_vendor()', which is what folds a '<spec>.lock.yaml' loaded by
# 'BuildCmd.load_all()' into 'spec["_vendor_lock"]' rather than
# 'spec["vendor"]' itself. Content-wise the same shape as the cache
# manifest above, since it exists to freeze exactly what that already
# freezes -- just checked into git instead of living under
# ContainerEngine.cache().
# ---------------------------------------------------------------------

# One suite's own frozen entry out of a loaded lock file, or None if
# either no lock was loaded at all or it says nothing about this suite.
def lock_manifest(spec, suite):
    return (spec.get("_vendor_lock") or {}).get(suite)

# The external-only view of a suite's resolved sources a committed lock
# actually needs: version/binaries/files (plus, once the caller adds
# it, their sha256). The cache manifest (save_manifest()) keeps 'fresh'
# exactly as resolve() produced it -- this narrowing is for the lock
# alone, not a change to what seine's own working data carries:
#
# - 'direct' is redundant the moment it would be written anywhere:
#   index() (see its own comment) recomputes exactly this, fresh, from
#   'entries' and the gocode BFS every time it runs, so nothing here is
#   information a serialized copy would preserve that a reader could
#   not already work out for itself.
# - 'build_dep_bins' is real, load-bearing data for that same BFS --
#   dropping it does cost something, an offline-only checkout of the
#   lock (no cache, no resolver) cannot run gocode promotion and falls
#   back to classifying by direct-ask membership alone. Left out anyway:
#   it is internal bookkeeping about how seine classifies what it
#   fetched, not an external fact about a dependency, and a committed
#   lock's job is only the latter -- what to fetch and how to know it
#   has not changed. The cache manifest still carries it in full, so an
#   ordinary '--refresh' (which writes both) keeps promoting correctly
#   right after.
def _lock_sources(sources):
    return {name: {k: v for k, v in entry.items()
                   if k not in ("direct", "build_dep_bins")}
           for name, entry in sources.items()}

# Every file a fetched source's own entry names, hashed off what is
# actually sitting in the suite's cache directory -- the lock's own
# no-deviation guarantee: a version can legally be re-uploaded with
# different bytes, which pinning the version string alone would not
# catch. Missing files (nothing fetched this run touched, or a
# check-mode resolve that never fetched at all) are silently left out
# rather than raising -- a lock written before a suite's files ever
# landed on disk is still useful for every field but this one.
def _file_hashes(suite, entry):
    where = repository(suite)
    hashes = {}
    for fname in entry.get("files", []):
        try:
            with open(os.path.join(where, fname), "rb") as f:
                hashes[fname] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            pass
    return hashes

# A source's own binaries, hashed the same way -- each looked up via
# _binary_file_path(), the same lookup _binary_has_gocode() itself
# uses: there is no 'files' list for a binary the way a source has one,
# only its name/arch/version, so which candidate filename is actually
# on disk has to be found rather than assumed.
def _binary_hashes(suite, entry):
    where = repository(suite)
    hashes = {}
    for binpkg, per_arch in entry.get("binaries", {}).items():
        for arch, version in per_arch.items():
            path = _binary_file_path(where, binpkg, arch, version)
            if path is None:
                continue
            with open(path, "rb") as f:
                hashes.setdefault(binpkg, {})[arch] = \
                    hashlib.sha256(f.read()).hexdigest()
    return hashes

# A binary's own filename is a pure function of its name, arch and
# version -- unlike a source's, which is only known once its stanza has
# been read (see RESOLVE_SCRIPT's own _source_files()) -- so this alone
# is enough to tell whether it is already fetched. Unlike a source's own
# .dsc/.orig.tar.* (Debian Policy ch-binary.html#uniqueness-of-version-
# numbers: an archive filename never carries an epoch), 'apt-get
# download' keeps a binary's epoch in the filename it writes, with ':'
# escaped as '%3a' -- '1:0.10.2-1' downloads as '..._1%3a0.10.2-1...',
# never '..._0.10.2-1...'. Stripping it here (as an earlier version of
# this did) made every epoch-carrying binary miss this check on every
# run: the file it looked for was never the file apt actually wrote.
# Matches CacheCmd._evict()'s own encoding (seine/cache.py).
def _binary_filename(binpkg, arch, version):
    return "%s_%s_%s.deb" % (binpkg, version.replace(":", "%3a"), arch)

# The name an earlier version of this module expected -- epoch stripped
# outright, on the (Debian Policy ch-binary.html#uniqueness-of-version-
# numbers) assumption that a binary's own filename never carries one the
# way a source's .dsc/.orig.tar.* don't. Kept as a second candidate, never
# the first: real 'apt-get download' runs against trixie name these with
# the epoch %3a-escaped instead (see _binary_filename() above), so a file
# actually on disk almost always matches that one -- but nothing here
# rules out some apt version, some other archive, or a file fetched
# before this fix existed writing the stripped name instead, and treating
# that as a cache miss would refetch a file already sitting there for no
# reason.
def _binary_filename_legacy(binpkg, arch, version):
    return "%s_%s_%s.deb" % (binpkg, version.split(":", 1)[-1], arch)

# Whichever of the four candidates (both epoch spellings, the binary's
# own architecture and 'all' -- see fetch.py's own _binary_already_
# fetched()) is actually sitting in 'where', or None. The one place
# that search is spelled out; _binary_has_gocode()/_binary_hashes()/the
# snapshot lookup in _enrich_for_lock() all just want the path, not the
# search.
def _binary_file_path(where, binpkg, arch, version):
    for candidate in (arch, "all"):
        for name in (_binary_filename(binpkg, candidate, version),
                     _binary_filename_legacy(binpkg, candidate, version)):
            path = os.path.join(where, name)
            if os.path.isfile(path):
                return path
    return None

def _local_sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()

# A source's own snapshot.debian.org lookup, cached on the exact
# artifact key fetch_source() already touches (the same one
# has_gocode's own metadata rides on) -- 'suite:vendor's own scale is
# thousands of sources per refresh, most of them unchanged from the
# last one, and asking a shared, small public mirror about every one
# of them again on every '--refresh' is both slow and a poor way to
# treat it. Keyed by version like every other artifact key here, so a
# version bump is a fresh key and an automatic cache miss -- nothing
# to invalidate by hand.
#
# Only ever grown, never pruned by this function: 'cached' starts as
# whatever an earlier refresh already found, gains whatever this one
# newly finds, and is written back whole. Holds sha1 alone, not a url
# -- see _enrich_for_lock()'s own comment on why the lock itself never
# stores one either -- so a cache entry that no longer matches the
# file's own current local sha1 (should not happen for an unchanged
# version, but costs nothing to guard, and is a plain dict comparison,
# no network) is treated as a miss rather than blindly trusted.
#
# A *miss* is never cached, unlike a match: snapshot.debian.org indexes
# a freshly uploaded version with some lag, and caching "not found"
# forever would mean a later refresh, run once the mirror has caught
# up, never noticing -- the whole point of asking again.
def _cached_local_matches(key, local_hashes):
    cached = dict((Index().get(VENDOR, key) or {}).get("snapshot_files") or {})
    missing = [f for f, h in local_hashes.items() if cached.get(f) != h]
    return cached, missing

def _save_source_snapshot_cache(key, cached):
    if cached:
        try:
            Index().patch(VENDOR, key, {"snapshot_files": cached})
        except Exception:
            pass

# Writes the committed lock file at 'path' -- 'suites' is the whole
# document's worth (every suite this spec's 'vendor:' section knows
# about, not just the ones a scoped '--refresh'/'--suite' run touched),
# so a run narrowed to one suite never drops what an earlier run froze
# for another.
#
# A source's own file folds its sha256 and its own snapshot.debian.org
# sha1 together the same way a binary's own does (see
# _compress_binaries()'s own comment below): a bare sha256 string, or
# 'sha256:sha1' when a snapshot was found. 'file_hashes'/'snapshot' drop
# out entirely, and 'files' becomes a {filename: value} mapping instead
# of a bare list plus two side dicts each repeating every filename a
# second and third time -- no version to diverge on here (a source's own
# files are inherently pinned to the one version its entry already
# names), so this never needs a binary's own small mapping either.
def _compress_files(sources):
    compressed = {}
    for name, entry in sources.items():
        entry = dict(entry)
        files = entry.pop("files", None)
        hashes = entry.pop("file_hashes", None)
        snaps = entry.pop("snapshot", None)
        if files:
            merged = {}
            for fname in files:
                h = (hashes or {}).get(fname)
                s = (snaps or {}).get(fname)
                merged[fname] = "%s:%s" % (h, s) if s else h
            entry["files"] = merged
        compressed[name] = entry
    return compressed

def _expand_files(sources):
    expanded = {}
    for name, entry in sources.items():
        entry = dict(entry)
        files = entry.get("files")
        if files:
            names, hashes, snaps = [], {}, {}
            for fname, value in files.items():
                names.append(fname)
                if value is None:
                    hashes[fname] = None
                else:
                    h, sep, s = value.partition(":")
                    hashes[fname] = h
                    if sep:
                        snaps[fname] = s
            entry["files"] = names
            entry["file_hashes"] = hashes
            if snaps:
                entry["snapshot"] = snaps
        expanded[name] = entry
    return expanded

# A binary's own entry folds its hash, its version and its own
# snapshot.debian.org sha1 together into one value, dropping the
# separate 'binary_hashes'/'binary_snapshot' dicts entirely: a bare
# 'sha256' string when the version is identical to its own source's and
# no snapshot was found, 'sha256:sha1' the same way when one was (a
# sha256 is 64 hex chars and a sha1 is 40 -- never ambiguous), or,
# only for the genuine divergence (a binNMU, '+b1' say) or the rarer
# case of that also lacking a snapshot hit, a small mapping instead.
# Kept per architecture, not per binary package: a binNMU can in
# principle complete on one architecture before another, and a snapshot
# hit is a fact about one exact .deb, not the package as a whole.
#
# Both this and _compress_files()/_expand_files() above apply at the
# lock's own read/write boundary alone (save_lock()/load_lock()) so
# nothing else in this module (fetch_tasks(), index(), _binary_hashes(),
# _file_hashes()...) ever has to know an entry's own 'files'/'binaries'
# might be either shape -- VendorCmd.main() calls both expansions a
# second time, for the *other* way a lock's data reaches here:
# BuildCmd's own generic YAML/jinja loader (spec["_vendor_lock"]), which
# never passes through load_lock() at all.
def _compress_binaries(sources):
    compressed = {}
    for name, entry in sources.items():
        entry = dict(entry)
        binaries = entry.pop("binaries", None)
        hashes = entry.pop("binary_hashes", None)
        snaps = entry.pop("binary_snapshot", None)
        if binaries:
            merged = {}
            for binpkg, per_arch in binaries.items():
                per_hash = (hashes or {}).get(binpkg, {})
                per_snap = (snaps or {}).get(binpkg, {})
                merged[binpkg] = {}
                for arch, version in per_arch.items():
                    h = per_hash.get(arch)
                    s = per_snap.get(arch)
                    if version == entry["version"]:
                        merged[binpkg][arch] = "%s:%s" % (h, s) if s else h
                    else:
                        value = {"hash": h, "version": version}
                        if s:
                            value["snapshot"] = s
                        merged[binpkg][arch] = value
            entry["binaries"] = merged
        compressed[name] = entry
    return compressed

def _expand_binaries(sources):
    expanded = {}
    for name, entry in sources.items():
        entry = dict(entry)
        binaries = entry.get("binaries")
        if binaries:
            plain, hashes, snaps = {}, {}, {}
            for binpkg, per_arch in binaries.items():
                plain[binpkg] = {}
                hashes[binpkg] = {}
                for arch, value in per_arch.items():
                    if isinstance(value, dict):
                        plain[binpkg][arch] = value["version"]
                        hashes[binpkg][arch] = value.get("hash")
                        if value.get("snapshot"):
                            snaps.setdefault(binpkg, {})[arch] = value["snapshot"]
                    else:
                        plain[binpkg][arch] = entry["version"]
                        # A binary missing its own local hash (never
                        # fetched, or a '--check' resolve with nothing
                        # on disk) writes bare 'None' through here --
                        # only an actual string is ever ':'-joined with
                        # a snapshot sha1.
                        if value is None:
                            hashes[binpkg][arch] = None
                        else:
                            h, sep, s = value.partition(":")
                            hashes[binpkg][arch] = h
                            if sep:
                                snaps.setdefault(binpkg, {})[arch] = s
            entry["binaries"] = plain
            entry["binary_hashes"] = hashes
            if snaps:
                entry["binary_snapshot"] = snaps
        expanded[name] = entry
    return expanded

def save_lock(path, suites):
    temporary = "%s.new" % path
    compact = {suite: dict(doc, sources=_compress_binaries(
                  _compress_files(doc.get("sources", {}))))
              for suite, doc in suites.items()}
    with open(temporary, "w") as f:
        yaml.safe_dump({"vendor": compact}, f, sort_keys=True,
                       default_flow_style=False)
    os.replace(temporary, path)

# The lock file's own 'vendor:' dict, read directly rather than through
# 'BuildCmd' -- for whoever wants to inspect a lock on its own (tests,
# 'seine vendor-why', a future 'seine vendor --list-lock') without
# templating a whole specification around it. Always expanded (see
# _expand_binaries()'s own comment) -- a caller of this never sees the
# lock's own on-disk compression trick.
def load_lock(path):
    with open(path) as f:
        data = (yaml.safe_load(f) or {}).get("vendor", {})
    return {suite: dict(doc, sources=_expand_binaries(
               _expand_files(doc.get("sources", {}))))
           for suite, doc in data.items()}

# Everything about a suite's 'vendor:' section that would change what a
# resolve decides, folded into one digest and kept beside the frozen
# manifest -- the same idea as packages.py's own build stamp
# (Builder.stamp()). Without this, editing a spec's 'vendor:' entries (or
# 'vendor-exclude:', build-profiles/options, or a feed's own uri/suite)
# and rerunning against a suite that already has a manifest silently kept
# resolving nothing: only a missing manifest or an explicit '--refresh'
# ever triggered a resolve, never the spec having actually changed. A
# suite's own feed config is in here too, not the release's alone: the
# resolver reads every feed of the suite's own release family (see
# _suite_distro()'s own comment / feeds_for_suite()), so any of those
# moving can change what it resolves -- a different release's feed,
# configured in the same specification for some other suite's sake,
# cannot, and is left out here for exactly that reason: editing it would
# otherwise force a suite that never saw it to re-resolve for nothing.
def manifest_digest(distro, entries, exclude, suite, extra_archs=()):
    relevant = sorted(
        (e.name, e.suites, e.architectures, e.version)
        for e in entries_for(entries, suite))
    payload = {
        "entries": relevant,
        "exclude": sorted(exclude),
        "extra-architectures": sorted(extra_archs),
        "build-profiles": distro.get("build-profiles", []),
        "build-options": distro.get("build-options", []),
        "feeds": feeds_for_suite(distro, suite),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
