# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import collections
import getopt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from seine.bootstrap    import Bootstrap, HostBootstrap
from seine.cache_index  import VENDOR, Index, say
from seine.cmd          import Cmd
from seine.sbuild       import BuilderImage, SbuildChroot
from seine.tasks        import Task
from seine.utils        import ContainerEngine, feeds, apt_sources
from seine.utils        import locked
from seine.utils        import PRIVILEGED_RUN_OPTIONS
from seine import settings
from seine import signing
from seine import tasks as task_runner

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
# (pool/, dists/, Release, signatures) under 'deploy/', the way
# image.py's own builds land at 'deploy/<release>/' -- built by index()
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
    path = os.path.join(ContainerEngine.deploy_root(), "vendor", suite)
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

# The resolver image: a slim container holding the tools needed to resolve
# a package's closure using libapt-pkg. Independently bootstrapped per
# suite so its dpkg status exactly matches the suite's own archive.
class VendorResolverImage(Bootstrap):
    kind = "resolver"

    def create(self, suiteBootstrap):
        return self.build(self.dockerfile(suiteBootstrap), base=suiteBootstrap.name)

    def dockerfile(self, suiteBootstrap):
        return VENDOR_RESOLVER_IMAGE_SCRIPT.format(
            suiteBootstrap.name,
            self.distro["source"],
            self.distro["release"],
            self._sources(),
            self.distro["release"])

    def _sources(self):
        sources = apt_sources(self.distro, sources=True)
        return " && ".join(
            "echo '%s' >> /etc/apt/sources.list" % s for s in sources)

    def defaultName(self):
        return os.path.join("resolver", self.distro["source"], self.distro["release"])

    def exec(self, args, architecture=None, volumes=None, workdir=None,
              environment=None, check=True, tty=False):
        cmd = ["container", "run", "--rm"] + PRIVILEGED_RUN_OPTIONS
        if tty:
            cmd += ["-t"]
        if architecture is not None:
            cmd += ["-v", "%s:/root/.cache/sbuild" %
                    ContainerEngine.chroots(self.distro["release"], architecture)]
        for host, container in (volumes or []):
            cmd += ["-v", "%s:%s" % (host, container)]
        for name, value in (environment or {}).items():
            cmd += ["-e", "%s=%s" % (name, value)]
        if workdir is not None:
            cmd += ["-w", workdir]
        return ContainerEngine.run(cmd + [self.name] + args, check=check)

    def output(self, args, architecture=None, volumes=None, workdir=None,
                environment=None):
        cmd = ["container", "run", "--rm"] + PRIVILEGED_RUN_OPTIONS
        if architecture is not None:
            cmd += ["-v", "%s:/root/.cache/sbuild" %
                    ContainerEngine.chroots(self.distro["release"], architecture)]
        for host, container in (volumes or []):
            cmd += ["-v", "%s:%s" % (host, container)]
        for name, value in (environment or {}).items():
            cmd += ["-e", "%s=%s" % (name, value)]
        if workdir is not None:
            cmd += ["-w", workdir]
        return ContainerEngine.check_output(cmd + [self.name] + args)

VENDOR_RESOLVER_IMAGE_SCRIPT = """
FROM {0}
RUN --mount=type=cache,target=/var/cache/apt/archives,id={4},sharing=locked \\
     apt-get update -qqy &&                       \\
     apt-get install -qqy --no-install-recommends \\
         python3-apt apt-utils zstd
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources \\
           /etc/apt/sources.list.d/*.list && \\
    {3} && \\
    apt-get update -qqy
"""

# ---------------------------------------------------------------------
# Resolving: turning a suite's vendor entries into frozen source/binary
# versions, with their full build-dependency closure. Runs inside
# VendorResolverImage, its own container bootstrapped per suite.
# ---------------------------------------------------------------------

# Where the resolve script's request/response files are bind-mounted.
RESOLVE_MOUNT = "/vendor"
REQUEST_FILE = "request.json"
RESPONSE_FILE = "response.json"

