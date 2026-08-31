#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import json
import os
import shutil
import subprocess
import sys
import tempfile

from unittest.mock import MagicMock, patch

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
# _run_wave() always makes a log directory now (ContainerEngine.logs_root()),
# unlike the cache: without this, a test exercising it -- RetryLoop, say --
# would leave one under this checkout's own ./build/logs instead.
os.environ["SEINE_LOG_DIR"] = tempfile.mkdtemp(prefix="seine-tests-logs-")
# index() now writes the repository it builds under deploy_repository(),
# not the cache -- without this, any test calling it (or _run_wave()'s
# own index wave) leaves real files under this checkout's own
# ./build/deploy instead.
os.environ["SEINE_DEPLOY_DIR"] = tempfile.mkdtemp(prefix="seine-tests-deploy-")
os.environ.pop("SEINE_SIGN_KEY", None)
os.environ.pop("SEINE_VENDOR_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_LOG_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_DEPLOY_DIR"], ignore_errors=True)

from seine import vendor, utils

def load(text):
    build = BuildCmd()
    build.loads(text)
    distro = utils.distribution(build.spec)
    return build.spec, distro

class SupportedEntries(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                vendor:
                    - name: openssl
                    - name: busybox
                      suite: bookworm
                      arch: [amd64, armhf]
                      version: ">=1.2"
        """)
        entries = vendor.parse(spec)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].name, "openssl")
        self.assertIsNone(entries[0].suites)
        self.assertIsNone(entries[0].architectures)
        self.assertEqual(entries[1].suites, ["bookworm"])
        self.assertEqual(entries[1].architectures, ["amd64", "armhf"])
        self.assertEqual(entries[1].version, ">=1.2")

class SuiteAndArchTakeAStringOrAList(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                vendor:
                    - name: busybox
                      suite: bookworm
                      arch: armhf
        """)
        entry = vendor.parse(spec)[0]
        self.assertEqual(entry.suites, ["bookworm"])
        self.assertEqual(entry.architectures, ["armhf"])

class NameIsRequired(avocado.Test):
    def test(self):
        try:
            vendor.parse({"vendor": [{"suite": "bookworm"}]})
            self.fail("an entry with no 'name' was accepted")
        except ValueError:
            pass

class NameIsNotASourcePackageName(avocado.Test):
    def test(self):
        try:
            vendor.parse({"vendor": [{"name": "Not Valid!"}]})
            self.fail("an invalid source package name was accepted")
        except ValueError:
            pass

class VendorIsNotAList(avocado.Test):
    def test(self):
        try:
            vendor.parse({"vendor": {"name": "busybox"}})
            self.fail("a non-list 'vendor' was accepted")
        except ValueError:
            pass

class VersionIsNotAString(avocado.Test):
    def test(self):
        try:
            vendor.parse({"vendor": [{"name": "busybox", "version": 1.2}]})
            self.fail("a non-string 'version' was accepted")
        except ValueError:
            pass

class EmptySuiteListIsRejected(avocado.Test):
    def test(self):
        try:
            vendor.parse({"vendor": [{"name": "busybox", "suite": []}]})
            self.fail("an empty 'suite' list was accepted")
        except ValueError:
            pass

class ExcludeIsAListOfStrings(avocado.Test):
    def test(self):
        self.assertEqual(vendor.exclusions({"vendor-exclude": ["gcc-12"]}),
                         ["gcc-12"])
        self.assertEqual(vendor.exclusions({}), [])
        try:
            vendor.exclusions({"vendor-exclude": "gcc-12"})
            self.fail("a non-list 'vendor-exclude' was accepted")
        except ValueError:
            pass

class UnqualifiedEntryAppliesToTheRelease(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                vendor:
                    - name: openssl
        """)
        entries = vendor.parse(spec)
        self.assertEqual(vendor.suites(entries, distro), ["bookworm"])
        self.assertEqual([e.name for e in vendor.entries_for(entries, "bookworm")],
                         ["openssl"])

class SuiteHasToBeAConfiguredFeed(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                vendor:
                    - name: openssl
                      suite: trixie
        """)
        try:
            vendor.suites(vendor.parse(spec), distro)
            self.fail("an unconfigured suite was accepted")
        except ValueError as e:
            self.assertIn("trixie", str(e))

class SuitesComeFromEveryConfiguredFeedAsked(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                        - suite: bookworm-security
                vendor:
                    - name: openssl
                    - name: busybox
                      suite: [bookworm, bookworm-security]
        """)
        entries = vendor.parse(spec)
        self.assertEqual(vendor.suites(entries, distro),
                         ["bookworm", "bookworm-security"])
        # 'openssl' is unqualified, so it applies here too -- alongside
        # 'busybox', which named this suite explicitly.
        self.assertEqual(
            sorted(e.name for e in vendor.entries_for(entries, "bookworm-security")),
            ["busybox", "openssl"])

class SuiteFlagOnlyValidatesFeedsItNeeds(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        spec_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False)
        self.addCleanup(os.unlink, spec_file.name)
        spec_file.write("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                vendor:
                    - name: openssl
                    - name: busybox
                      suite: trixie
        """)
        spec_file.close()

        cmd = VendorCmd()
        seen = {}
        cmd._run = lambda distro, entries, exclude, wanted, refresh: (
            seen.update(wanted=wanted) or 0)

        # 'trixie' has no configured feed, but '--suite bookworm' never
        # asks for it -- only what a run actually wants needs one.
        with self.assertRaises(SystemExit) as ctx:
            cmd.main(["--suite", "bookworm", spec_file.name])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(seen["wanted"], ["bookworm"])

        # Asking for the unconfigured suite still fails, and only then.
        with self.assertRaises(SystemExit) as ctx:
            cmd.main(["--suite", "trixie", spec_file.name])
        self.assertEqual(ctx.exception.code, 3)

