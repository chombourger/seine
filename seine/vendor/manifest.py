# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The 'vendor:' spec section (VendorPackage and friends), the on-disk
# repository/lock layout, and the manifest/lock load/save -- not how it
# gets built (resolve.py) or fetched (fetch.py).

import hashlib
import json
import os
import re
import yaml

from seine.cache_index import VENDOR, Index
from seine.container import ContainerEngine
from seine.utils import feeds


# One 'vendor:' entry: a source package plus the conditions
# ('suite:'/'arch:'/'version:') deciding when it applies. Keyed by source
# name, like 'packages:', since binaries derive from a source.
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

    # 'suite:'/'arch:' take one name or a list; unset means every one of
    # them, i.e. the entry applies everywhere.
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

    # Architectures this entry wants, narrowed to 'wanted'. No 'arch:'
    # means every one of them.
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

# Source packages to drop from the build-dependency closure only -- never
# an entry named directly under 'vendor:'. Escape hatch for a
# build-profile-only dep (docs, tests) nobody wants vendored.
def exclusions(spec):
    excluded = spec.get("vendor-exclude", [])
    if type(excluded) != type([]) or any(type(e) != type("") for e in excluded):
        raise ValueError("'vendor-exclude' shall be a list of source package names!")
    return excluded

# 'distribution: architectures:': extra architectures a run vendors for,
# beside 'distribution: architecture:' (singular). Merged additively with
# every entry's own 'arch:' in architectures() below.
def extra_architectures(spec):
    archs = (spec.get("distribution") or {}).get("architectures", [])
    if type(archs) != type([]) or any(type(a) != type("") for a in archs):
        raise ValueError(
            "'distribution: architectures:' shall be a list of architecture names!")
    return archs

# Every suite a 'vendor:' section names: every configured release for an
# unqualified entry, plus whatever 'suite:' names explicitly (configured
# or not). Used to validate '--suite' and as the full set a run needs
# when unscoped.
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

# Every suite 'vendor:' asks for, validated against configured feeds
# here at parse time. Checks every named suite, not just a '--suite'-
# scoped run (see VendorCmd.main() for that).
def suites(entries, distro):
    named = named_suites(entries, distro)
    unknown = unconfigured_suites(named, distro)
    if len(unknown) > 0:
        raise ValueError(
            "'vendor:' asks for %s, which %s no configured feed -- add it "
            "under 'distribution: feeds:' first"
            % (", ".join(unknown), "has" if len(unknown) == 1 else "have"))
    return named

# Every architecture 'vendor:' asks for across every suite -- what 'dpkg
# --add-architecture' needs for the whole run. Per-suite scoping happens
# later, in entries_for()/architectures_for().
def architectures(entries, distro, extra=()):
    wanted = {distro["architecture"]} | set(extra)
    for entry in entries:
        if entry.architectures is not None:
            wanted.update(entry.architectures)
    return sorted(wanted)

# The entries that apply to one suite.
def entries_for(entries, suite):
    return [e for e in entries if e.for_suite(suite)]

# Where a suite's fetched packages are cached: one flat directory, named
# by dpkg's own convention, no pool/dists layout. Durable -- only 'seine
# cache clear vendor' removes anything. See deploy_repository()/index()
# for the classified, indexed view.
def repository(suite):
    path = ContainerEngine.cache("vendor", suite)
    os.makedirs(path, exist_ok=True)
    return path

# Where a suite's vendor repository is *delivered*: a real apt archive
# (pool/, dists/, Release) under 'deploy/vendor/', relocatable via
# SEINE_VENDOR_DIR since it's a shared input, not a per-machine build
# artifact. Built by index() out of repository(suite)'s flat files --
# what an offline build reads from and what license compliance archives.
def deploy_repository(suite):
    path = os.path.join(ContainerEngine.vendor_root(), suite)
    os.makedirs(path, exist_ok=True)
    return path

# Whether a release's vendor repository already exists -- from an
# earlier build here, or copied in from elsewhere. Trusted outright:
# 'Packages' is only ever written once index() finishes, never
# re-verified against the manifest or reindexed.
def is_deployed(release):
    return os.path.isfile(os.path.join(deploy_repository(release), "Packages"))

# Named build-context HostBootstrap/TransportBootstrap bind the vendor
# repo under for 'apt-pull-mode: offline' -- 'podman build' can't
# bind-mount an arbitrary host path into a RUN like 'container run -v' can.
BUILD_CONTEXT = "vendor-repo"

# What a Dockerfile-baked apt-get needs to read the vendor repo -- raises
# up front rather than letting an empty mount fail inside 'apt-get update'.
def offline_build_context(release):
    if not is_deployed(release):
        raise ValueError(
            "apt-pull-mode: offline needs a vendor repository for '%s' "
            "-- run 'seine vendor' first" % release)
    return deploy_repository(release)

# Digest folded into the offline Dockerfile text so 'podman build's own
# text-only cache invalidates when a vendor refresh changes what apt
# would install. None when 'apt-pull-mode' isn't offline.
def offline_dockerfile_digest(spec, distro):
    if distro.get("apt-pull-mode") != "offline":
        return None
    release = distro["release"]
    return manifest_digest(distro, parse(spec), exclusions(spec), release)