# Run inside the suite's builder container as 'python3 /vendor/resolve.py'.
# Reads RESOLVE_MOUNT/REQUEST_FILE, writes RESOLVE_MOUNT/RESPONSE_FILE, and
# never raises past its own 'main()' -- an apt failure is reported in the
# response rather than as a podman exit code, so the host can say which
# package it was about, not just that 'python3' failed.
#
# Foreign architectures are asked of every apt call with
# '-o APT::Architectures::=' rather than a persisted 'dpkg
# --add-architecture': each of BuilderImage.exec()'s invocations is a
# throwaway container, and only what is bind-mounted (the suite's apt
# lists, seeded by the first 'apt-get update' this script runs) survives
# between them.
RESOLVE_SCRIPT = r'''
import apt
import apt_pkg
import collections, json, os, subprocess, traceback

MOUNT = "/vendor"

# The exact files a source's own stanza says belong to it -- 'Files:'
# (md5) and 'Checksums-Sha256:' name the same set, so either alone is
# enough; read both and dedupe rather than assume one is always there.
# This is what lets a fetch be skipped by name on the host, before a
# container is even spawned for it, instead of only ever finding out
# once apt itself is asked and shrugs.
def _source_files(record):
    section = apt_pkg.TagSection(record)
    files = set()
    for key in ("Checksums-Sha256", "Files"):
        for line in section.get(key, "").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                files.add(parts[2])
    return sorted(files)

def main():
    with open(os.path.join(MOUNT, "request.json")) as f:
        request = json.load(f)
    
    archs = request["archs"]
    exclude = set(request["exclude"])
    base = {arch: set(names) for arch, names in request["base_chroot"].items()}

    # Ensure apt lists are up to date, especially for deb-src
    print("Running apt-get update...")
    subprocess.run(["apt-get", "update"], check=True)

    # Initialize system and caches
    try:
        apt_pkg.init_config()
        apt_pkg.init_system()
    except Exception as e:
        with open(os.path.join(MOUNT, "response.json"), "w") as f:
            json.dump({"ok": False, "error": "init_system failure: %s" % e}, f)
        return

    try:
        bin_cache = apt.Cache()
        src_records = apt_pkg.SourceRecords()
    except Exception as e:
        with open(os.path.join(MOUNT, "response.json"), "w") as f:
            json.dump({"ok": False, "error": "Cache failure: %s\n%s" % (e, traceback.format_exc())}, f)
        return

    resolved = {}
    queue = collections.deque(
        (s["name"], s["version"], True) for s in request["sources"])
    bin_queue = collections.deque()
    seen = {}  # name -> direct (True=main, False=extra)
    seen_bins = set()
    queued = {s: d for s, _, d in queue}
    queued_bins = set()
    warnings = []

    # Provenance for the dependency graph (see vendor-graph/vendor-why):
    # 'depths' is a source's own BFS depth from the nearest root, assigned
    # once when it is first queued -- roots start at 0. 'edges' records
    # one row per (source, Build-Depends binary) survivor of pruning below,
    # 'to' filled in once every source is resolved (a binary's owning
    # source is only known once its own stanza has been read, which may
    # happen after the edge naming it). 'bin_origin_depth' remembers, for
    # a build-dep binary, the depth its first-discovering source would
    # hand to whatever source ends up owning it -- bin_queue processing
    # (below) does not otherwise know which source asked for it.
    depths = {name: 0 for name, _, _ in queue}
    edges = []
    pruned_base = []
    pruned_excluded = []
    bin_origin_depth = {}

    while queue or bin_queue:
        if queue:
            name, constraint, direct = queue.popleft()
            queued.pop(name, None)
            if name in seen:
                if seen[name] is False and direct is True:
                    seen[name] = True
                    if name in resolved:
                        resolved[name]["direct"] = True
                continue
            seen[name] = direct

            print("resolving %s%s..." % (name, "" if direct else " (build-dep)"))

            # Find matches in SourceRecords via lookup (SourceRecords is not iterable)
            # lookup(name) also finds sources that provide a binary named `name`
            # (e.g. lookup("ecj") finds eclipse-jdt-core), so filter to
            # package == name to get the source itself.
            candidates = []
            src_records.restart()
            while src_records.lookup(name):
                if src_records.package != name:
                    continue
                ver = src_records.version
                if constraint is None or apt_pkg.version_compare(ver, constraint) >= 0:
                    # snapshot binaries/record before next lookup moves cursor
                    candidates.append((ver, list(src_records.binaries), src_records.record))

            if not candidates:
                # No match above -- work out whether the package is missing
                # entirely or just failed the version constraint, for a more
                # precise warning below.
                src_records.restart()
                found = False
                while src_records.lookup(name):
                    if src_records.package == name:
                        found = True
                        break
                if not found:
                    warnings.append("'%s' is not in this suite" % name)
                else:
                    warnings.append("'%s' does not satisfy constraint %s" % (name, constraint))
                continue
            
            # highest version
            best = candidates[0]
            for cand in candidates[1:]:
                if apt_pkg.version_compare(cand[0], best[0]) == 1:
                    best = cand
            
            version, binaries_list, record = best

            entry = {"version": version, "direct": direct, "binaries": {},
                     "files": _source_files(record)}
            any_new = direct
            
            for binpkg in binaries_list:
                per_arch = {}
                if binpkg in bin_cache:
                    pkg = bin_cache[binpkg]
                    for arch in archs:
                        candidate = pkg.candidate
                        if candidate:
                            binver = candidate.version
                            if direct == False and binpkg in base.get(arch, set()):
                                continue
                            per_arch[arch] = binver
                            any_new = True
                if per_arch:
                    entry["binaries"][binpkg] = per_arch
            
            if direct or any_new:
                resolved[name] = entry
                
                for binpkg in entry["binaries"]:
                    if binpkg in exclude: continue
                    try:
                        # high-level apt.Cache: source_name via candidate
                        pkg_obj = bin_cache[binpkg]
                        cand = pkg_obj.candidate
                        source = cand.source_name if cand else binpkg
                    except Exception:
                        source = binpkg
                    if source in exclude or source in seen or source in queued: continue
                    queue.append((source, None, False))
                    queued[source] = False
                    depths.setdefault(source, depths.get(name, 0))

                # Build-Depends are binary packages with arch and profile qualifiers.
                # Evaluate per wanted arch using apt_pkg.parse_src_depends with
                # the suite's configured build-profiles/options (DEB_BUILD_PROFILES
                # / DEB_BUILD_OPTIONS). Profiles/options are joined into
                # APT::Build-Profiles as apt expects.
                profiles = " ".join(request.get("build_profiles", []) + request.get("build_options", []))
                apt_pkg.config.set("APT::Build-Profiles", profiles)
                section = apt_pkg.TagSection(record)
                dep_bins = set()
                for key in ("Build-Depends", "Build-Depends-Indep"):
                    dep_str = section.get(key, "")
                    if not dep_str:
                        continue
                    for arch in archs:
                        try:
                            parsed = apt_pkg.parse_src_depends(dep_str, False, arch)
                        except Exception:
                            # fallback: unfiltered parse if arch-specific fails
                            parsed = apt_pkg.parse_src_depends(dep_str)
                        for dep_group in parsed:
                            for dep in dep_group:
                                dep_name = dep[0] if isinstance(dep, (tuple, list)) else str(dep).split()[0]
                                raw = dep_name
                                if isinstance(dep, (tuple, list)) and len(dep) > 2 and dep[1]:
                                    raw = "%s (%s %s)" % (dep_name, dep[2], dep[1])
                                if dep_name in exclude:
                                    pruned_excluded.append(
                                        {"source": name, "name": dep_name, "arch": arch,
                                         "field": key, "reason": "excluded"})
                                    continue
                                if dep_name in base.get(arch, set()):
                                    pruned_base.append(
                                        {"source": name, "name": dep_name, "arch": arch,
                                         "field": key, "reason": "already in sbuild chroot"})
                                    continue
                                # Recorded for this source regardless of
                                # whether another source's build-deps
                                # already queued it -- seen_bins/queued_bins
                                # dedup the fetch queue, not which sources
                                # actually depend on it, and index()'s
                                # gocode closure walks this list per source.
                                dep_bins.add(dep_name)
                                # 'to' is filled in once every source is
                                # resolved (see bin_owner below) -- the
                                # binary's owning source may not have been
                                # read yet.
                                edges.append(
                                    {"from": name, "to": None, "via": dep_name,
                                     "arch": arch, "field": key, "raw": raw,
                                     "depth": depths.get(name, 0)})
                                if dep_name in seen_bins or dep_name in queued_bins:
                                    continue
                                bin_queue.append(dep_name)
                                queued_bins.add(dep_name)
                                bin_origin_depth.setdefault(
                                    dep_name, depths.get(name, 0) + 1)
                entry["build_dep_bins"] = sorted(dep_bins)
        else:
            # resolve a binary package to its source package (suite+arch aware)
            bin_name = bin_queue.popleft()
            queued_bins.discard(bin_name)
            if bin_name in seen_bins or bin_name in exclude:
                continue
            seen_bins.add(bin_name)
            # skip if already in base for every arch (already handled above, but keep as guard)
            try:
                pkg = bin_cache[bin_name]
                cand = pkg.candidate
                if cand is None:
                    warnings.append("'%s' is not in this suite" % bin_name)
                    continue
                source = cand.source_name
                if source in exclude or source in seen or source in queued:
                    continue
                # suite remains the resolver's suite; arch is carried via the binary's candidate (native arch for now).
                # If the same source was already queued via another binary, dedup via queued/seen.
                queue.append((source, None, False))
                queued[source] = False
                depths.setdefault(source, bin_origin_depth.get(bin_name, 1))
            except Exception:
                warnings.append("'%s' is not in this suite" % bin_name)
                continue

    # 'to' names the source owning 'via' -- only known now that every
    # source's own 'binaries' (its Binary: field) has been read. An edge
    # whose binary never resolved to any source (excluded from bin_cache,
    # apt had nothing for it) is dropped rather than shipped with a null
    # target -- vendor-why has nothing useful to say about it either way.
    bin_owner = {}
    for src, ent in resolved.items():
        for binpkg in ent.get("binaries", {}):
            bin_owner[binpkg] = src
    for edge in edges:
        edge["to"] = bin_owner.get(edge["via"])
    edges = [e for e in edges if e["to"] is not None]

    # Reverse lookup ("who pulled X"), sorted so the shallowest -- most
    # direct -- reason comes first.
    reverse = {}
    for edge in edges:
        reverse.setdefault(edge["to"], []).append(
            {"parent": edge["from"], "via": edge["via"], "arch": edge["arch"],
             "field": edge["field"], "depth": edge["depth"]})
    for rows in reverse.values():
        rows.sort(key=lambda r: (r["depth"], r["parent"]))

    graph = {"edges": edges, "reverse": reverse,
             "pruned": {"base_chroot": pruned_base, "excluded": pruned_excluded}}

    with open(os.path.join(MOUNT, "response.json"), "w") as f:
        json.dump({"ok": True, "sources": resolved, "warnings": warnings,
                   "graph": graph}, f)

try:
    main()
except Exception as e:
    with open(os.path.join(MOUNT, "response.json"), "w") as f:
        json.dump({"ok": False, "error": "%s\n%s" % (e, traceback.format_exc())}, f)
'''

