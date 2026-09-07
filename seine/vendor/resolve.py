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


# A slim container holding the tools to resolve a package's closure with
# libapt-pkg, bootstrapped per suite so its dpkg status matches that
# suite's own archive.
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
# Reads/writes RESOLVE_MOUNT's request/response files and never raises
# past 'main()' -- an apt failure is reported in the response rather than
# a bare podman exit code, so the host knows which package it was about.
RESOLVE_SCRIPT = r'''
import apt
import apt_pkg
import collections, json, os, subprocess, traceback

MOUNT = "/vendor"

# A source's own files, read from both 'Files:' and 'Checksums-Sha256:'
# and deduped, since either alone should list the same set.
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

    # Needed before 'apt-get update' can fetch a foreign arch's own
    # Packages/Sources indices. Harmless for the native arch too.
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

    # Provenance for the dependency graph (vendor-graph/vendor-why):
    # 'depths' is BFS depth from the nearest root (roots start at 0);
    # 'edges' is one row per surviving (source, build-dep binary), 'to'
    # filled in once every source is resolved; 'bin_origin_depth' carries
    # a build-dep binary's depth over to whichever source ends up owning it.
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

            # SourceRecords isn't iterable, only lookup()-able. lookup(name)
            # also matches a binary named 'name' (e.g. "ecj" finds
            # eclipse-jdt-core), so filter to package == name.
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
                # Qualified 'binpkg:arch' lookup -- an unqualified
                # 'bin_cache[binpkg]' only ever resolves apt's native
                # arch, which would silently copy that version into every
                # foreign arch's slot instead. An arch with no candidate
                # (built for some archs, not others) is skipped, not
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
                        pkg_obj = bin_cache[binpkg]
                        cand = pkg_obj.candidate
                        source = cand.source_name if cand else binpkg
                    except Exception:
                        source = binpkg
                    if source in exclude or source in seen or source in queued: continue
                    queue.append((source, None, False))
                    queued[source] = False
                    depths.setdefault(source, depths.get(name, 0))

                # Build-Depends carry arch and profile qualifiers, evaluated
                # per wanted arch with the suite's build-profiles/options
                # set as APT::Build-Profiles.
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
                                # Recorded per source even if another
                                # source already queued it -- index()'s
                                # gocode closure walks this list per source.
                                dep_bins.add(dep_name)
                                # 'to' fills in later, once every source
                                # is resolved (see bin_owner below).
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
                # Deduped via queued/seen if another binary already
                # queued the same source.
                queue.append((source, None, False))
                queued[source] = False
                depths.setdefault(source, bin_origin_depth.get(bin_name, 1))
            except Exception:
                warnings.append("'%s' is not in this suite" % bin_name)
                continue

    # 'to' names the source owning 'via', only known now that every
    # source's own binaries have been read. An edge whose binary never
    # resolved to a source is dropped rather than shipped with a null
    # target.
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
# (packages.py) since a vendor's closure needs a suite of its own, not
# the release the rest of the spec builds for.
class VendorResolver:
    def __init__(self, distro, suite, options):
        self.distro = distro
        self.suite = suite
        self.options = options
        self.suite_distro = _suite_distro(distro, suite)

    def _builder(self, hostBootstrap):
        # Built from the suite's underlying release ('bookworm' for
        # 'bookworm-security'), not the suite name -- only a release is
        # ever a Debian docker tag; 'FROM debian:bookworm-security' pulls
        # nothing that exists.
        release = next((f.get("release", f["suite"]) for f in feeds(self.distro)
                        if f["suite"] == self.suite), self.suite)
        # Qualified, not a bare import: tests patch 'seine.vendor.
        # HostBootstrap' (this package's re-export), which a bare name
        # here would never see.
        from seine import vendor
        _suiteBootstrap = vendor.HostBootstrap(
            dict(self.suite_distro, release=release), self.options, force_online=True)
        _suiteBootstrap.create()
        builder = VendorResolverImage(self.suite_distro, self.options)
        builder.create(_suiteBootstrap)
        return builder

    # Package names the spec's own buildd chroot already provides for
    # 'arch', so the closure doesn't chase past what a rebuild would
    # already install. The spec's own release, not this suite's --
    # 'packages:' only ever rebuilds against that release, whichever
    # suite is being resolved.
    def base_chroot(self, builder, arch):
        chroot = SbuildChroot(self.distro, self.options, arch).create(builder)
        # './var/lib/dpkg/status', not 'var/lib/dpkg/status': mmdebstrap
        # tars with a leading './', and '-xO' matches by exact name. Uses
        # 'volumes=' rather than output()'s own 'architecture=', which
        # would mount the wrong distro's chroot cache here.
        out = builder.output(
            ["tar", "--zstd", "-xO", "-f",
             "/root/.cache/sbuild/%s" % chroot.filename, "./var/lib/dpkg/status"],
            volumes=[(os.path.dirname(chroot.path), "/root/.cache/sbuild")])
        return {m.group(1) for m in re.finditer(
            r"^Package:\s*(\S+)$", out.decode(), re.MULTILINE)}

    # The suite's resolved manifest: every source its entries name, plus
    # their full build-dependency closure, each with its binaries and
    # per-arch versions.
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
            # apt's lists are host-persisted per suite, so a second
            # '--refresh' doesn't re-fetch the multi-MB Sources/Packages.
            # '-u': unbuffered, so progress streams live over podman's
            # pipe instead of Python buffering it all until exit.
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