class ArchitecturesUnionEveryEntry(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                    architecture: amd64
                vendor:
                    - name: openssl
                    - name: busybox
                      arch: [armhf]
        """)
        entries = vendor.parse(spec)
        self.assertEqual(vendor.architectures(entries, distro), ["amd64", "armhf"])

class ArchitecturesForNarrowsToWhatWasAsked(avocado.Test):
    def test(self):
        entry = vendor.VendorPackage({"name": "busybox", "arch": ["armhf", "arm64"]}, 1)
        self.assertEqual(entry.architectures_for(["amd64", "armhf"]), ["armhf"])
        unqualified = vendor.VendorPackage({"name": "openssl"}, 1)
        self.assertEqual(unqualified.architectures_for(["amd64", "armhf"]),
                         ["amd64", "armhf"])

class ImageParseValidatesVendorEagerly(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                vendor:
                    - name: Not-Valid!
                image:
                    filename: vendor-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.parse()
            self.fail("a bad 'vendor:' section was not caught at parse time")
        except ValueError as e:
            self.assertIn("vendor", str(e))

class SuiteRepositoryPathsAreKeptApart(avocado.Test):
    def test(self):
        bookworm = vendor.repository("bookworm")
        trixie = vendor.repository("trixie")
        self.assertNotEqual(bookworm, trixie)
        self.assertTrue(os.path.isdir(bookworm))
        self.assertTrue(os.path.isdir(trixie))

# apt-get download keeps a binary's epoch, ':' escaped as '%3a' -- unlike
# a source's .dsc/.orig.tar.*, which never carries one. Stripping it here
# made _binary_already_fetched() miss every already-fetched epoch-carrying
# binary (golang-github-akavel-rsrc-dev and friends), so 'seine vendor'
# re-fetched them (a fast no-op apt already had cached, but still a
# needless task and a misleading "to fetch" every run) on every run.
class BinaryFilenameKeepsAnEpochEscaped(avocado.Test):
    def test(self):
        self.assertEqual(
            vendor._binary_filename("golang-github-akavel-rsrc-dev", "amd64", "1:0.10.2-1"),
            "golang-github-akavel-rsrc-dev_1%3a0.10.2-1_amd64.deb")

    def test_no_epoch_is_untouched(self):
        self.assertEqual(
            vendor._binary_filename("serf", "amd64", "1.3.10-3"),
            "serf_1.3.10-3_amd64.deb")

# The %3a-encoded name is checked first, but a file already on disk under
# the old, epoch-stripped name (from before this fix, or some apt that
# names it that way) still counts as fetched -- it must not be refetched
# just because the check now prefers a different spelling.
class BinaryAlreadyFetchedFallsBackToTheLegacyEpochlessName(avocado.Test):
    def test(self):
        where = self.workdir
        open(os.path.join(where, "golang-github-akavel-rsrc-dev_0.10.2-1_all.deb"), "w").close()
        self.assertTrue(vendor._binary_already_fetched(
            where, "golang-github-akavel-rsrc-dev", "amd64", "1:0.10.2-1"))

# fetch_source()'s own use of 'apt-get source', by a fake builder
# recording what it was asked to exec() -- the same disambiguation
# build_deps() needs (see ResolveScriptBuildDepsCollectsInstAndConf's own
# 'src:' test), for the same reason: a source package sharing its name
# with an unrelated binary is otherwise fetched as if it were that
# binary's own source instead.
class FetchSourceMarksTheNameAsASourcePackage(avocado.Test):
    def test(self):
        class FakeBuilder:
            options = {}
            def exec(self, args, **kwargs):
                self.args = args
        builder = FakeBuilder()
        vendor.fetch_source(builder, "fetch-test-%d" % os.getpid(), "serf", "1.3.10-3")
        self.assertIn("src:serf=1.3.10-3", builder.args)

class ManifestRoundTrips(avocado.Test):
    def test(self):
        suite = "manifest-test-%d" % os.getpid()
        self.assertEqual(vendor.load_manifest(suite), {})
        vendor.save_manifest(suite, {"sources": {"openssl": {"version": "3.0"}}})
        self.assertEqual(vendor.load_manifest(suite),
                         {"sources": {"openssl": {"version": "3.0"}}})

    # The 'graph' field save_manifest()/load_manifest() carry alongside
    # 'sources' -- both untouched, so this is really about VendorCmd._run()
    # always writing one, not the pass-through functions themselves.
    def test_graph_and_graph_version_round_trip(self):
        suite = "manifest-graph-test-%d" % os.getpid()
        graph = {"edges": [], "reverse": {},
                 "pruned": {"base_chroot": [], "excluded": []}}
        vendor.save_manifest(suite, {"sources": {}, "digest": "d",
                                     "graph": graph,
                                     "graph_version": vendor.GRAPH_VERSION})
        document = vendor.load_manifest(suite)
        self.assertEqual(document["graph"], graph)
        self.assertEqual(document["graph_version"], vendor.GRAPH_VERSION)

    # A manifest a pre-graph 'seine vendor' wrote has no 'graph' key at
    # all -- a reader has to ask for it with .get(), not [], the same as
    # a manifest missing 'digest' already does.
    def test_a_manifest_without_a_graph_still_loads(self):
        suite = "manifest-no-graph-test-%d" % os.getpid()
        vendor.save_manifest(suite, {"sources": {"openssl": {"version": "3.0"}}})
        document = vendor.load_manifest(suite)
        self.assertIsNone(document.get("graph"))

class SuiteDistroKeepsEveryConfiguredFeed(avocado.Test):
    # Renamed for this suite, but not narrowed to its own feed alone: a
    # build-dependency closure needs the same consistent view of the
    # archive a real build's chroot already gets, or a package whose
    # runtime library was bumped by a security update, while its own
    # '-dev' headers stay pinned to the base pocket's exact version by
    # an '=' build-dep, resolves to nothing apt can install -- found by
    # 'seine vendor' actually failing on 'git' this way, vendoring
    # nothing but the base 'trixie' pocket.
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                        - suite: bookworm-security
                          release: bookworm
                          uri: http://security.debian.org/debian-security
        """)
        security = vendor._suite_distro(distro, "bookworm-security")
        self.assertEqual(security["release"], "bookworm-security")
        self.assertEqual(len(security["feeds"]), 2)
        self.assertEqual(security["feeds"], distro["feeds"])

    # feeds() is asked, not re-derived: distro['feeds'] is left exactly
    # as it arrived, in its raw 'feeds:' shape, so nothing downstream
    # re-parsing it (apt_sources(), a container's own dockerfile()) ever
    # has to make sense of feeds()'s own parsed-and-resolved return shape
    # ('valid_until', an underscore, already True/False) instead.
    def test_the_feeds_reparse_cleanly(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                    uri: https://snapshot.debian.org/archive/debian/20260801T000000Z
                    feeds:
                        - suite: bookworm
                          valid-until: false
        """)
        renamed = vendor._suite_distro(distro, "bookworm")
        self.assertEqual(utils.apt_sources(renamed), [
            "deb [check-valid-until=no] "
            "https://snapshot.debian.org/archive/debian/20260801T000000Z "
            "bookworm main"])

# The regression this guards: a specification pinning both a trixie
# release and a handful of 'suite: bookworm' entries (examples/vendor/
# main.yaml) must not let trixie's own resolver quietly pick a build-dep
# out of bookworm just because both are configured -- that produced
# spurious re-fetches (a package resolving to a different candidate
# version each time bookworm's own feed config changed) and, worse, a
# resolved closure that could actually depend on a release the built
# image never installs from.
class SuiteDistroExcludesAnotherRelease(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: trixie
                    feeds:
                        - suite: trixie
                        - suite: trixie-security
                          release: trixie
                        - suite: bookworm
        """)
        trixie = vendor._suite_distro(distro, "trixie")
        self.assertEqual(sorted(f["suite"] for f in trixie["feeds"]),
                         ["trixie", "trixie-security"])
        bookworm = vendor._suite_distro(distro, "bookworm")
        self.assertEqual([f["suite"] for f in bookworm["feeds"]], ["bookworm"])