# One suite's resolve container: a BuilderImage of that suite's own feed,
# standing on the shared host bootstrap. Kept apart from Builder
# (seine/packages.py) since a vendor's own build-dependency closure needs
# a suite of its own, not the release the rest of the specification
# builds for.
class VendorResolver:
    def __init__(self, distro, suite, options):
        self.distro = distro
        self.suite = suite
        self.options = options
        self.suite_distro = _suite_distro(distro, suite)

    def _builder(self, hostBootstrap):
        # Suite-specific bootstrap so the resolver's dpkg status does not
        # pollute candidate selection across releases -- built from the
        # suite's own underlying release ('bookworm' for
        # 'bookworm-security'), not the suite name itself: only a release
        # is ever a Debian docker tag, and 'FROM debian:bookworm-security'
        # pulls nothing that exists (docker.io publishes base and
        # '-backports' tags, never '-security'/'-updates').
        release = next((f.get("release", f["suite"]) for f in feeds(self.distro)
                        if f["suite"] == self.suite), self.suite)
        _suiteBootstrap = HostBootstrap(
            dict(self.suite_distro, release=release), self.options, force_online=True)
        _suiteBootstrap.create()
        builder = VendorResolverImage(self.suite_distro, self.options)
        builder.create(_suiteBootstrap)
        return builder

    # The package names the specification's own buildd chroot already
    # provides for 'arch', so the closure below does not chase past what
    # a rebuild would already have installed. The specification's own
    # release, not this suite's -- 'packages:' only ever rebuilds against
    # the release being built, whichever suite a vendor entry is being
    # resolved for, so deduping against anything else would compare
    # against a chroot no real rebuild is ever going to use. Made (or
    # reused) the same way 'packages:' would make it for a real build --
    # see SbuildChroot.create() -- and so the same cache entry as one,
    # when both are asked for.
    def base_chroot(self, builder, arch):
        chroot = SbuildChroot(self.distro, self.options, arch).create(builder)
        # './var/lib/dpkg/status', not 'var/lib/dpkg/status': mmdebstrap
        # tars its root with 'tar -C rootfs .', which GNU tar always
        # names with a leading './' -- '-xO' matches a member by its
        # exact name, and the archive has no member named without it.
        # Not 'architecture=arch': that mounts the chroot cache directory
        # of 'builder's own distro (this suite's), while the chroot above
        # was made under the specification's own release -- the two agree
        # for an ordinary build, where the builder is that release's own,
        # but not here.
        out = builder.output(
            ["tar", "--zstd", "-xO", "-f",
             "/root/.cache/sbuild/%s" % chroot.filename, "./var/lib/dpkg/status"],
            volumes=[(os.path.dirname(chroot.path), "/root/.cache/sbuild")])
        return {m.group(1) for m in re.finditer(
            r"^Package:\s*(\S+)$", out.decode(), re.MULTILINE)}

    # The suite's own resolved manifest: every source this suite's
    # entries name, and their full build-dependency closure, each with
    # the binaries (and per-architecture versions) a vendor of it needs.
    def resolve(self, hostBootstrap, entries, archs, exclude):
        builder = self._builder(hostBootstrap)
        base = {arch: sorted(self.base_chroot(builder, arch)) for arch in archs}

        request = {
            "archs": archs,
            "exclude": exclude,
            "base_chroot": base,
            "sources": [{"name": e.name, "version": e.version} for e in entries],
            "build_options": self.distro.get("build-options", []),
            "build_profiles": self.distro.get("build-profiles", []),
        }
        scratch = tempfile.mkdtemp(dir=ContainerEngine.scratch(), prefix="vendor-")
        try:
            with open(os.path.join(scratch, REQUEST_FILE), "w") as f:
                json.dump(request, f)
            script = os.path.join(scratch, "resolve.py")
            with open(script, "w") as f:
                f.write(RESOLVE_SCRIPT)
            # apt's own lists, host-persisted per suite the same way
            # downloads(suite) already is for /var/cache/apt/archives (see
            # ContainerEngine.downloads_lists()) -- so a second resolve of
            # this suite (an ordinary '--refresh') does not re-fetch the
            # multi-MB Sources/Packages files a first one already did.
            # '-u': unbuffered, so the progress this prints as it works
            # reaches whoever is reading this task's own output -- live,
            # over podman's pipe -- as it happens rather than in one
            # block when the interpreter exits. Python fully buffers its
            # stdout the moment it is not a terminal, which a container
            # run through podman never is.
            builder.exec(["python3", "-u", "/vendor/resolve.py"],
                        volumes=[(scratch, RESOLVE_MOUNT),
                                 (ContainerEngine.downloads_lists(self.suite),
                                  "/var/lib/apt/lists")],
                        check=True)
            with open(os.path.join(scratch, RESPONSE_FILE)) as f:
                response = json.load(f)
        finally:
            if self.options.get("keep"):
                print("keeping '%s' (vendor resolve request/response) as requested"
                      % scratch)
            else:
                shutil.rmtree(scratch, ignore_errors=True)

        if response.get("ok") != True:
            raise ValueError("resolving vendor packages for '%s' failed: %s"
                             % (self.suite, response.get("error")))
        for warning in response.get("warnings", []):
            print("warning: %s" % warning)
        return response["sources"], response["graph"]

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

