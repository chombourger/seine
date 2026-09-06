# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Resolving a suite's own apt dependency graph: a throwaway container
# built from the suite's feeds (VendorResolverImage), and the script
# (RESOLVE_SCRIPT) run inside it to do the actual 'apt-get source'/
# download-only dry run (VendorResolver).

import json
import os
import re
import shutil
import tempfile

from seine.bootstrap import Bootstrap
from seine.sbuild import SbuildChroot
from seine.container import ContainerEngine
from seine.utils import feeds, apt_sources
from seine.utils import APT_CLEANUP
from seine.utils import PRIVILEGED_RUN_OPTIONS

from .manifest import _suite_distro


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
            self.distro["release"],
            APT_CLEANUP)

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
    apt-get update -qqy && \\
    {5}
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

    # A foreign 'archs' entry needs its own Packages/Sources indices
    # before 'apt-get update' below can fetch them -- 'dpkg
    # --add-architecture' persists that for the rest of this one
    # process (unlike fetch_binary()'s own per-call '-o
    # APT::Architectures::=', which is throwaway by necessity). Safe to
    # call for the native architecture too: dpkg already carries it.
    for arch in archs:
        subprocess.run(["dpkg", "--add-architecture", arch], check=True)

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
                # Looked up per arch, qualified 'binpkg:arch' -- an
                # unqualified 'bin_cache[binpkg]' only ever resolves to
                # apt's *native* architecture's own Package object, so
                # reusing its '.candidate' for every arch in a loop (as
                # this used to) silently copied the native version into
                # every foreign arch's slot instead of actually
                # resolving one. Correct by construction for an
                # 'Architecture: all' binary (apt gives every
                # configured arch the same candidate for one of those
                # anyway) and now correct for an arch-specific one too;
                # a 'binpkg:arch' apt has no candidate for (built for
                # some archs and not others) is simply skipped, not
                # fabricated.
                for arch in archs:
                    qualified = "%s:%s" % (binpkg, arch)
                    if qualified not in bin_cache:
                        continue
                    candidate = bin_cache[qualified].candidate
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
        # Qualified rather than the bare name a plain 'from seine.bootstrap
        # import HostBootstrap' would give: tests replace this by patching
        # 'seine.vendor.HostBootstrap' (this package's own re-export), which
        # a bare name here would never see.
        from seine import vendor
        _suiteBootstrap = vendor.HostBootstrap(
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