# The distribution a suite's own container bootstraps/resolves against:
# same source/arch/uri as the spec, feeds narrowed to this suite's own
# release (see feeds_for_suite()) so build-deps see one consistent
# archive view, never a different release configured for another suite.
def _suite_distro(distro, suite):
    if suite not in {feed["suite"] for feed in feeds(distro)}:
        raise ValueError("no feed for suite '%s'!" % suite)
    return dict(distro, release=suite, feeds=feeds_for_suite(distro, suite))

# Feeds a suite may see: every feed sharing 'suite's own release, never a
# different release configured elsewhere in 'distribution: feeds:'.
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
# repository so an ordinary rerun doesn't silently drift. '--refresh'
# asks for a new one.
# ---------------------------------------------------------------------

MANIFEST = ".vendor-manifest.json"

# Bumped when the 'graph' field's shape (edges/reverse/pruned) changes,
# so a reader can tell old graphs apart without guessing.
GRAPH_VERSION = 1

def _manifest_path(suite):
    return os.path.join(repository(suite), MANIFEST)

def load_manifest(suite):
    try:
        with open(_manifest_path(suite)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

# Rebuilds the 'reverse' index from 'edges' -- same construction
# RESOLVE_SCRIPT does in-container, needed again after merging two runs.
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
# The committed lock file: 'vendor:' as {suite: {digest, sources}} rather
# than a list -- see build.py's '_merge_vendor()', which folds a loaded
# '<spec>.lock.yaml' into spec['_vendor_lock']. Same shape as the cache
# manifest, just checked into git.
# ---------------------------------------------------------------------

# One suite's own frozen entry out of a loaded lock file, or None if
# either no lock was loaded at all or it says nothing about this suite.
def lock_manifest(spec, suite):
    return (spec.get("_vendor_lock") or {}).get(suite)

# The external view of a suite's sources a lock needs: version/binaries/
# files plus hashes -- drops 'direct' (index() recomputes it fresh every
# run) and 'build_dep_bins' (internal bookkeeping; the cache manifest
# still keeps it so gocode promotion works right after a refresh).
def _lock_sources(sources):
    return {name: {k: v for k, v in entry.items()
                   if k not in ("direct", "build_dep_bins")}
           for name, entry in sources.items()}

# Hashes every file a source's entry names, off what's actually on disk
# -- the lock's no-deviation guarantee against a version being
# re-uploaded with different bytes. Missing files are skipped, not raised.
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

# Same, for a source's binaries -- located via _binary_file_path() since
# a binary has no 'files' list, only name/arch/version to search by.
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

# A binary's filename is a pure function of name/arch/version. 'apt-get
# download' keeps the epoch in it, ':' escaped as '%3a' -- stripping it
# (an earlier bug) made every epoch-carrying binary miss this check.
def _binary_filename(binpkg, arch, version):
    return "%s_%s_%s.deb" % (binpkg, version.replace(":", "%3a"), arch)

# The name an earlier version of this module wrote, epoch stripped.
# Kept as a fallback candidate for files fetched before the fix above,
# never tried first.
def _binary_filename_legacy(binpkg, arch, version):
    return "%s_%s_%s.deb" % (binpkg, version.split(":", 1)[-1], arch)

# Whichever of the four filename candidates is actually on disk, or
# None -- the one place this search is spelled out.
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

# A source's snapshot.debian.org lookup, cached per artifact key --
# thousands of sources per refresh, most unchanged, so asking the small
# public mirror again every time is slow and unfriendly. A miss is
# never cached (the mirror indexes with lag), only a match is.
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

# Packs a source's files into the lock's compact {filename: 'sha256' or
# 'sha256:sha1'} shape, dropping the separate file_hashes/snapshot dicts.
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

# Reverses _compress_files() back into files/file_hashes/snapshot.
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

# Packs a binary's per-arch entry into 'sha256[:sha1]', or a small
# {hash, version[, snapshot]} dict when its version diverges from the
# source's own (a binNMU) -- kept per arch since a binNMU can finish on
# one before another.
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

# Reverses _compress_binaries() back into binaries/binary_hashes/
# binary_snapshot.
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
                        # Bare 'None' for a binary never fetched (no
                        # local hash to store); only a real hash string
                        # is ever ':'-joined with a snapshot sha1.
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

# Writes the lock at 'path' with every suite the doc knows, so a run
# scoped to one suite never drops what an earlier run froze for another.
def save_lock(path, suites):
    temporary = "%s.new" % path
    compact = {suite: dict(doc, sources=_compress_binaries(
                  _compress_files(doc.get("sources", {}))))
              for suite, doc in suites.items()}
    with open(temporary, "w") as f:
        yaml.safe_dump({"vendor": compact}, f, sort_keys=True,
                       default_flow_style=False)
    os.replace(temporary, path)

# Reads a lock file directly, without going through BuildCmd's spec
# loader -- always expanded (never the on-disk compact shape).
def load_lock(path):
    with open(path) as f:
        data = (yaml.safe_load(f) or {}).get("vendor", {})
    return {suite: dict(doc, sources=_expand_binaries(
               _expand_files(doc.get("sources", {}))))
           for suite, doc in data.items()}

# Digest of everything in a suite's 'vendor:' section that would change
# what a resolve decides (entries, excludes, profiles/options, feeds) --
# kept beside the manifest so an edited spec re-resolves instead of
# silently staying stale. Only this suite's own feed family, never a
# different release configured for another suite.
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
