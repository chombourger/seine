# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The task-graph glue (resolve_tasks/fetch_tasks/index_tasks) and
# 'seine vendor' itself (VendorCmd): what a build's own 'vendor' task
# runs, and what the command line drives directly.

import getopt
import os
import subprocess
import sys
import tempfile
import threading
import time

from seine.cache_index import VENDOR, Index, say
from seine.cmd import Cmd
from seine.tasks import Task
from seine.container import ContainerEngine
from seine.utils import lock_sibling
from seine import settings
from seine import signing
from seine import snapshot
from seine import tasks as task_runner

from .fetch import (_artifact_key, _binary_already_fetched, _binary_has_gocode,
                    _dedup_binaries, _index_has_gocode, fetch_binary,
                    fetch_source, index)
from .manifest import (GRAPH_VERSION, _binary_file_path, _binary_hashes,
                       _cached_local_matches, _expand_binaries, _expand_files,
                       _file_hashes, _local_sha1, _lock_sources, _reverse_of,
                       _save_source_snapshot_cache, architectures, entries_for,
                       exclusions, extra_architectures, load_manifest,
                       manifest_digest, named_suites, parse, repository,
                       save_lock, save_manifest, unconfigured_suites)
from .resolve import VendorResolver


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
# repository (see fetch.py's own fetch_source()/fetch_binary()). An artifact
# already sitting there under its own name is not given a task at all:
# a source's exact files are known from the manifest's own 'files' (see
# RESOLVE_SCRIPT's _source_files()), and a binary's filename is a pure
# function of its name/arch/version (_binary_filename()), so this is
# decided on the host, before any container -- unlike apt's own
# skip-if-present check, which only happens once one has already been
# spawned to ask it.
def fetch_tasks(distro, suite, manifest, options, hostBootstrap, archs=None):
    # Qualified: tests replace what a fetch actually does by patching
    # 'seine.vendor._builder_for' (this package's own re-export), which a
    # bare name here would never see.
    from seine import vendor
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
            # A source whose entry already carries a 'snapshot' (only
            # ever set by '--refresh', into a committed lock -- see
            # _enrich_for_lock()) is fetched straight from
            # snapshot.debian.org, no builder/container at all: apt was
            # never going to have this exact version anyway (that is
            # the entire reason '--refresh' recorded it), and a plain
            # HTTPS download needs neither one. '_builder_for()' -- the
            # actual bootstrap/image-build chain -- is why this stays a
            # lambda rather than a plain call: it must not run at all
            # on this path, only inside the other one's body.
            snap = entry.get("snapshot")
            hashes = entry.get("file_hashes")
            tasks.append(Task(
                "fetch-src:%s:%s" % (suite, source),
                lambda distro=distro, suite=suite, source=source, version=version,
                       snap=snap, hashes=hashes:
                    fetch_source(
                        None if snap else vendor._builder_for(distro, suite, options, hostBootstrap),
                        suite, source, version, snapshot_hashes=snap,
                        expected_hashes=hashes, options=options)))
        for binpkg, arch, binver in _dedup_binaries(entry, seen_bins, archs):
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
            snap = entry.get("binary_snapshot", {}).get(binpkg, {}).get(arch)
            bin_hash = entry.get("binary_hashes", {}).get(binpkg, {}).get(arch)
            tasks.append(Task(
                "fetch-bin:%s:%s:%s" % (suite, binpkg, arch),
                lambda distro=distro, suite=suite, binpkg=binpkg, arch=arch,
                       binver=binver, snap=snap, bin_hash=bin_hash:
                    fetch_binary(
                        None if snap else vendor._builder_for(distro, suite, options, hostBootstrap),
                        suite, binpkg, arch, binver, snapshot_sha1=snap,
                        expected_hash=bin_hash, options=options)))
    return tasks