# Grouping is an explicit 'release:' tag (utils.feeds()'s own comment),
# never a guess off the suite's own name: two feeds sharing a name prefix
# stay apart unless one of them actually says so.
class UntaggedPocketsAreNotGuessedIntoAGroup(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: trixie
                    feeds:
                        - suite: trixie
                        - suite: trixie-updates
        """)
        trixie = vendor._suite_distro(distro, "trixie")
        self.assertEqual([f["suite"] for f in trixie["feeds"]], ["trixie"])

class ManifestDigestIgnoresAnotherReleasesFeed(avocado.Test):
    def test(self):
        from seine.vendor import manifest_digest, parse
        distro = {"source": "debian", "release": "trixie", "architecture": "amd64",
                  "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}, {"suite": "bookworm"}]}
        entries = parse({"vendor": [{"name": "openssl"}]})
        moved = dict(distro, feeds=[{"suite": "trixie"},
                                    {"suite": "bookworm",
                                     "uri": "http://elsewhere.example/debian"}])
        self.assertEqual(
            manifest_digest(distro, entries, [], "trixie"),
            manifest_digest(moved, entries, [], "trixie"))
        self.assertNotEqual(
            manifest_digest(distro, entries, [], "bookworm"),
            manifest_digest(moved, entries, [], "bookworm"))

class NoFeedForASuiteIsRejected(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
        """)
        try:
            vendor._suite_distro(distro, "trixie")
            self.fail("a suite with no configured feed was accepted")
        except ValueError:
            pass