def _deb_has_gocode(deb_path):
    try:
        result = subprocess.run(
            ["sh", "-c", "dpkg-deb -c \"$1\" 2>/dev/null | grep -q 'usr/share/gocode/src/'", "_", deb_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return result.returncode == 0
    except Exception:
        return False

def _binary_has_gocode(where, binpkg, arch, version):
    for candidate in (arch, "all"):
        for name in (_binary_filename(binpkg, candidate, version),
                     _binary_filename_legacy(binpkg, candidate, version)):
            deb = os.path.join(where, name)
            if os.path.isfile(deb):
                return _deb_has_gocode(deb)
    return False

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

def fetch_source(builder, suite, source, version):
    where = repository(suite)
    with locked(where, shared=True):
        # 'src:', not a bare name: apt-get prefers a *binary* package of
        # the same name over a source one when both exist -- see
        # build_deps()'s own comment, in RESOLVE_SCRIPT above, for a
        # concrete case. Bookworm's apt-get source (2.6) does not understand
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
    say(builder.options, "vendor source %s made" % key)

def fetch_binary(builder, suite, binpkg, arch, version):
    where = repository(suite)
    with locked(where, shared=True):
        builder.exec(["apt-get", "-o", "APT::Architectures::=%s" % arch] + _APT_SANDBOX_OPTS +
                     ["download", "-qq", "%s:%s=%s" % (binpkg, arch, version)],
                    workdir="/vendor-repo",
                    volumes=[(where, "/vendor-repo"), _lists_volume(suite)])
    key = _artifact_key(suite, binpkg, binpkg, arch, version)
    has = _binary_has_gocode(where, binpkg, arch, version)
    Index().made(VENDOR, key, metadata={"has_gocode": has})
    say(builder.options, "vendor binary %s made" % key)

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
# refuses the link (cache and deploy can be moved to different roots of
# their own -- SEINE_CACHE_DIR/SEINE_DEPLOY_DIR -- so unlike a fetch
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
# already fetched into repository(suite) (the cache) and the frozen
# manifest beside it. Nothing is read from, or written to, the cache
# beyond that: cache holds only ever the flat, durable fetched files,
# never a pool/dists view of its own -- see repository()'s and
# deploy_repository()'s own comments for why the two are kept apart.
#
# Rebuilt from scratch every call, never incrementally: a source or
# binary's main-vs-extra classification is decided here, fresh from the
# manifest, every time -- not migrated file-by-file from wherever a
# previous run put it. That is what lets classification change freely
# between two runs (a different spec, or the closure resolving
# differently) without a "promotion" step to patch it up after the
# fact, and it is cheap enough to always do outright: no network call
# anywhere in this function, only hardlinking files already on disk and
# running apt-ftparchive/gpg -- called by index_tasks() as part of a
# build's own 'vendor' task, which 'rootfs' waits on (image.py's own
# task graph) rather than reindexing again itself.
def index(builder, suite, signer):
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

        manifest_doc = load_manifest(suite)
        manifest = manifest_doc.get("sources", {})

        # Build binpkg -> (owning source, per_arch version dict)
        bin_owner = {}
        for src, ent in manifest.items():
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

        # BFS closure over build_dep_bins seeded from already-direct sources
        closure = set()
        queue = collections.deque(src for src, ent in manifest.items() if ent.get("direct"))
        seen_queue = set(queue)
        while queue:
            src = queue.popleft()
            for binpkg in manifest[src].get("build_dep_bins", []):
                if not bin_has_gocode(binpkg):
                    continue
                target, _ = bin_owner.get(binpkg, (None, None))
                if target is None or target in closure or manifest[target].get("direct"):
                    continue
                closure.add(target)
                if target not in seen_queue:
                    seen_queue.add(target)
                    queue.append(target)

        if closure:
            for src in closure:
                manifest[src]["direct"] = True
            manifest_doc["sources"] = manifest
            save_manifest(suite, manifest_doc)

        for source, entry in sorted(manifest.items()):
            component = "main" if entry.get("direct") else "extra"
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

# ---------------------------------------------------------------------
# The task graph. Built in three waves, each its own 'tasks.run()' call,
# rather than one static graph: which artifacts wave 2 fetches is not
# known until wave 1's resolve step has run, and seine/tasks.py's own DAG
# is built once up front -- see VendorCmd._run().
#
# Wave 1's own result does not come back through a Task: tasks.py's
# 'Task.run()' is called for what it does, its return value thrown away
# (the same as every other caller of it -- see packages.py's own
# Task bodies). Each resolve task instead writes into 'results', a dict
# handed in and shared by every task of this wave, keyed by suite to a
# (sources, graph) pair -- VendorResolver.resolve()'s own return shape.
# ---------------------------------------------------------------------

def resolve_tasks(distro, entries, suites_wanted, options, hostBootstrap,
                  exclude, results, extra_archs=()):
    tasks = []
    for suite in suites_wanted:
        suite_entries = entries_for(entries, suite)
        if len(suite_entries) == 0:
            continue
        archs = sorted({a for e in suite_entries
                        for a in e.architectures_for(
                            architectures(entries, distro, extra_archs))})
        resolver = VendorResolver(distro, suite, options)

        def run(resolver=resolver, suite_entries=suite_entries, archs=archs,
                suite=suite):
            results[suite] = resolver.resolve(hostBootstrap, suite_entries,
                                              archs, exclude)

        tasks.append(Task("resolve:%s" % suite, run, needs=["bootstrap-host"]))
    return tasks

# The suite's own builder container, made (or found already current) the
# first time a task asks for it and reused after -- not before any task
# has run, the way fetch_tasks()/index_tasks() building it themselves
# would: constructing the task list is then free of podman, which is
# what lets it be described (or tested) without one.
def _builder_for(distro, suite, options, hostBootstrap):
    return VendorResolver(distro, suite, options)._builder(hostBootstrap)

# One flat, dependency-free task per artifact still missing -- every
# fetch for a suite may run beside every other, including another
# suite's, since each writes its own filename into its own suite's
# repository (see fetch_source()/fetch_binary() above). An artifact
# already sitting there under its own name is not given a task at all:
# a source's exact files are known from the manifest's own 'files' (see
# RESOLVE_SCRIPT's _source_files()), and a binary's filename is a pure
# function of its name/arch/version (_binary_filename()), so this is
# decided on the host, before any container -- unlike apt's own
# skip-if-present check, which only happens once one has already been
# spawned to ask it.
def fetch_tasks(distro, suite, manifest, options, hostBootstrap, archs=None):
    tasks = []
    seen_bins = set()
    where = repository(suite)
    for source, entry in sorted(manifest.items()):
        version = entry["version"]
        files = entry.get("files") or []
        src_key = _artifact_key(suite, source, "source", None, version)
        if len(files) > 0 and all(os.path.isfile(os.path.join(where, f))
                                  for f in files):
            Index().hit(VENDOR, src_key)
            say(options, "vendor source %s reused" % src_key)
        else:
            tasks.append(Task(
                "fetch-src:%s:%s" % (suite, source),
                lambda distro=distro, suite=suite, source=source, version=version:
                    fetch_source(_builder_for(distro, suite, options, hostBootstrap),
                                suite, source, version)))
        for binpkg, per_arch in sorted(entry["binaries"].items()):
            for arch, binver in sorted(per_arch.items()):
                if archs is not None and arch not in archs:
                    continue
                key = (binpkg, arch)
                if key in seen_bins:
                    continue
                seen_bins.add(key)
                bin_key = _artifact_key(suite, binpkg, binpkg, arch, binver)
                if _binary_already_fetched(where, binpkg, arch, binver):
                    cached = _index_has_gocode(bin_key)
                    if cached is None:
                        has = _binary_has_gocode(where, binpkg, arch, binver)
                        try:
                            Index().patch(VENDOR, bin_key, {"has_gocode": has})
                        except Exception:
                            pass
                    else:
                        Index().hit(VENDOR, bin_key)
                    say(options, "vendor binary %s reused" % bin_key)
                    continue
                tasks.append(Task(
                    "fetch-bin:%s:%s:%s" % (suite, binpkg, arch),
                    lambda distro=distro, suite=suite, binpkg=binpkg, arch=arch,
                           binver=binver:
                        fetch_binary(_builder_for(distro, suite, options, hostBootstrap),
                                    suite, binpkg, arch, binver)))
    return tasks

# One task per suite, run only once every one of that suite's fetches has
# already finished -- a separate, later 'tasks.run()' call rather than a
# 'needs' edge onto the fetch wave, so a failed fetch can be resubmitted
# (see VendorCmd._run_wave()) without the index task's own 'needs' naming
# an attempt that no longer exists under that name.
def index_tasks(distro, suites_wanted, options, hostBootstrap, signer):
    tasks = []
    for suite in suites_wanted:
        tasks.append(Task(
            "index:%s" % suite,
            lambda distro=distro, suite=suite:
                index(_builder_for(distro, suite, options, hostBootstrap),
                     suite, signer)))
    return tasks

# ---------------------------------------------------------------------
# 'seine vendor': the CLI surface.
# ---------------------------------------------------------------------

# Echoes a task's own log file to the terminal as it grows, so a wave run
# one task at a time (jobs=1) keeps a log file -- read back on failure the
# same way tasks.py's own '_report()' already would, and by hand for a
# run that succeeded -- without giving up seeing it live. A file is
# followed rather than teed at the Python level because it has to be:
# tasks.py's own 'capture()' hands podman's subprocess the log file's
# real descriptor to write into directly (see its own docstring), which
# is not something a Python object standing in for two streams can be.
#
# Never used for jobs>1: several tasks' own output interleaved on one
# terminal is exactly what giving each of them a file of its own avoids
# (see tasks.py's own 'output()' docstring), and this would put it back.
class _LiveFollower:
    def __init__(self, logs):
        self.logs = logs
        self._thread = None
        self._stop = None

    def started(self, name):
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._follow,
            args=(os.path.join(self.logs, "%s.log" % name), self._stop),
            daemon=True)
        self._thread.start()

    def finished(self, name, failed=False):
        if self._stop is not None:
            self._stop.set()
            self._thread.join()
        self._stop = self._thread = None

    def say(self, message):
        sys.stderr.write("\n%s\n" % message)

    # 'started()' fires before tasks.py opens the file, so the first
    # moments poll for it rather than fail over its absence -- then reads
    # whatever is appended, sleeping between polls the way 'tail -f'
    # does. 'finished()' setting the stop event does not mean nothing is
    # left to read: the task may have written its last lines and exited
    # between this loop's last read and 'stop' being noticed, so one more
    # read follows before this returns.
    def _follow(self, path, stop):
        while not stop.is_set() and not os.path.isfile(path):
            time.sleep(0.05)
        try:
            with open(path, "r") as f:
                while True:
                    line = f.readline()
                    if line:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        continue
                    if stop.is_set():
                        rest = f.read()
                        if rest:
                            sys.stdout.write(rest)
                            sys.stdout.flush()
                        return
                    time.sleep(0.05)
        except OSError:
            return

# A failed fetch is resubmitted as a new Task, up to this many times in
# total, rather than retried silently inside one Task.run(): each attempt
# gets a name (and, with several running beside each other, a log file)
# of its own, an honest place in the record rather than folded into one
# task's wall clock. Resolving and indexing are not retried -- an apt
# failure there is almost always the specification asking for something
# that is not there, which asking again does not fix.
MAX_ATTEMPTS = 3

class VendorCmd(Cmd):
    NAME = "vendor"
    SHORT_OPTIONS = "dhj:v"
    LONG_OPTIONS = ["debug", "help", "jobs=", "vendor-sign-key=",
                    "architecture=", "suite=", "verbose"]

    def __init__(self):
        # 'jobs' falls back to the persisted setting (seine/settings.py,
        # '/set jobs N' in the TUI) before the hardcoded '1', the same
        # way BuildCmd.__init__() already does -- an explicit '-j'/
        # '--jobs' below still overrides either.
        self.options = {"debug": False, "jobs": settings.load().get("jobs") or 1,
                        "keep": False, "vendor_sign_key": None, "verbose": False}

    def usage(self):
        return USAGE

    # getopt has no notion of an argument that is only sometimes there,
    # and '--refresh' takes one only sometimes: '--refresh' alone asks
    # for every entry, '--refresh=NAME' for one. Taken out of argv by
    # hand before getopt sees the rest, which knows nothing about it.
    def _take_refresh(self, argv):
        remaining = []
        refresh = False
        for arg in argv:
            if arg == "--refresh":
                refresh = True
            elif arg.startswith("--refresh="):
                refresh = arg.split("=", 1)[1]
            else:
                remaining.append(arg)
        return refresh, remaining

    def main(self, argv):
        refresh, argv = self._take_refresh(argv)
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.usage())
            sys.exit(1)

        suites_asked = []
        archs_asked = []
        for o, a in opts:
            if o in ("-d", "--debug"):
                self.options["debug"] = True
                self.options["verbose"] = True
            elif o in ("-h", "--help"):
                print(self.usage())
                return
            elif o in ("-j", "--jobs"):
                try:
                    self.options["jobs"] = int(a)
                except ValueError:
                    sys.stderr.write("error: --jobs expects a number\n")
                    sys.exit(1)
                if self.options["jobs"] < 1:
                    sys.stderr.write("error: --jobs shall be at least 1\n")
                    sys.exit(1)
            elif o in ("--vendor-sign-key"):
                self.options["vendor_sign_key"] = a
            elif o in ("--suite"):
                suites_asked.append(a)
            elif o in ("--architecture"):
                archs_asked.append(a)
            elif o in ("-v", "--verbose"):
                self.options["verbose"] = True
            else:
                assert False, "unhandled option"

        if len(args) == 0:
            sys.stderr.write("error: vendor command expects a YAML file\n")
            sys.exit(1)

        from seine.build import BuildCmd
        from seine import utils
        build = BuildCmd()
        build.options = dict(build.options, ansible_library=[])
        try:
            build.load_all(args)
            distro = utils.distribution(build.spec)
            entries = parse(build.spec)
            exclude = exclusions(build.spec)
            extra_archs = extra_architectures(build.spec)
            available = named_suites(entries, distro)
        except OSError as e:
            sys.stderr.write("error: couldn't open specification file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(3)

        if len(entries) == 0:
            print("nothing to vendor: this specification has no 'vendor:' section")
            return 0

        for suite in suites_asked:
            if suite not in available:
                sys.stderr.write(
                    "error: '--suite %s' names a suite this specification's "
                    "'vendor:' section does not ask for, expected one of %s\n"
                    % (suite, ", ".join(available)))
                sys.exit(1)
        wanted = suites_asked if len(suites_asked) > 0 else available

        # Unlike '--suite', narrowing here never skips a resolve: an
        # architecture's closure is walked the same way regardless of
        # what a run wants fetched, so a suite's frozen manifest always
        # stays complete for every architecture 'vendor:' asks for --
        # only fetch_tasks() (and so what actually reaches the disk)
        # is scoped down. None (nothing asked) keeps that unscoped.
        available_archs = architectures(entries, distro, extra_archs)
        for arch in archs_asked:
            if arch not in available_archs:
                sys.stderr.write(
                    "error: '--architecture %s' names an architecture this "
                    "specification's 'vendor:' section does not ask for, "
                    "expected one of %s\n"
                    % (arch, ", ".join(available_archs)))
                sys.exit(1)
        archs = archs_asked if len(archs_asked) > 0 else None

        # Only what this run actually wants needs a configured feed: a
        # '--suite' run does not fail over a suite it never asked for --
        # see named_suites()/unconfigured_suites() above.
        unknown = unconfigured_suites(wanted, distro)
        if len(unknown) > 0:
            sys.stderr.write(
                "error: 'vendor:' asks for %s, which %s no configured feed "
                "-- add it under 'distribution: feeds:' first\n"
                % (", ".join(unknown), "has" if len(unknown) == 1 else "have"))
            sys.exit(3)

        try:
            sys.exit(self._run(distro, entries, exclude, wanted, refresh, archs,
                               extra_archs))
        except OSError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(3)
        except subprocess.CalledProcessError as e:
            sys.stderr.write("error: vendor failed: %s\n" % e)
            sys.exit(4)
        except (task_runner.Interrupted, KeyboardInterrupt) as e:
            sys.stderr.write("error: vendor was %s\n" % (str(e) or "interrupted"))
            sys.exit(130)

    # 'display' is the same Reporter-shaped sink 'seine build's own
    # TUI screen already feeds tasks.run() through -- 'started'/
    # 'finished'/'say', nothing else. None (the CLI's own path, via
    # main()) keeps '_run_wave()'s existing verbose/'-j 1' live-tail
    # instead; a caller wanting its own progress view (the vendor
    # screen) hands one in and gets it on every wave, not just some.
    #
    # 'archs' scopes fetch_tasks() alone (see main()'s own comment on
    # why resolving stays unscoped) -- None fetches every architecture
    # 'vendor:' asks for, same as before this existed.
    #
    # 'extra_archs' is extra_architectures()'s own return
    # ('distribution: architectures:') -- folded into manifest_digest()
    # too, so a spec newly naming one re-resolves to actually cover it
    # rather than keeping a manifest frozen before it was asked for.
    def _run(self, distro, entries, exclude, wanted, refresh, archs=None,
             extra_archs=(), display=None):
        hostBootstrap = HostBootstrap(distro, self.options, force_online=True)

        # Which suites need a fresh resolve: every one of them when
        # '--refresh' was given with no name, one already-frozen entry
        # refreshed within an otherwise-kept manifest when given one,
        # every suite with no manifest yet at all, and every suite whose
        # frozen manifest no longer matches what this spec's own
        # 'vendor:' section (entries, excludes, profiles/options, feeds)
        # asks for -- see manifest_digest(). An ordinary, unchanged
        # rerun still just freezes what an earlier one resolved rather
        # than asking apt again for it.
        digests = {suite: manifest_digest(distro, entries, exclude, suite,
                                          extra_archs)
                  for suite in wanted}
        stale = []
        manifests = {}
        for suite in wanted:
            document = load_manifest(suite)
            manifest = document.get("sources", {})
            if (refresh is not False or len(manifest) == 0 or
                    document.get("digest") != digests[suite]):
                stale.append(suite)
            else:
                manifests[suite] = manifest

        if len(stale) > 0:
            results = {}
            resolve = resolve_tasks(distro, entries, stale, self.options,
                                    hostBootstrap, exclude, results,
                                    extra_archs)
            self._run_wave([hostBootstrap.task()] + resolve, retryable=False,
                          display=display)
            for suite in stale:
                fresh, graph = results[suite]
                if isinstance(refresh, str):
                    old = load_manifest(suite)
                    old_sources = old.get("sources", {})
                    merged = self._merge_refresh(old_sources, fresh, refresh)
                    # Same selection _merge_refresh() just made for
                    # 'sources' -- a source kept unchanged (reverted to
                    # its old entry) keeps its old graph rows too, so the
                    # two never disagree about a source this run never
                    # actually touched.
                    moved = {name for name in merged
                             if name == refresh or name not in old_sources}
                    graph = self._merge_refresh_graph(
                        old.get("graph", {}), graph, moved)
                    fresh = merged
                manifests[suite] = fresh
                save_manifest(suite, {"sources": fresh, "digest": digests[suite],
                                      "graph": graph, "graph_version": GRAPH_VERSION})
        else:
            # The resolve wave above is what builds this, as one of its
            # own tasks -- skipped entirely when every suite's manifest
            # is already frozen, which leaves the image the fetch/index
            # containers stand on still unbuilt (or gone, after 'seine
            # cache clear images') the first time a run touches nothing
            # but already-resolved suites. Still run as a one-task wave,
            # not a bare call: a caller with its own display (the vendor
            # screen) gets a row and a real log file for it either way --
            # otherwise this step is invisible on every ordinary rerun,
            # which is the *common* case once a suite's manifest is
            # frozen, not a rare one.
            self._run_wave([hostBootstrap.task()], retryable=False, display=display)

        fetch = []
        for suite in wanted:
            fetch += fetch_tasks(distro, suite, manifests[suite], self.options,
                                 hostBootstrap, archs)
        self._run_wave(fetch, retryable=True, display=display)

        signer = signing.vendor_signer(self.options)
        self._run_wave(
            index_tasks(distro, wanted, self.options, hostBootstrap, signer),
            retryable=False, display=display)

        for suite in wanted:
            print("vendored %d source package(s) for %s"
                 % (len(manifests[suite]), suite))
        # A caller with its own display has nothing to read the prints
        # above off of (they go to the real terminal, not wherever it is
        # watching) -- the same summary, one line, through 'say()'.
        if display is not None:
            display.say("vendored " + ", ".join(
                "%d source package(s) for %s" % (len(manifests[suite]), suite)
                for suite in wanted))
        return 0

    # '--refresh=NAME': the freshly-resolved closure with every entry but
    # NAME put back to what the existing manifest already had -- NAME
    # moves, and whatever only its updated build-deps now reach (absent
    # from the old manifest, so there is nothing to put back) moves with
    # it, but nothing else does.
    def _merge_refresh(self, old, fresh, name):
        merged = dict(fresh)
        for source, entry in old.items():
            if source != name and source in merged:
                merged[source] = entry
        return merged

    # The graph's own half of _merge_refresh(): edges/pruned rows keep
    # whichever side (old or freshly-resolved) 'moved' says their own
    # 'source'/'from' belongs to, then 'reverse' is rebuilt from the
    # combined edges rather than merged row by row -- a 'to' target may
    # gain or lose a parent on either side, and rebuilding is simpler
    # (and cheaper, this is never more than a few hundred rows) than
    # reconciling that by hand.
    def _merge_refresh_graph(self, old, fresh, moved):
        old_edges = old.get("edges", [])
        fresh_edges = fresh.get("edges", [])
        edges = ([e for e in old_edges if e["from"] not in moved] +
                 [e for e in fresh_edges if e["from"] in moved])
        old_pruned = old.get("pruned", {})
        fresh_pruned = fresh.get("pruned", {})
        pruned = {}
        for kind in ("base_chroot", "excluded"):
            pruned[kind] = (
                [p for p in old_pruned.get(kind, []) if p["source"] not in moved] +
                [p for p in fresh_pruned.get(kind, []) if p["source"] in moved])
        return {"edges": edges, "reverse": _reverse_of(edges), "pruned": pruned}

    def _logs(self):
        base = ContainerEngine.logs_root()
        os.makedirs(base, exist_ok=True)
        return tempfile.mkdtemp(dir=base, prefix="vendor-")

    # One 'tasks.run()' call, with retries for the ones that may sensibly
    # be retried (see MAX_ATTEMPTS above): a task that never got to run
    # because an earlier failure stopped new ones starting is retried
    # exactly like one that failed outright -- neither says anything
    # about that task itself.
    def _run_wave(self, wave_tasks, retryable, display=None):
        if len(wave_tasks) == 0:
            return
        jobs = self.options["jobs"]
        verbose = self.options["verbose"]
        # Every wave leaves a log behind, verbose or not -- unlike
        # 'seine build', which only bothers when nothing is watching
        # live (see tasks.py's own 'output()' docstring): a resolve can
        # run for minutes over a large build-dependency closure with a
        # single line to show for whole stretches of it, and losing that
        # to a lost terminal is a worse trade than the file costs.
        logs = self._logs()
        # Optional: a caller tailing this wave's own log files (the
        # vendor screen) needs to know where they landed -- a fresh
        # directory every wave, unlike a build's single stable
        # 'image.logs'. '_LiveFollower' has no use for its own path
        # back, so this is checked for, not assumed.
        if display is not None:
            wave_logs = getattr(display, "wave_logs", None)
            if wave_logs is not None:
                wave_logs(logs)
        # A caller's own display (the vendor screen's TextualReporter)
        # wins outright -- verbose/'-j 1' live-tail is the CLI's own
        # fallback for when nothing else is watching, not a rule that
        # applies to every caller of this.
        if display is not None:
            follower = display
        else:
            # Followed live only when nothing else could be running beside
            # it to interleave with (see _LiveFollower's own docstring).
            follower = _LiveFollower(logs) if (verbose and jobs <= 1) else None

        # Not retried: 'tasks.run()' as 'seine build' itself already uses
        # it, stopping (jobs=1) or reporting (jobs>1) the way every other
        # step of a build does.
        if retryable == False:
            task_runner.run(wave_tasks, jobs=jobs, logs=logs, verbose=verbose,
                            display=follower)
            return

        # Retried: every fetch is independent of every other (see
        # fetch_tasks()), so one failing is not a reason for the rest not
        # to run -- true with jobs=1 as much as with several, unlike a
        # real build's steps, which is why this does not simply delegate
        # to 'tasks.run()' the way the branch above does: with jobs=1
        # that raises straight out of the failing task instead of
        # collecting it the way jobs>1's 'Failed' does, and stops there.
        # Each task's own body is wrapped to catch what it raises instead
        # of letting it propagate, so both paths behave alike.
        pending = wave_tasks
        # A retried task's name stays '<base>#<attempt>', not one '#N'
        # suffix piled onto the last -- tracked apart from 'pending'
        # itself so a task retried twice is still named after what it
        # is, not after its own previous attempt.
        bases = {task.name: task.name for task in wave_tasks}
        attempt = 1
        while True:
            failures = []
            lock = threading.Lock()
            wrapped = []
            for task in pending:
                def body(task=task):
                    try:
                        task.run()
                    except Exception as e:
                        with lock:
                            failures.append((task.name, e))
                wrapped.append(Task(task.name, body))
            task_runner.run(wrapped, jobs=jobs, logs=logs, verbose=verbose,
                            display=follower)
            if len(failures) == 0:
                return
            if attempt >= MAX_ATTEMPTS:
                raise task_runner.Failed(failures, [])
            attempt += 1
            print("retrying %d task(s) (attempt %d/%d)..."
                 % (len(failures), attempt, MAX_ATTEMPTS))
            by_name = {task.name: task for task in pending}
            retried = []
            for name, _ in failures:
                new_name = "%s#%d" % (bases[name], attempt)
                bases[new_name] = bases[name]
                retried.append(Task(new_name, by_name[name].run))
            pending = retried

USAGE = """
Build a local, signed apt repository of a specification's own packages

Description:
  'vendor:' entries name source packages (and, transitively, their full
  build-dependency closure) to fetch out of the distribution's feeds into
  a repository of their own -- one per suite -- so a specification can
  still be rebuilt years after those feeds are gone. See 'vendor:' and
  'apt-pull-mode:' in the specification documentation for the schema.

  A suite already vendored is not resolved again: what apt would resolve
  today may not be what it resolved when a version was first frozen, and
  a specification's vendor should not drift underneath it between two
  ordinary runs. '--refresh' asks for a new resolve; '--refresh=NAME'
  scopes that to one source package, keeping every other one frozen as
  it was.

Usage:
  seine vendor [-j N] [--refresh[=NAME]] [--vendor-sign-key KEY]
               [--suite NAME]... [--architecture NAME]... SPEC...

Flags:
  -d, --debug           print what each step decided and its full output
  -h, --help            print this message
  -j, --jobs N          fetch up to N artifacts at once (1 by default)
      --vendor-sign-key KEY
                        sign the repository with this gpg key (or set
                        SEINE_VENDOR_SIGN_KEY), independent of the key
                        'packages:' rebuilds are signed with
      --refresh[=NAME]  resolve again rather than keep the frozen
                        manifest; scoped to one source package when
                        given a name, every one of them otherwise
      --suite NAME      vendor only this suite; may be given more than
                        once. Every suite the specification's 'vendor:'
                        section asks for otherwise
      --architecture NAME
                        fetch binaries for only this architecture; may
                        be given more than once. Every architecture the
                        specification's 'vendor:' section asks for
                        otherwise. Resolving itself is unaffected --
                        only what gets fetched is narrowed
  -v, --verbose         print each step as it runs, and what the cache
                        reused or made
"""