# One task per suite, run only once every one of that suite's fetches has
# already finished -- a separate, later 'tasks.run()' call rather than a
# 'needs' edge onto the fetch wave, so a failed fetch can be resubmitted
# (see VendorCmd._run_wave()) without the index task's own 'needs' naming
# an attempt that no longer exists under that name.
# 'manifests'/'entries' are VendorCmd._run()'s own -- index() (see its
# own comment) needs a suite's resolved sources explicitly, and the
# names 'entries_for(entries, suite)' asks for directly to seed
# main-vs-extra classification, rather than reading either off disk.
def index_tasks(distro, suites_wanted, options, hostBootstrap, signer,
                manifests, entries):
    # Qualified for the same reason fetch_tasks() above is.
    from seine import vendor
    tasks = []
    for suite in suites_wanted:
        direct = {e.name for e in entries_for(entries, suite)}
        tasks.append(Task(
            "index:%s" % suite,
            lambda distro=distro, suite=suite, sources=manifests[suite],
                   direct=direct:
                index(vendor._builder_for(distro, suite, options, hostBootstrap),
                     suite, signer, sources, direct)))
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
    LONG_OPTIONS = ["check", "debug", "help", "jobs=", "vendor-sign-key=",
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
        check = False
        for o, a in opts:
            if o in ("-d", "--debug"):
                self.options["debug"] = True
                self.options["verbose"] = True
            elif o in ("--check"):
                check = True
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

        # Writing a lock (a fresh '--refresh', or a '--check' comparing
        # against one) needs exactly one physical file to know which
        # '<file>.lock.yaml' it is about -- 'seine build's own multi-file
        # composition has no notion of "the" file a lock belongs to (see
        # [[seine-multiconfig-plan]]), so this is refused rather than
        # guessed at.
        if (refresh is not False or check) and len(args) != 1:
            sys.stderr.write(
                "error: %s needs exactly one specification file, to know "
                "which '<file>.lock.yaml' to write\n"
                % ("--refresh" if refresh is not False else "--check"))
            sys.exit(1)
        lock_path = lock_sibling(args[0]) if (refresh is not False or check) else None

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
            # 'load_lock()' expands a binary's own merged hash/version
            # and a source's own merged file hash/snapshot for their own
            # callers -- this is the *other* way a lock's data reaches
            # here, BuildCmd's own generic YAML/jinja loader
            # (_merge_vendor()'s dict branch), which never goes through
            # 'load_lock()' at all and so needs the exact same expansion
            # applied by hand.
            vendor_lock = {
                suite: dict(doc, sources=_expand_binaries(
                    _expand_files(doc.get("sources", {}))))
                for suite, doc in (build.spec.get("_vendor_lock") or {}).items()}
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
        # see manifest.py's own named_suites()/unconfigured_suites().
        unknown = unconfigured_suites(wanted, distro)
        if len(unknown) > 0:
            sys.stderr.write(
                "error: 'vendor:' asks for %s, which %s no configured feed "
                "-- add it under 'distribution: feeds:' first\n"
                % (", ".join(unknown), "has" if len(unknown) == 1 else "have"))
            sys.exit(3)

        try:
            sys.exit(self._run(distro, entries, exclude, wanted, refresh, archs,
                               extra_archs, vendor_lock=vendor_lock,
                               lock_path=lock_path, check=check))
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
             extra_archs=(), display=None, vendor_lock=None, lock_path=None,
             check=False):
        # Qualified: tests replace this by patching 'seine.vendor.
        # HostBootstrap' (see resolve.py's own comment on the same fix).
        from seine import vendor
        hostBootstrap = vendor.HostBootstrap(distro, self.options, force_online=True)
        vendor_lock = vendor_lock or {}

        # Which suites need a fresh resolve: every one of them when
        # '--refresh' was given with no name, one already-frozen entry
        # refreshed within an otherwise-kept manifest when given one,
        # every suite with no manifest yet at all, and every suite whose
        # frozen manifest no longer matches what this spec's own
        # 'vendor:' section (entries, excludes, profiles/options, feeds)
        # asks for -- see manifest_digest(). An ordinary, unchanged
        # rerun still just freezes what an earlier one resolved rather
        # than asking apt again for it. '--check' forces every suite
        # stale too, the same as '--refresh' -- it needs a real resolve
        # to compare against the lock, not whatever the cache already
        # has lying around.
        digests = {suite: manifest_digest(distro, entries, exclude, suite,
                                          extra_archs)
                  for suite in wanted}
        stale = []
        manifests = {}
        for suite in wanted:
            # A suite already frozen in a committed lock is never
            # resolved at all on an ordinary run -- no resolver
            # container, no apt: its sources are simply trusted, unless
            # the spec's own 'vendor:' section has moved since the lock
            # was last written, in which case this refuses outright
            # rather than silently drifting back to whatever apt
            # resolves today (that guarantee is the entire point of a
            # committed lock). '--refresh'/'--check' bypass this and
            # resolve for real, same as a suite with no lock at all.
            locked = vendor_lock.get(suite)
            if locked is not None and refresh is False and not check:
                if locked.get("digest") != digests[suite]:
                    raise ValueError(
                        "vendor lock for '%s' is out of date with this "
                        "specification's 'vendor:' section -- run 'seine "
                        "vendor --refresh' to update it" % suite)
                manifests[suite] = locked.get("sources", {})
                continue
            document = load_manifest(suite)
            manifest = document.get("sources", {})
            if (refresh is not False or check or len(manifest) == 0 or
                    document.get("digest") != digests[suite]):
                stale.append(suite)
            else:
                manifests[suite] = manifest

        if len(stale) > 0:
            results = {}
            resolve = vendor.resolve_tasks(distro, entries, stale, self.options,
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
                # '--check' writes nothing at all, cache manifest
                # included -- it exists purely to compare a fresh
                # resolve against the committed lock, never to update
                # anything on disk.
                if not check:
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

        # '--check' stops here: comparing the freshly resolved
        # sources/versions against the committed lock needs no fetch or
        # index at all, and the whole point is to touch nothing on disk.
        if check:
            return self._report_check(vendor_lock, manifests, wanted)

        fetch = []
        for suite in wanted:
            fetch += vendor.fetch_tasks(distro, suite, manifests[suite], self.options,
                                        hostBootstrap, archs)
        self._run_wave(fetch, retryable=True, display=display)

        signer = signing.vendor_signer(self.options)
        self._run_wave(
            vendor.index_tasks(distro, wanted, self.options, hostBootstrap, signer,
                               manifests, entries),
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

        # '--refresh' always writes (or creates) the lock beside the one
        # spec file it was given, whether or not one existed before --
        # bootstrapping a new lock is just the first refresh, same
        # command as updating an existing one (see docs). Every suite
        # the existing lock already named is kept as-is except the ones
        # this run actually touched ('wanted'), so a run scoped by
        # '--suite' never drops what an earlier run froze for another.
        if refresh is not False and lock_path is not None:
            updated = dict(vendor_lock)
            for suite in wanted:
                enriched = self._enrich_for_lock(suite, manifests[suite], display=display)
                # The cache manifest keeps the enrichment too (hashes,
                # any snapshot.debian.org URL found) -- read-modify-write
                # so its own 'digest'/'graph'/'graph_version' (already
                # written, above, right after resolving) survive
                # untouched; only 'sources' gains what this just found.
                document = load_manifest(suite)
                document["sources"] = enriched
                save_manifest(suite, document)
                updated[suite] = {"digest": digests[suite],
                                  "sources": _lock_sources(enriched)}
            save_lock(lock_path, updated)
            print("wrote %s" % lock_path)
        return 0

    # '--check': whether a fresh resolve of every wanted suite still
    # matches what the committed lock says, without writing anything
    # back either way. Compared by source name and version only --
    # binaries/build-deps follow from a source's own version already
    # (RESOLVE_SCRIPT resolves them together), and this is meant to
    # answer "did the archive move under us", not to restate the whole
    # manifest as a diff.
    def _report_check(self, vendor_lock, manifests, wanted):
        drifted = []
        for suite in wanted:
            old = (vendor_lock.get(suite) or {}).get("sources", {})
            new = manifests[suite]
            added = sorted(set(new) - set(old))
            removed = sorted(set(old) - set(new))
            changed = sorted(name for name in set(new) & set(old)
                             if old[name].get("version") != new[name].get("version"))
            if not (added or removed or changed):
                print("vendor lock for '%s' matches a fresh resolve" % suite)
                continue
            drifted.append(suite)
            print("vendor lock for '%s' has drifted from a fresh resolve:" % suite)
            for name in added:
                print("  + %s %s (not in the lock)" % (name, new[name]["version"]))
            for name in removed:
                print("  - %s %s (no longer resolved)" % (name, old[name].get("version")))
            for name in changed:
                print("  ~ %s: locked %s, resolved %s"
                     % (name, old[name].get("version"), new[name].get("version")))
        if drifted:
            sys.stderr.write(
                "error: vendor lock has drifted for %s\n" % ", ".join(drifted))
            return 1
        return 0

    # '--refresh's own enrichment of a suite's freshly-resolved sources --
    # the only place this runs (see seine/snapshot.py's own docstring
    # and fetch_source()/fetch_binary()'s use of what this records).
    # Builds exactly what both the cache manifest and the committed lock
    # want to carry: every file's sha256 (_file_hashes()/
    # _binary_hashes(), the lock's own no-deviation guarantee), plus --
    # when snapshot.debian.org already knows the exact bytes apt just
    # fetched -- its own sha1 for them ('snapshot'/'binary_snapshot'),
    # the path a plain 'seine vendor' reaches for once the live feed has
    # moved past this exact version.
    #
    # A source's own scale is thousands of packages, most with several
    # binaries each, and a lookup is a real network round trip against a
    # small shared public mirror -- run one at a time, this was by far
    # the longest, least parallel stretch of a whole '--refresh' (see
    # the prints right after 'vendored N source package(s)', which is
    # exactly where this picks up). Every already-cached hit (see
    # _cached_local_matches()'s own comment) is resolved on the spot,
    # cheaply, no network and no task; only an actual miss becomes a
    # Task, one per source (bundling all of that source's own missing
    # files into the single API call source_files() already makes for
    # the whole source) and one per (binpkg, version) (binary_files()
    # already returns every architecture in one call -- see its own
    # comment -- so archs sharing a version are batched into the same
    # task rather than each firing an identical query) -- run through
    # the exact same '_run_wave()' every fetch/resolve/index wave
    # already does, so '--jobs' governs this the same way, each task's
    # own 'made' line lands in its own log file instead of interleaving
    # on one terminal (see tasks.py's own 'output()' docstring). A task
    # that raises (a real request failure, not just "nothing found") is
    # left to propagate -- '_run_wave(retryable=True)' only ever retries
    # what it actually sees fail, so swallowing the error here the way
    # an earlier version did meant a transient failure was recorded as
    # a permanent "not on snapshot.debian.org" instead of being retried.
    #
    # 'snap_results'/'binary_snap_results' are the shared, task-body-
    # populated results tasks.py's own 'resolve_tasks()' already uses
    # this same way (see its own 'results' dict) -- every task writes to
    # its own key (a source's own name; a (name, binpkg, arch) triple),
    # never one another's, so nothing here needs a lock the way
    # '_run_wave()'s own 'failures' list does (that one is genuinely
    # shared across every task).
    #
    # Cross-checked against the sha1 snapshot.debian.org itself declares
    # for the name/version/filename -- a metadata comparison, not a
    # second download -- so a mismatch (some other upload sharing this
    # exact name/version, or the two disagreeing for any other reason)
    # is never recorded as if it were the same file: a warning, and the
    # entry is left without a 'snapshot' of its own, exactly like
    # snapshot.debian.org never having heard of it at all. Neither is an
    # error -- '--refresh' still succeeds, it just has nothing to fall
    # back to later for that one file.
    def _enrich_for_lock(self, suite, sources, display=None):
        where = repository(suite)
        options = self.options
        enriched = {}
        snap_results = {}          # name -> {fname: sha1}
        binary_snap_results = {}   # (name, binpkg, arch) -> sha1
        # The same (binpkg, arch) can legitimately be reachable from
        # more than one source's own 'binaries' entry (build-dep
        # closures overlap) -- fetch_tasks() already dedupes fetching
        # it twice the same way (its own 'seen_bins'); this is the
        # same guard for the snapshot lookup, found missing live: two
        # sources both queuing a 'snapshot-bin:<suite>:ecj:amd64' task
        # crashed task ordering outright ("duplicate task").
        seen_bins = set()
        tasks = []

        for name, entry in sources.items():
            entry = dict(entry)
            enriched[name] = entry
            if entry.get("files"):
                entry["file_hashes"] = _file_hashes(suite, entry)
                src_key = _artifact_key(suite, name, "source", None, entry["version"])
                local_hashes = {}
                for fname in entry["files"]:
                    path = os.path.join(where, fname)
                    if os.path.isfile(path):
                        local_hashes[fname] = _local_sha1(path)
                cached, missing = _cached_local_matches(src_key, local_hashes)
                if missing:
                    def run(name=name, version=entry["version"], src_key=src_key,
                            local_hashes=local_hashes, missing=missing, cached=cached):
                        sess = snapshot.session()
                        known = snapshot.source_files(sess, name, version)
                        for fname in missing:
                            local = local_hashes[fname]
                            # Every candidate snapshot.debian.org knows
                            # for this filename, not just the highest-
                            # priority one -- the same name/version can
                            # legitimately carry more than one upload
                            # under different bytes (see source_files()'s
                            # own comment), and only the locally fetched
                            # hash can say which one this actually is.
                            candidates = known.get(fname, [])
                            if any(h == local for h, _ in candidates):
                                cached[fname] = local
                            elif candidates:
                                print("warning: '%s' does not match any of "
                                     "snapshot.debian.org's own checksums for "
                                     "it -- not recording a snapshot URL" % fname)
                            # else: snapshot.debian.org has never heard of
                            # this filename at all -- not a mismatch, just
                            # nothing to fall back to yet.
                        _save_source_snapshot_cache(src_key, cached)
                        snap_results[name] = {fname: h for fname, h in cached.items()
                                              if local_hashes.get(fname) == h}
                        say(options, "vendor snapshot %s=%s made" % (name, version))
                    tasks.append(Task("snapshot-src:%s:%s" % (suite, name), run))
                else:
                    snap_results[name] = {fname: h for fname, h in cached.items()
                                          if local_hashes.get(fname) == h}
                    if local_hashes:
                        say(options, "vendor snapshot %s=%s reused"
                           % (name, entry["version"]))
            if entry.get("binaries"):
                entry["binary_hashes"] = _binary_hashes(suite, entry)
                groups = {}
                for binpkg, arch, version in _dedup_binaries(entry, seen_bins):
                    groups.setdefault((binpkg, version), []).append(arch)
                for (binpkg, version), archs_for in sorted(groups.items()):
                    need = {}   # arch -> (local sha1, cache key)
                    for arch in archs_for:
                        path = _binary_file_path(where, binpkg, arch, version)
                        if path is None:
                            continue
                        local = _local_sha1(path)
                        bin_key = _artifact_key(suite, binpkg, binpkg, arch, version)
                        hit = (Index().get(VENDOR, bin_key) or {}).get("snapshot_sha1")
                        if hit == local:
                            binary_snap_results[(name, binpkg, arch)] = local
                            say(options, "vendor snapshot %s:%s=%s reused"
                               % (binpkg, arch, version))
                        else:
                            need[arch] = (local, bin_key)
                    if not need:
                        continue
                    def run(name=name, binpkg=binpkg, version=version,
                            src_version=entry["version"], need=need):
                        sess = snapshot.session()
                        by_arch = snapshot.binary_files(
                            sess, name, src_version, binpkg, version)
                        for arch, (local, bin_key) in sorted(need.items()):
                            candidates = by_arch.get(arch, [])
                            if local in candidates:
                                binary_snap_results[(name, binpkg, arch)] = local
                                try:
                                    Index().patch(VENDOR, bin_key,
                                                 {"snapshot_sha1": local})
                                except Exception:
                                    pass
                                say(options, "vendor snapshot %s:%s=%s made"
                                   % (binpkg, arch, version))
                            elif candidates:
                                print("warning: '%s:%s' does not match any of "
                                     "snapshot.debian.org's own checksums for it "
                                     "-- not recording a snapshot URL"
                                     % (binpkg, arch))
                    tasks.append(Task(
                        "snapshot-bin:%s:%s:%s:%d"
                        % (suite, binpkg, version, len(tasks)), run))

        self._run_wave(tasks, retryable=True, display=display)

        for name, entry in enriched.items():
            snap = snap_results.get(name)
            if snap:
                entry["snapshot"] = snap
            if entry.get("binaries"):
                binary_snap = {}
                for binpkg, per_arch in entry["binaries"].items():
                    for arch in per_arch:
                        sha1 = binary_snap_results.get((name, binpkg, arch))
                        if sha1:
                            binary_snap.setdefault(binpkg, {})[arch] = sha1
                if binary_snap:
                    entry["binary_snapshot"] = binary_snap
        return enriched

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

  'SPEC.yaml' pairs with a committed 'SPEC.lock.yaml' if one sits beside
  it: loaded automatically, no field asks for it. A suite it names is
  never resolved at all -- trusted outright, unless this specification's
  own 'vendor:' section has changed since the lock was last written, in
  which case this refuses rather than silently drifting. '--refresh'
  needs exactly one SPEC and always (re)writes its lock; '--check' does
  the same resolve but only reports whether it still matches the lock,
  writing nothing either way -- what a CI job would run on a schedule.

Usage:
  seine vendor [-j N] [--refresh[=NAME] | --check] [--vendor-sign-key KEY]
               [--suite NAME]... [--architecture NAME]... SPEC...

Flags:
  --check               resolve fresh and compare against SPEC's own
                        lock file, without writing anything; exits
                        non-zero on drift. Needs exactly one SPEC
  -d, --debug           print what each step decided and its full output
  -h, --help            print this message
  -j, --jobs N          fetch up to N artifacts at once (1 by default)
      --vendor-sign-key KEY
                        sign the repository with this gpg key (or set
                        SEINE_VENDOR_SIGN_KEY), independent of the key
                        'packages:' rebuilds are signed with
      --refresh[=NAME]  resolve again rather than keep the frozen
                        manifest; scoped to one source package when
                        given a name, every one of them otherwise.
                        Needs exactly one SPEC, whose lock file this
                        then (re)writes
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