# The task graph built for wave 1/2/3, without touching podman: none of
# resolve_tasks()/fetch_tasks()/index_tasks() run a task's own body just
# by being called -- see vendor._builder_for(), only reached from inside
# a Task's own 'run'.
class TaskGraphShape(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}, {"suite": "bookworm-security"}]}

    def test_resolve_tasks_need_the_host_bootstrap(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        entries = vendor.parse({"vendor": [
            {"name": "openssl"},
            {"name": "busybox", "suite": ["bookworm-security"]},
        ]})
        results = {}
        tasks = vendor.resolve_tasks(
            distro, entries, ["bookworm", "bookworm-security"], {},
            HostBootstrap(distro, {}), [], results)
        names = sorted(t.name for t in tasks)
        self.assertEqual(names, ["resolve:bookworm", "resolve:bookworm-security"])
        for task in tasks:
            self.assertEqual(task.needs, ["bootstrap-host"])

    def test_fetch_tasks_are_independent(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        manifest = {
            "openssl": {"version": "3.0.11-1", "direct": True,
                       "binaries": {"libssl3": {"amd64": "3.0.11-1"}}},
        }
        tasks = vendor.fetch_tasks(distro, "bookworm", manifest, {},
                                   HostBootstrap(distro, {}))
        names = sorted(t.name for t in tasks)
        self.assertEqual(names,
                         ["fetch-bin:bookworm:libssl3:amd64",
                          "fetch-src:bookworm:openssl"])
        for task in tasks:
            self.assertEqual(task.needs, [])

    def test_fetch_tasks_skip_a_source_whose_files_are_all_there(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        suite = "fetch-skip-test-%d" % os.getpid()
        where = vendor.repository(suite)
        for name in ["openssl_3.0.11-1.dsc", "openssl_3.0.11.orig.tar.xz"]:
            open(os.path.join(where, name), "w").close()
        try:
            manifest = {
                "openssl": {"version": "3.0.11-1", "direct": True,
                           "binaries": {}, "files": [
                               "openssl_3.0.11-1.dsc",
                               "openssl_3.0.11.orig.tar.xz"]},
            }
            tasks = vendor.fetch_tasks(distro, suite, manifest, {},
                                       HostBootstrap(distro, {}))
            self.assertEqual(tasks, [])
            from seine import cache_index
            entries = cache_index.Index().entries()
            self.assertTrue(any(kind == cache_index.VENDOR and
                                key.startswith("%s_openssl_source_" % suite)
                                for kind, key, _ in entries))
        finally:
            shutil.rmtree(where, ignore_errors=True)

    def test_fetch_tasks_still_fetch_a_source_missing_one_file(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        suite = "fetch-skip-test-%d" % os.getpid()
        where = vendor.repository(suite)
        open(os.path.join(where, "openssl_3.0.11-1.dsc"), "w").close()
        try:
            manifest = {
                "openssl": {"version": "3.0.11-1", "direct": True,
                           "binaries": {}, "files": [
                               "openssl_3.0.11-1.dsc",
                               "openssl_3.0.11.orig.tar.xz"]},
            }
            tasks = vendor.fetch_tasks(distro, suite, manifest, {},
                                       HostBootstrap(distro, {}))
            self.assertEqual([t.name for t in tasks],
                             ["fetch-src:%s:openssl" % suite])
        finally:
            shutil.rmtree(where, ignore_errors=True)

    def test_fetch_tasks_skip_a_binary_already_there(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        suite = "fetch-skip-test-%d" % os.getpid()
        where = vendor.repository(suite)
        open(os.path.join(where, "libssl3_3.0.11-1_amd64.deb"), "w").close()
        try:
            manifest = {
                "openssl": {"version": "3.0.11-1", "direct": True,
                           "binaries": {"libssl3": {"amd64": "3.0.11-1"}},
                           "files": []},
            }
            tasks = vendor.fetch_tasks(distro, suite, manifest, {},
                                       HostBootstrap(distro, {}))
            # No 'files' for the source, so it is still fetched -- only
            # the binary, whose filename is known outright, is skipped.
            self.assertEqual([t.name for t in tasks],
                             ["fetch-src:%s:openssl" % suite])
        finally:
            shutil.rmtree(where, ignore_errors=True)

    # An 'Architecture: all' binary is still resolved (and fetched) per
    # requested arch, but apt names the file it writes after the
    # package's own architecture -- 'all', never 'amd64' -- so the skip
    # check has to look for that name too, not just the requested one.
    def test_fetch_tasks_skip_an_architecture_all_binary(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        suite = "fetch-skip-test-%d" % os.getpid()
        where = vendor.repository(suite)
        open(os.path.join(where, "dh-acc_2.3-3_all.deb"), "w").close()
        try:
            manifest = {
                "abi-compliance-checker": {
                    "version": "2.3-3", "direct": True, "files": [],
                    "binaries": {"dh-acc": {"amd64": "2.3-3"}}},
            }
            tasks = vendor.fetch_tasks(distro, suite, manifest, {},
                                       HostBootstrap(distro, {}))
            self.assertEqual(
                [t.name for t in tasks],
                ["fetch-src:%s:abi-compliance-checker" % suite])
        finally:
            shutil.rmtree(where, ignore_errors=True)

    def test_index_tasks_one_per_suite(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        tasks = vendor.index_tasks(distro, ["bookworm", "bookworm-security"], {},
                                   HostBootstrap(distro, {}), None)
        self.assertEqual(sorted(t.name for t in tasks),
                         ["index:bookworm", "index:bookworm-security"])

# _run_wave()'s own retry loop, with fake task bodies rather than real
# fetches -- what is under test is the retry/renaming logic, not apt.
class RetryLoop(avocado.Test):
    def cmd(self, jobs=1):
        from seine.vendor import VendorCmd
        cmd = VendorCmd()
        cmd.options["jobs"] = jobs
        cmd.options["verbose"] = True
        return cmd

    def test_a_flaky_task_succeeds_on_retry(self):
        from seine.tasks import Task
        cmd = self.cmd()
        attempts = {"n": 0}
        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ValueError("boom")
        cmd._run_wave([Task("fetch-bin:x:amd64", flaky)], retryable=True)
        self.assertEqual(attempts["n"], 2)

    def test_exhausting_retries_raises(self):
        from seine.tasks import Task, Failed
        cmd = self.cmd()
        def always_fails():
            raise ValueError("still broken")
        try:
            cmd._run_wave([Task("fetch-bin:y:amd64", always_fails)], retryable=True)
            self.fail("exhausted retries did not raise")
        except Failed as e:
            self.assertTrue(any(n.startswith("fetch-bin:y:amd64#")
                                for n, _ in e.failures))

    def test_a_non_retryable_wave_raises_immediately(self):
        from seine.tasks import Task
        cmd = self.cmd()
        calls = {"n": 0}
        def always_fails():
            calls["n"] += 1
            raise ValueError("nope")
        try:
            cmd._run_wave([Task("resolve:bookworm", always_fails)], retryable=False)
            self.fail("did not raise")
        except ValueError:
            pass
        self.assertEqual(calls["n"], 1)

    def test_siblings_run_regardless_of_one_failure_even_with_jobs_1(self):
        from seine.tasks import Task, Failed
        cmd = self.cmd(jobs=1)
        calls = {"good": 0}
        def good():
            calls["good"] += 1
        def bad():
            raise ValueError("bad")
        try:
            cmd._run_wave([Task("fetch-bin:good1:amd64", good),
                           Task("fetch-bin:bad:amd64", bad),
                           Task("fetch-bin:good2:amd64", good)], retryable=True)
            self.fail("did not raise after exhausting retries")
        except Failed:
            pass
        self.assertEqual(calls["good"], 2)

class RefreshTakenOutOfArgvByHand(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        cmd = VendorCmd()
        refresh, remaining = cmd._take_refresh(["--refresh", "-j", "4", "spec.yaml"])
        self.assertEqual(refresh, True)
        self.assertEqual(remaining, ["-j", "4", "spec.yaml"])
        refresh, remaining = cmd._take_refresh(["--refresh=openssl", "spec.yaml"])
        self.assertEqual(refresh, "openssl")
        self.assertEqual(remaining, ["spec.yaml"])
        refresh, remaining = cmd._take_refresh(["spec.yaml"])
        self.assertEqual(refresh, False)
        self.assertEqual(remaining, ["spec.yaml"])

class RefreshWithANameKeepsEveryOtherEntryFrozen(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        cmd = VendorCmd()
        merged = cmd._merge_refresh(
            {"a": {"version": "1"}, "b": {"version": "2"}},
            {"a": {"version": "1new"}, "b": {"version": "2new"},
             "c": {"version": "3new"}},
            "b")
        self.assertEqual(merged, {"a": {"version": "1"}, "b": {"version": "2new"},
                                  "c": {"version": "3new"}})

# _merge_refresh_graph()'s own version of the same rule: a source not in
# 'moved' keeps its old edges/pruned rows untouched, one that is takes
# the freshly-resolved ones -- and 'reverse' always reflects whichever
# edges actually survived the split, never the old run's own reverse map.
class RefreshWithANameMergesTheGraphTheSameWay(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        cmd = VendorCmd()
        old = {"edges": [{"from": "a", "to": "shared", "via": "libshared-dev",
                          "arch": "amd64", "field": "Build-Depends",
                          "raw": "libshared-dev", "depth": 0}],
              "pruned": {"base_chroot": [{"source": "a", "name": "gcc",
                                          "arch": "amd64", "field": "Build-Depends",
                                          "reason": "already in sbuild chroot"}],
                        "excluded": []}}
        fresh = {"edges": [{"from": "b", "to": "shared", "via": "libshared-dev",
                            "arch": "amd64", "field": "Build-Depends",
                            "raw": "libshared-dev (>= 3.0)", "depth": 0}],
                "pruned": {"base_chroot": [], "excluded": []}}
        graph = cmd._merge_refresh_graph(old, fresh, moved={"b"})
        self.assertEqual(graph["edges"], old["edges"] + fresh["edges"])
        self.assertEqual(graph["pruned"], {
            "base_chroot": old["pruned"]["base_chroot"], "excluded": []})
        self.assertEqual(
            {row["parent"] for row in graph["reverse"]["shared"]}, {"a", "b"})

# The resolve wave is what builds the host bootstrap image, as one of its
# own tasks -- so a run touching only suites whose manifest is already
# frozen has to build it itself, or the fetch/index containers standing
# on it fail the moment podman's own image storage does not already have
# it (a plain 'seine cache clear images' is all that takes).
class HostBootstrapIsBuiltEvenWhenNothingIsStale(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.vendor import VendorCmd, save_manifest, manifest_digest

        digest = manifest_digest(self.distro(), [], [], "bookworm")
        save_manifest("bookworm", {"sources": {
            "openssl": {"version": "3.0.11-1", "direct": True, "binaries": {}}},
            "digest": digest})

        created = {"n": 0}
        class FakeHostBootstrap:
            def __init__(self, distro, options):
                pass
            def create(self):
                created["n"] += 1
            def task(self):
                raise AssertionError(
                    "task() should not be needed when nothing is stale")

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable: None
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap):
            cmd._run(self.distro(), [], [], ["bookworm"], False)
        self.assertEqual(created["n"], 1)

# manifest_digest() -- what decides whether a frozen manifest still
# matches the spec that would resolve it.
class ManifestDigestChangesWithWhatWouldResolveDifferently(avocado.Test):
    def distro(self, **over):
        base = {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}
        return dict(base, **over)

    def test_stable_when_nothing_relevant_changed(self):
        from seine.vendor import manifest_digest, parse
        entries = parse({"vendor": [{"name": "openssl"}]})
        self.assertEqual(
            manifest_digest(self.distro(), entries, [], "bookworm"),
            manifest_digest(self.distro(), entries, [], "bookworm"))

    def test_changes_when_entries_change(self):
        from seine.vendor import manifest_digest, parse
        before = parse({"vendor": [{"name": "openssl"}]})
        after = parse({"vendor": [{"name": "openssl"}, {"name": "busybox"}]})
        self.assertNotEqual(
            manifest_digest(self.distro(), before, [], "bookworm"),
            manifest_digest(self.distro(), after, [], "bookworm"))

    def test_changes_when_exclude_changes(self):
        from seine.vendor import manifest_digest, parse
        entries = parse({"vendor": [{"name": "openssl"}]})
        self.assertNotEqual(
            manifest_digest(self.distro(), entries, [], "bookworm"),
            manifest_digest(self.distro(), entries, ["gcc-12"], "bookworm"))

    def test_changes_when_a_feed_moves(self):
        from seine.vendor import manifest_digest, parse
        entries = parse({"vendor": [{"name": "openssl"}]})
        moved = self.distro(feeds=[{"suite": "bookworm",
                                    "uri": "http://elsewhere.example/debian"}])
        self.assertNotEqual(
            manifest_digest(self.distro(), entries, [], "bookworm"),
            manifest_digest(moved, entries, [], "bookworm"))

# _run()'s own staleness decision: a manifest that is present and
# non-empty, with no '--refresh' given, is still resolved again once the
# spec no longer matches the digest it was frozen with.
class AChangedSpecReresolvesWithoutRefresh(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.vendor import VendorCmd, save_manifest, manifest_digest

        save_manifest("bookworm", {"sources": {
            "openssl": {"version": "3.0.11-1", "direct": True, "binaries": {}}},
            "digest": "stale-digest-from-a-different-spec"})

        resolved = {"suites": None}
        def fake_resolve_tasks(distro, entries, suites_wanted, options,
                              hostBootstrap, exclude, results):
            resolved["suites"] = list(suites_wanted)
            results["bookworm"] = (
                {}, {"edges": [], "reverse": {},
                    "pruned": {"base_chroot": [], "excluded": []}})
            return []

        class FakeHostBootstrap:
            def __init__(self, distro, options):
                pass
            def create(self):
                pass
            def task(self):
                return None

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable: None
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor.resolve_tasks", fake_resolve_tasks):
            cmd._run(self.distro(), [], [], ["bookworm"], False)
        self.assertEqual(resolved["suites"], ["bookworm"])
        self.assertEqual(
            vendor.load_manifest("bookworm").get("digest"),
            manifest_digest(self.distro(), [], [], "bookworm"))

# index()'s own rebuild-from-scratch of pool/: the actual regression this
# was written against -- a package reclassified between two runs must
# end up in only the component the *current* manifest names, never left
# behind in the one an earlier run put it in.
class IndexRebuildsPoolFromTheCurrentManifest(avocado.Test):
    class FakeBuilder:
        options = {}
        def exec(self, args, **kwargs):
            pass

    def test_reclassifying_a_source_moves_it_between_components(self):
        from seine.vendor import index, save_manifest, repository, deploy_repository

        suite = "index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        for name in ["openssl_3.0.11-1.dsc", "openssl_3.0.11.orig.tar.xz"]:
            open(os.path.join(fetched, name), "w").close()
        try:
            save_manifest(suite, {"sources": {
                "openssl": {"version": "3.0.11-1", "direct": True, "binaries": {}}}})
            index(self.FakeBuilder(), suite, None)
            self.assertTrue(os.path.isfile(
                os.path.join(deployed, "pool", "main", "openssl_3.0.11-1.dsc")))
            self.assertFalse(os.path.isfile(
                os.path.join(deployed, "pool", "extra", "openssl_3.0.11-1.dsc")))

            save_manifest(suite, {"sources": {
                "openssl": {"version": "3.0.11-1", "direct": False, "binaries": {}}}})
            index(self.FakeBuilder(), suite, None)
            self.assertFalse(os.path.isfile(
                os.path.join(deployed, "pool", "main", "openssl_3.0.11-1.dsc")))
            self.assertTrue(os.path.isfile(
                os.path.join(deployed, "pool", "extra", "openssl_3.0.11-1.dsc")))
            # Hardlinked, not copied: the flat, durable cache copy is
            # untouched.
            self.assertTrue(os.path.isfile(
                os.path.join(fetched, "openssl_3.0.11-1.dsc")))
        finally:
            shutil.rmtree(fetched, ignore_errors=True)
            shutil.rmtree(deployed, ignore_errors=True)

    # A package name that is a hyphenated prefix of a sibling's (as
    # 'lib0install-solver-ocaml' is of 'lib0install-solver-ocaml-dev')
    # must not have the sibling's own file linked in under its name --
    # doing so used to crash the second package's own link attempt
    # outright (os.link() found the destination already there, and the
    # shutil.copy2() fallback then raised over linking a file to itself).
    def test_a_package_name_that_prefixes_a_sibling_does_not_swallow_it(self):
        from seine.vendor import index, save_manifest, repository, deploy_repository

        suite = "index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        for name in ["lib0install-solver-ocaml_2.18-1+b4_amd64.deb",
                    "lib0install-solver-ocaml-dev_2.18-1+b4_amd64.deb"]:
            open(os.path.join(fetched, name), "w").close()
        try:
            save_manifest(suite, {"sources": {"0install-solver": {
                "version": "2.18-1", "direct": True, "binaries": {
                    "lib0install-solver-ocaml": {"amd64": "2.18-1+b4"},
                    "lib0install-solver-ocaml-dev": {"amd64": "2.18-1+b4"}}}}})
            index(self.FakeBuilder(), suite, None)
            self.assertTrue(os.path.isfile(os.path.join(
                deployed, "pool", "main",
                "lib0install-solver-ocaml_2.18-1+b4_amd64.deb")))
            self.assertTrue(os.path.isfile(os.path.join(
                deployed, "pool", "main",
                "lib0install-solver-ocaml-dev_2.18-1+b4_amd64.deb")))
        finally:
            shutil.rmtree(fetched, ignore_errors=True)
            shutil.rmtree(deployed, ignore_errors=True)

    # A source and one of its own binaries sharing a name (as
    # 'abi-compliance-checker' names both) means _link_fetched() is
    # asked for the same prefix twice over -- the second call must find
    # its file already linked and leave it alone, not treat that as a
    # reason to fall back to copying a file onto itself.
    def test_a_source_and_a_same_named_binary_link_without_colliding(self):
        from seine.vendor import index, save_manifest, repository, deploy_repository

        suite = "index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        for name in ["abi-compliance-checker_2.3-3.dsc",
                    "abi-compliance-checker_2.3.orig.tar.gz",
                    "abi-compliance-checker_2.3-3.debian.tar.xz",
                    "abi-compliance-checker_2.3-3_all.deb"]:
            open(os.path.join(fetched, name), "w").close()
        try:
            save_manifest(suite, {"sources": {"abi-compliance-checker": {
                "version": "2.3-3", "direct": True,
                "files": ["abi-compliance-checker_2.3-3.dsc",
                         "abi-compliance-checker_2.3.orig.tar.gz",
                         "abi-compliance-checker_2.3-3.debian.tar.xz"],
                "binaries": {
                    "abi-compliance-checker": {"amd64": "2.3-3"}}}}})
            index(self.FakeBuilder(), suite, None)
            for name in ["abi-compliance-checker_2.3-3.dsc",
                        "abi-compliance-checker_2.3.orig.tar.gz",
                        "abi-compliance-checker_2.3-3.debian.tar.xz",
                        "abi-compliance-checker_2.3-3_all.deb"]:
                self.assertTrue(os.path.isfile(
                    os.path.join(deployed, "pool", "main", name)))
        finally:
            shutil.rmtree(fetched, ignore_errors=True)
            shutil.rmtree(deployed, ignore_errors=True)

# A builder that actually runs index()'s own shell script through 'sh',
# rather than faking it out -- catches what a FakeBuilder recording
# arguments never can: whether the script itself is something apt-
# ftparchive accepts. 'volumes'/'workdir' are podman's own vocabulary for
# what is really just "run this in 'where'", so they are ignored in
# favour of passing 'cwd' straight to subprocess.
class RealShellBuilder:
    options = {}
    def exec(self, args, volumes=None, workdir=None, **kwargs):
        where = (volumes or [(None, None)])[0][0]
        subprocess.run(args, cwd=where, check=True)

def _build_deb(name, version, where):
    staging = tempfile.mkdtemp(prefix="seine-tests-deb-")
    try:
        os.makedirs(os.path.join(staging, "DEBIAN"))
        with open(os.path.join(staging, "DEBIAN", "control"), "w") as f:
            f.write("Package: %s\nVersion: %s\nArchitecture: amd64\n"
                    "Maintainer: t <t@t>\nDescription: t\n" % (name, version))
        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", staging,
             os.path.join(where, "%s_%s_amd64.deb" % (name, version))],
            check=True, capture_output=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

# The actual regression this guards: 'apt-ftparchive packages DIR1 DIR2'
# reads its second argument as an override *file*, not a second
# directory -- so the flat backward-compat Packages/Sources this builds
# out of 'pool/main pool/extra' silently dropped 'extra' entirely (and
# errored trying to fgets() a directory as a file) the moment nothing
# was left redirecting that error into /dev/null and falling back to a
# recursive '.' scan to paper over it. 'pool' alone, recursed into by
# apt-ftparchive on its own, is what actually has to be asked for.
class IndexBuildsAFlatListingCoveringBothComponents(avocado.Test):
    def test(self):
        from seine.vendor import index, save_manifest, repository, deploy_repository

        suite = "index-real-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        try:
            _build_deb("mainpkg", "1.0", fetched)
            _build_deb("extrapkg", "2.0", fetched)
            save_manifest(suite, {"sources": {
                "mainpkg": {"version": "1.0", "direct": True,
                           "files": [], "binaries": {
                               "mainpkg": {"amd64": "1.0"}}},
                "extrapkg": {"version": "2.0", "direct": False,
                            "files": [], "binaries": {
                                "extrapkg": {"amd64": "2.0"}}},
            }})
            index(RealShellBuilder(), suite, None)
            with open(os.path.join(deployed, "Packages")) as f:
                flat = f.read()
            self.assertIn("Package: mainpkg", flat)
            self.assertIn("Package: extrapkg", flat)
        finally:
            shutil.rmtree(fetched, ignore_errors=True)
            shutil.rmtree(deployed, ignore_errors=True)

# RESOLVE_SCRIPT's main() -- the apt_pkg-driven closure walk that runs
# inside the resolve container. Exercised here by extracting it into its
# own namespace (real dpkg version comparison and apt_pkg dependency
# parsing kept, apt.Cache/apt_pkg.SourceRecords/subprocess.run faked out
# in place of a live container's package caches).
def resolve_namespace():
    from seine.vendor import RESOLVE_SCRIPT
    body = RESOLVE_SCRIPT.rsplit("try:\n    main()", 1)[0]
    ns = {}
    exec(compile(body, "<vendor-resolve>", "exec"), ns)
    return ns

# A source stanza, as apt_pkg.SourceRecords would report it: fields plus
# the raw record text main() feeds to apt_pkg.TagSection() for Build-Depends.
def source_stanza(name, version, binaries, build_depends="", files=None):
    record = "Package: %s\nVersion: %s\nBuild-Depends: %s\n" % (
        name, version, build_depends)
    if files:
        record += "Checksums-Sha256:\n"
        for fname in files:
            record += " %s 100 %s\n" % ("0" * 64, fname)
    record += "\n"
    return {"package": name, "version": version, "binaries": binaries,
            "record": record}

class FakeSourceRecords:
    def __init__(self, stanzas):
        self._stanzas = stanzas
        self._pos = 0

    def restart(self):
        self._pos = 0

    def lookup(self, name):
        while self._pos < len(self._stanzas):
            stanza = self._stanzas[self._pos]
            self._pos += 1
            if stanza["package"] == name:
                self.package = stanza["package"]
                self.version = stanza["version"]
                self.binaries = stanza["binaries"]
                self.record = stanza["record"]
                return True
        return False

# A binary package, as apt.Cache()[name] would report it: only the bit
# main() reads off its candidate version.
class FakeBinary:
    def __init__(self, version, source_name=None):
        self.candidate = MagicMock(version=version,
                                   source_name=source_name or "")

class FakeCache(dict):
    pass

# The plumbing every resolve_namespace() test shares: run main() with a
# faked apt.Cache/apt_pkg.SourceRecords/subprocess against a request, and
# return the response.json it wrote.
def run_resolve(request, stanzas, binaries):
    ns = resolve_namespace()
    mount = tempfile.mkdtemp(prefix="seine-tests-resolve-")
    try:
        ns["MOUNT"] = mount
        with open(os.path.join(mount, "request.json"), "w") as f:
            json.dump(request, f)
        with patch("subprocess.run"), \
             patch("apt_pkg.init_config"), \
             patch("apt_pkg.init_system"), \
             patch("apt.Cache", return_value=FakeCache(binaries)), \
             patch("apt_pkg.SourceRecords",
                  return_value=FakeSourceRecords(stanzas)):
            ns["main"]()
        with open(os.path.join(mount, "response.json")) as f:
            return json.load(f)
    finally:
        shutil.rmtree(mount, ignore_errors=True)

class ResolveWalksTheBuildDepClosure(avocado.Test):
    def test(self):
        stanzas = [
            source_stanza("foo", "1.0-1", ["foo-bin"],
                          build_depends="libbar-dev"),
            source_stanza("bar", "2.0-1", ["libbar-dev"]),
        ]
        binaries = {
            "foo-bin": FakeBinary("1.0-1", source_name="foo"),
            "libbar-dev": FakeBinary("2.0-1", source_name="bar"),
        }
        request = {"archs": ["amd64"], "exclude": [], "base_chroot": {},
                  "build_profiles": [], "build_options": [],
                  "sources": [{"name": "foo", "version": None}]}
        response = run_resolve(request, stanzas, binaries)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["warnings"], [])
        self.assertEqual(response["sources"]["foo"]["direct"], True)
        self.assertEqual(response["sources"]["foo"]["binaries"],
                         {"foo-bin": {"amd64": "1.0-1"}})
        # 'bar' is only there because 'foo' build-depends on one of its
        # binaries -- pulled in as an extra (direct=False), not asked
        # for directly.
        self.assertEqual(response["sources"]["bar"]["direct"], False)
        self.assertEqual(response["sources"]["bar"]["binaries"],
                         {"libbar-dev": {"amd64": "2.0-1"}})
        # The graph records the same closure as a 'foo' -(Build-Depends)->
        # 'libbar-dev' -(candidate.source_name)-> 'bar' edge, with a
        # reverse row 'vendor-why bar' can read straight off.
        graph = response["graph"]
        self.assertEqual(graph["edges"], [
            {"from": "foo", "to": "bar", "via": "libbar-dev", "arch": "amd64",
             "field": "Build-Depends", "raw": "libbar-dev", "depth": 0}])
        self.assertEqual(graph["reverse"], {
            "bar": [{"parent": "foo", "via": "libbar-dev", "arch": "amd64",
                    "field": "Build-Depends", "depth": 0}]})
        self.assertEqual(graph["pruned"], {"base_chroot": [], "excluded": []})

# seen_bins/queued_bins dedup the fetch queue (a binary is only fetched
# once); build_dep_bins must not inherit that dedup -- index()'s gocode
# closure walks each source's own build_dep_bins, so a binary two sources
# both build-depend on has to be named by both, not just whichever the
# resolve BFS reaches first.
class ResolveRecordsBuildDepBinsPerSourceNotJustFirstDiscoverer(avocado.Test):
    def test(self):
        stanzas = [
            source_stanza("a", "1.0-1", ["a-bin"],
                          build_depends="libshared-dev"),
            source_stanza("b", "1.0-1", ["b-bin"],
                          build_depends="libshared-dev"),
            source_stanza("shared", "2.0-1", ["libshared-dev"]),
        ]
        binaries = {
            "a-bin": FakeBinary("1.0-1", source_name="a"),
            "b-bin": FakeBinary("1.0-1", source_name="b"),
            "libshared-dev": FakeBinary("2.0-1", source_name="shared"),
        }
        request = {"archs": ["amd64"], "exclude": [], "base_chroot": {},
                  "build_profiles": [], "build_options": [],
                  "sources": [{"name": "a", "version": None},
                              {"name": "b", "version": None}]}
        response = run_resolve(request, stanzas, binaries)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertIn("libshared-dev", response["sources"]["a"]["build_dep_bins"])
        self.assertIn("libshared-dev", response["sources"]["b"]["build_dep_bins"])

# A resolved source's own 'files' -- what lets fetch_tasks() know, by
# name alone, whether it already has every file a source package needs
# without spawning anything to ask apt.
class ResolveRecordsASourcesOwnFiles(avocado.Test):
    def test(self):
        stanzas = [
            source_stanza("foo", "1.0-1", ["foo-bin"],
                          files=["foo_1.0-1.dsc", "foo_1.0.orig.tar.gz",
                                "foo_1.0-1.debian.tar.xz"]),
        ]
        binaries = {"foo-bin": FakeBinary("1.0-1", source_name="foo")}
        request = {"archs": ["amd64"], "exclude": [], "base_chroot": {},
                  "build_profiles": [], "build_options": [],
                  "sources": [{"name": "foo", "version": None}]}
        response = run_resolve(request, stanzas, binaries)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["sources"]["foo"]["files"],
                         ["foo_1.0-1.debian.tar.xz", "foo_1.0-1.dsc",
                          "foo_1.0.orig.tar.gz"])

    def test_empty_when_a_stanza_names_none(self):
        stanzas = [source_stanza("foo", "1.0-1", ["foo-bin"])]
        binaries = {"foo-bin": FakeBinary("1.0-1", source_name="foo")}
        request = {"archs": ["amd64"], "exclude": [], "base_chroot": {},
                  "build_profiles": [], "build_options": [],
                  "sources": [{"name": "foo", "version": None}]}
        response = run_resolve(request, stanzas, binaries)
        self.assertEqual(response["sources"]["foo"]["files"], [])

class ResolveHonoursBuildProfiles(avocado.Test):
    def test_nocheck_drops_a_test_only_build_dep(self):
        stanzas = [
            source_stanza("foo", "1.0-1", ["foo-bin"],
                          build_depends="libbar-dev <!nocheck>"),
            source_stanza("bar", "2.0-1", ["libbar-dev"]),
        ]
        binaries = {
            "foo-bin": FakeBinary("1.0-1", source_name="foo"),
            "libbar-dev": FakeBinary("2.0-1", source_name="bar"),
        }
        request = {"archs": ["amd64"], "exclude": [], "base_chroot": {},
                  "build_profiles": ["nocheck"], "build_options": [],
                  "sources": [{"name": "foo", "version": None}]}
        response = run_resolve(request, stanzas, binaries)
        self.assertTrue(response["ok"], response.get("error"))
        # 'bar' only exists to satisfy a <!nocheck> build-dep: with
        # 'nocheck' set, it should never be queued at all.
        self.assertNotIn("bar", response["sources"])

class ResolveGraphRecordsWhyABuildDepWasPruned(avocado.Test):
    def test_base_chroot_and_exclude_both_short_circuit_before_an_edge(self):
        stanzas = [
            source_stanza("foo", "1.0-1", ["foo-bin"],
                          build_depends="gcc, doc-package"),
        ]
        binaries = {"foo-bin": FakeBinary("1.0-1", source_name="foo")}
        request = {"archs": ["amd64"], "exclude": ["doc-package"],
                  "base_chroot": {"amd64": ["gcc"]},
                  "build_profiles": [], "build_options": [],
                  "sources": [{"name": "foo", "version": None}]}
        response = run_resolve(request, stanzas, binaries)
        self.assertTrue(response["ok"], response.get("error"))
        # Neither ever became a source of its own, nor an edge -- both
        # were pruned before 'foo's own build-dep loop got that far.
        self.assertNotIn("gcc", response["sources"])
        self.assertNotIn("doc-package", response["sources"])
        graph = response["graph"]
        self.assertEqual(graph["edges"], [])
        self.assertEqual(graph["reverse"], {})
        self.assertEqual(graph["pruned"]["base_chroot"], [
            {"source": "foo", "name": "gcc", "arch": "amd64",
             "field": "Build-Depends", "reason": "already in sbuild chroot"}])
        self.assertEqual(graph["pruned"]["excluded"], [
            {"source": "foo", "name": "doc-package", "arch": "amd64",
             "field": "Build-Depends", "reason": "excluded"}])

class ResolveWarnsAboutAnUnknownSource(avocado.Test):
    def test(self):
        request = {"archs": ["amd64"], "exclude": [], "base_chroot": {},
                  "build_profiles": [], "build_options": [],
                  "sources": [{"name": "ghost", "version": None}]}
        response = run_resolve(request, stanzas=[], binaries={})
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["sources"], {})
        self.assertEqual(response["warnings"],
                         ["'ghost' is not in this suite"])

class NewResolverArchFiltering(avocado.Test):
    def test_bash_armel_only_for_armel(self):
        import apt_pkg
        # parse_src_depends filters [armel] correctly
        s = "bash [armel], foo"
        # For amd64, only foo should remain
        res_amd64 = apt_pkg.parse_src_depends(s, False, "amd64")
        names_amd64 = [dep[0] for grp in res_amd64 for dep in grp]
        self.assertIn("foo", names_amd64)
        self.assertNotIn("bash", names_amd64)
        # For armel, both
        res_armel = apt_pkg.parse_src_depends(s, False, "armel")
        names_armel = [dep[0] for grp in res_armel for dep in grp]
        self.assertIn("bash", names_armel)
        self.assertIn("foo", names_armel)

class NewResolverProfileFiltering(avocado.Test):
    def test_nocheck_profile(self):
        import apt_pkg
        s = "foo <!nocheck>, bar"
        # Without nocheck profile, foo is included
        apt_pkg.config.set("APT::Build-Profiles", "")
        res = apt_pkg.parse_src_depends(s, False, "amd64")
        names = [dep[0] for grp in res for dep in grp]
        self.assertIn("foo", names)
        # With nocheck, foo is excluded
        apt_pkg.config.set("APT::Build-Profiles", "nocheck")
        res2 = apt_pkg.parse_src_depends(s, False, "amd64")
        names2 = [dep[0] for grp in res2 for dep in grp]
        self.assertNotIn("foo", names2)
        self.assertIn("bar", names2)
        apt_pkg.config.set("APT::Build-Profiles", "")
