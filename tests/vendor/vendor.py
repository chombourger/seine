#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import io
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
# VendorCmd.__init__() now reads settings.load() for its own 'jobs'
# default (the same fallback BuildCmd.__init__() already had) -- without
# this, every 'VendorCmd()' constructed below reads whatever a real
# '~/.config/seine/settings.json' on the machine running these actually
# says, instead of the deterministic default every other test here (and
# tests/tui/tui.py/ai.py, for the exact same reason) already assumes.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-tests-config-")
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_LOG_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_DEPLOY_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["XDG_CONFIG_HOME"], ignore_errors=True)

from seine import vendor, settings, utils

def load(text):
    build = BuildCmd()
    build.loads(text)
    distro = utils.distribution(build.spec)
    return build.spec, distro

# VendorCmd's own jobs default -- BuildCmd's twin (tests/build/build.py's
# own DefaultJobCount): 1 unless a persisted setting overrides it, an
# explicit -j/--jobs still winning either way.
class DefaultJobCount(avocado.Test):
    def setUp(self):
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_one_with_no_settings_file(self):
        self.assertEqual(vendor.VendorCmd().options["jobs"], 1)

    def test_the_persisted_value_otherwise(self):
        current = settings.load()
        current["jobs"] = 3
        settings.save(current)
        self.assertEqual(vendor.VendorCmd().options["jobs"], 3)

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

class ExtraArchitectureIsAListOfStrings(avocado.Test):
    def test(self):
        self.assertEqual(
            vendor.extra_architectures(
                {"distribution": {"architectures": ["arm64"]}}),
            ["arm64"])
        self.assertEqual(vendor.extra_architectures({}), [])
        try:
            vendor.extra_architectures(
                {"distribution": {"architectures": "arm64"}})
            self.fail("a non-list 'distribution: architectures:' was accepted")
        except ValueError:
            pass

class ArchitecturesUnionsInExtraArchitecturesToo(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    architectures:
                        - arm64
                vendor:
                    - name: openssl
        """)
        entries = vendor.parse(spec)
        extra = vendor.extra_architectures(spec)
        self.assertEqual(vendor.architectures(entries, distro, extra),
                         ["amd64", "arm64"])
        # Without the extra, only the base architecture -- confirms the
        # widening comes from 'distribution: architectures:', not from
        # 'openssl' itself (unqualified, so it never names an architecture).
        self.assertEqual(vendor.architectures(entries, distro), ["amd64"])

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

# named_suites() -- an unqualified entry means every release 'feeds:'
# configures, not distro['release'] alone: examples/vendor/main.yaml
# carries no 'distribution: release:' of its own on purpose (a build
# merging it after an image spec would otherwise clobber that image's
# own release), and still has to vendor both bookworm and trixie by
# default when run standalone.
class UnqualifiedEntriesMeanEveryConfiguredRelease(avocado.Test):
    def test(self):
        spec, distro = load("""
                distribution:
                    feeds:
                        - suite: bookworm
                        - suite: trixie
                vendor:
                    - name: openssl
                    - name: curl
                      suite: trixie
        """)
        entries = vendor.parse(spec)
        self.assertEqual(vendor.named_suites(entries, distro),
                         ["bookworm", "trixie"])

    # distro['release'] defaulting to something with no feed configured
    # here at all (utils.distribution()'s own fallback) never leaks into
    # what an unqualified entry names -- only 'trixie' is configured, so
    # the fallback showing up in the result would mean this regressed.
    def test_unset_release_default_is_never_named(self):
        spec, distro = load("""
                distribution:
                    feeds:
                        - suite: trixie
                vendor:
                    - name: openssl
        """)
        entries = vendor.parse(spec)
        self.assertNotEqual(distro["release"], "trixie")
        self.assertEqual(vendor.named_suites(entries, distro), ["trixie"])

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
        cmd._run = lambda distro, entries, exclude, wanted, refresh, archs=None, \
                          extra_archs=(), **kwargs: (
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

class ArchitectureFlagNarrowsFetchingAlone(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        spec_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False)
        self.addCleanup(os.unlink, spec_file.name)
        spec_file.write("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    feeds:
                        - suite: bookworm
                vendor:
                    - name: openssl
                    - name: busybox
                      arch: [armhf]
        """)
        spec_file.close()

        cmd = VendorCmd()
        seen = {}
        cmd._run = lambda distro, entries, exclude, wanted, refresh, archs=None, \
                          extra_archs=(), **kwargs: (
            seen.update(archs=archs) or 0)

        # No '--architecture' at all: unscoped, same as before this flag
        # existed.
        with self.assertRaises(SystemExit) as ctx:
            cmd.main([spec_file.name])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIsNone(seen["archs"])

        # '--architecture armhf' is one 'vendor:' asks for (via
        # 'busybox's own 'arch:'), so it is accepted and threaded through.
        with self.assertRaises(SystemExit) as ctx:
            cmd.main(["--architecture", "armhf", spec_file.name])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(seen["archs"], ["armhf"])

        # 'arm64' is not among what 'vendor:' asks for (amd64, from
        # 'distribution:', and armhf, from 'busybox's own 'arch:') --
        # rejected before ever reaching '_run()'.
        with self.assertRaises(SystemExit) as ctx:
            cmd.main(["--architecture", "arm64", spec_file.name])
        self.assertEqual(ctx.exception.code, 1)

# 'distribution: architectures:' reaches the CLI the same way 'vendor:'
# entries' own 'arch:' does -- both fold into main()'s own
# available_archs, and both reach '_run()' as 'extra_archs'.
class DistributionArchitecturesWidensWhatArchitectureAccepts(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        spec_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False)
        self.addCleanup(os.unlink, spec_file.name)
        spec_file.write("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    architectures:
                        - arm64
                    feeds:
                        - suite: bookworm
                vendor:
                    - name: openssl
        """)
        spec_file.close()

        cmd = VendorCmd()
        seen = {}
        cmd._run = lambda distro, entries, exclude, wanted, refresh, archs=None, \
                          extra_archs=(), **kwargs: (
            seen.update(archs=archs, extra_archs=extra_archs) or 0)

        # 'arm64' is accepted even though no entry names it -- only
        # 'distribution: architectures:' does.
        with self.assertRaises(SystemExit) as ctx:
            cmd.main(["--architecture", "arm64", spec_file.name])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(seen["archs"], ["arm64"])

        # And unconditionally reaches '_run()' as 'extra_archs', whether
        # or not '--architecture' narrowed anything.
        self.assertEqual(seen["extra_archs"], ["arm64"])

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

    # 'extra_archs' (vendor.extra_architectures()'s own return) has to
    # reach the resolver same as an entry's own 'arch:' does -- otherwise
    # 'distribution: architectures:' would validate at the CLI (main()'s
    # own available_archs) without ever actually being resolved for.
    def test_resolve_tasks_fold_extra_archs_into_the_closure(self):
        import inspect
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        entries = vendor.parse({"vendor": [{"name": "openssl"}]})
        results = {}
        tasks = vendor.resolve_tasks(
            distro, entries, ["bookworm"], {},
            HostBootstrap(distro, {}), [], results, extra_archs=["arm64"])
        archs = inspect.signature(tasks[0].run).parameters["archs"].default
        self.assertEqual(archs, ["amd64", "arm64"])

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

    # 'archs' scopes which binaries fetch_tasks() bothers with -- the
    # source itself is unaffected, since a suite's manifest already
    # decided (at resolve time, over every architecture 'vendor:' asks
    # for) that this source belongs at all.
    def test_fetch_tasks_narrowed_to_one_architecture(self):
        from seine.bootstrap import HostBootstrap
        distro = self.distro()
        manifest = {
            "openssl": {"version": "3.0.11-1", "direct": True,
                       "binaries": {"libssl3": {"amd64": "3.0.11-1",
                                                "armhf": "3.0.11-1"}}},
        }
        tasks = vendor.fetch_tasks(distro, "bookworm", manifest, {},
                                   HostBootstrap(distro, {}), ["amd64"])
        names = sorted(t.name for t in tasks)
        self.assertEqual(names,
                         ["fetch-bin:bookworm:libssl3:amd64",
                          "fetch-src:bookworm:openssl"])

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
        manifests = {"bookworm": {}, "bookworm-security": {}}
        tasks = vendor.index_tasks(distro, ["bookworm", "bookworm-security"], {},
                                   HostBootstrap(distro, {}), None, manifests, [])
        self.assertEqual(sorted(t.name for t in tasks),
                         ["index:bookworm", "index:bookworm-security"])

# 'seine build' resolving 'vendor:' itself, ahead of 'packages:', when an
# offline suite actually needs it -- Image._vendor_task()/shared_tasks().
# None of these touch podman: a Task's own body is a deferred callable,
# never run just by building the graph (see TaskGraphShape's own header).
class ImageWiresVendorAheadOfPackages(avocado.Test):
    def build(self, offline):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    uri: http://example.com/debian
                    %s
                    feeds:
                        - suite: bookworm
                vendor:
                    - name: openssl
                packages:
                    - source: apt://busybox
                image:
                    filename: vendor-wiring-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """ % ("apt-pull-mode: offline" if offline else ""))
        build.parse()
        return build

    def test_a_vendor_task_is_added_when_offline_needs_it(self):
        steps = self.build(offline=True).image.tasks()
        self.assertIn("vendor", [t.name for t in steps])

    # A pre-existing deploy/vendor/bookworm (an index() run left behind,
    # here or on another machine) is trusted outright, not reresolved,
    # refetched or reindexed -- so there is nothing left for a 'vendor'
    # task to do, and none is added at all.
    def test_no_vendor_task_when_the_repository_is_already_deployed(self):
        from seine import vendor
        open(os.path.join(vendor.deploy_repository("bookworm"), "Packages"),
            "w").close()
        steps = self.build(offline=True).image.tasks()
        self.assertNotIn("vendor", [t.name for t in steps])

    def test_no_vendor_task_online(self):
        steps = self.build(offline=False).image.tasks()
        self.assertNotIn("vendor", [t.name for t in steps])

    def test_packages_prepare_waits_on_vendor(self):
        steps = self.build(offline=True).image.tasks()
        prepare = next(t for t in steps if t.name == "packages-prepare")
        self.assertIn("vendor", prepare.needs)

    # Narrowed to 'bookworm' (the release built) even though 'vendor:' and
    # 'apt-pull-mode: offline' both also cover 'bookworm-security'.
    def test_vendor_task_is_narrowed_to_the_release_being_built(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    uri: http://example.com/debian
                    apt-pull-mode: offline
                    feeds:
                        - suite: bookworm
                        - suite: bookworm-security
                vendor:
                    - name: openssl
                    - name: curl
                      suite: bookworm-security
                packages:
                    - source: apt://busybox
                image:
                    filename: vendor-wiring-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        vendor_task = next(t for t in build.image.tasks() if t.name == "vendor")
        wanted = vendor_task.run.args[3]
        self.assertEqual(wanted, ["bookworm"])

    # Fetching is narrowed to this build's own architecture, like 'seine
    # vendor --architecture amd64' -- 'vendor:' asking for 'armhf' too is
    # none of this build's business.
    def test_vendor_task_is_narrowed_to_the_architecture_being_built(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    uri: http://example.com/debian
                    apt-pull-mode: offline
                    feeds:
                        - suite: bookworm
                vendor:
                    - name: openssl
                    - name: curl
                      arch: [armhf]
                packages:
                    - source: apt://busybox
                image:
                    filename: vendor-wiring-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        vendor_task = next(t for t in build.image.tasks() if t.name == "vendor")
        self.assertEqual(vendor_task.run.keywords["archs"], ["amd64"])

    # Same narrowing, but the other suite is a wholly different release
    # ('trixie', not a pocket of 'bookworm') with its own feed -- confirms
    # only the release actually being built is selected, not every release
    # 'vendor:'/'apt-pull-mode: offline' together cover.
    def test_vendor_task_ignores_a_different_release_also_configured(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    uri: http://example.com/debian
                    apt-pull-mode: offline
                    feeds:
                        - suite: bookworm
                        - suite: trixie
                vendor:
                    - name: openssl
                    - name: curl
                      suite: trixie
                packages:
                    - source: apt://busybox
                image:
                    filename: vendor-wiring-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        vendor_task = next(t for t in build.image.tasks() if t.name == "vendor")
        wanted = vendor_task.run.args[3]
        self.assertEqual(wanted, ["bookworm"])

    def test_a_vendor_section_nothing_offline_needs_adds_no_task(self):
        # 'vendor:' with no 'apt-pull-mode: offline' behind it is still
        # only ever built by a plain 'seine vendor' -- nothing here would
        # read it, so 'seine build' leaves it alone.
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    architecture: amd64
                    uri: http://example.com/debian
                    feeds:
                        - suite: bookworm
                vendor:
                    - name: openssl
                image:
                    filename: vendor-wiring-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        self.assertNotIn("vendor", [t.name for t in build.image.tasks()])

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
class VendorBootstrapIsBuiltEvenWhenNothingIsStale(avocado.Test):
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

        from seine.tasks import Task

        created = {"n": 0}
        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def create(self):
                created["n"] += 1
            def task(self):
                return Task("bootstrap-host", self.create)

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        # Only the bootstrap-only wave actually runs its task -- fetch/
        # index stay no-ops, same as before, not this test's concern
        # (and 'openssl' has no real files to fetch here). Still run as
        # a one-task wave even with nothing else stale (see _run()'s own
        # comment), so a caller with its own display never sees this
        # step as invisible on an otherwise ordinary rerun.
        seen_display = {}
        def fake_run_wave(wave_tasks, retryable, display=None):
            for task in wave_tasks:
                if task.name == "bootstrap-host":
                    seen_display["display"] = display
                    if display is not None:
                        display.started(task.name)
                    task.run()
                    if display is not None:
                        display.finished(task.name, failed=False)
        cmd._run_wave = fake_run_wave
        # A spy 'display', the same shape TextualReporter is -- proves
        # _run() actually threads it into the bootstrap-only wave, not
        # just that the wave itself still runs the task.
        calls = []
        class SpyDisplay:
            def started(self, name):
                calls.append(("started", name))
            def finished(self, name, failed=False):
                calls.append(("finished", name, failed))
            def say(self, text):
                pass
        spy = SpyDisplay()
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap):
            cmd._run(self.distro(), [], [], ["bookworm"], False, display=spy)
        self.assertEqual(created["n"], 1)
        self.assertIs(seen_display["display"], spy)
        self.assertEqual(calls, [("started", "bootstrap-host"),
                                 ("finished", "bootstrap-host", False)])

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

    # A spec newly naming 'distribution: architectures:' has to
    # re-resolve -- otherwise a manifest frozen before it was asked for
    # would stay frozen without arm64, forever, since nothing else about
    # the spec changed.
    def test_changes_when_extra_archs_change(self):
        from seine.vendor import manifest_digest, parse
        entries = parse({"vendor": [{"name": "openssl"}]})
        self.assertNotEqual(
            manifest_digest(self.distro(), entries, [], "bookworm"),
            manifest_digest(self.distro(), entries, [], "bookworm", ["arm64"]))

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
                              hostBootstrap, exclude, results,
                              extra_archs=()):
            resolved["suites"] = list(suites_wanted)
            results["bookworm"] = (
                {}, {"edges": [], "reverse": {},
                    "pruned": {"base_chroot": [], "excluded": []}})
            return []

        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def create(self):
                pass
            def task(self):
                return None

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable, display=None: None
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
        from seine.vendor import index, repository, deploy_repository

        suite = "index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        for name in ["openssl_3.0.11-1.dsc", "openssl_3.0.11.orig.tar.xz"]:
            open(os.path.join(fetched, name), "w").close()
        try:
            sources = {"openssl": {"version": "3.0.11-1", "binaries": {}}}
            index(self.FakeBuilder(), suite, None, sources, {"openssl"})
            self.assertTrue(os.path.isfile(
                os.path.join(deployed, "pool", "main", "openssl_3.0.11-1.dsc")))
            self.assertFalse(os.path.isfile(
                os.path.join(deployed, "pool", "extra", "openssl_3.0.11-1.dsc")))

            # No longer among the directly-asked names -- classification
            # is decided fresh from 'direct' every call, never carried
            # over from a previous one.
            index(self.FakeBuilder(), suite, None, sources, set())
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
        from seine.vendor import index, repository, deploy_repository

        suite = "index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        for name in ["lib0install-solver-ocaml_2.18-1+b4_amd64.deb",
                    "lib0install-solver-ocaml-dev_2.18-1+b4_amd64.deb"]:
            open(os.path.join(fetched, name), "w").close()
        try:
            sources = {"0install-solver": {
                "version": "2.18-1", "binaries": {
                    "lib0install-solver-ocaml": {"amd64": "2.18-1+b4"},
                    "lib0install-solver-ocaml-dev": {"amd64": "2.18-1+b4"}}}}
            index(self.FakeBuilder(), suite, None, sources, {"0install-solver"})
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
        from seine.vendor import index, repository, deploy_repository

        suite = "index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        for name in ["abi-compliance-checker_2.3-3.dsc",
                    "abi-compliance-checker_2.3.orig.tar.gz",
                    "abi-compliance-checker_2.3-3.debian.tar.xz",
                    "abi-compliance-checker_2.3-3_all.deb"]:
            open(os.path.join(fetched, name), "w").close()
        try:
            sources = {"abi-compliance-checker": {
                "version": "2.3-3",
                "files": ["abi-compliance-checker_2.3-3.dsc",
                         "abi-compliance-checker_2.3.orig.tar.gz",
                         "abi-compliance-checker_2.3-3.debian.tar.xz"],
                "binaries": {
                    "abi-compliance-checker": {"amd64": "2.3-3"}}}}
            index(self.FakeBuilder(), suite, None, sources,
                 {"abi-compliance-checker"})
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
        from seine.vendor import index, repository, deploy_repository

        suite = "index-real-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        try:
            _build_deb("mainpkg", "1.0", fetched)
            _build_deb("extrapkg", "2.0", fetched)
            sources = {
                "mainpkg": {"version": "1.0",
                           "files": [], "binaries": {
                               "mainpkg": {"amd64": "1.0"}}},
                "extrapkg": {"version": "2.0",
                            "files": [], "binaries": {
                                "extrapkg": {"amd64": "2.0"}}},
            }
            index(RealShellBuilder(), suite, None, sources, {"mainpkg"})
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
# return the response.json it wrote. 'binaries' is keyed by plain binary
# name, same as callers write it -- also aliased here under every
# 'name:arch' resolve() actually looks up a per-arch candidate through
# (real apt.Cache resolves those independently; this fixture has only one
# FakeBinary per name, so every arch in the request sees the same version).
def run_resolve(request, stanzas, binaries):
    ns = resolve_namespace()
    mount = tempfile.mkdtemp(prefix="seine-tests-resolve-")
    try:
        ns["MOUNT"] = mount
        with open(os.path.join(mount, "request.json"), "w") as f:
            json.dump(request, f)
        cache = dict(binaries)
        for name, binpkg in binaries.items():
            for arch in request.get("archs", []):
                cache.setdefault("%s:%s" % (name, arch), binpkg)
        with patch("subprocess.run"), \
             patch("apt_pkg.init_config"), \
             patch("apt_pkg.init_system"), \
             patch("apt.Cache", return_value=FakeCache(cache)), \
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

# ---------------------------------------------------------------------
# The committed lock file: kas-style auto-discovery ('foo.yaml' pairs
# with 'foo.lock.yaml'), 'vendor:' as a dict rather than a list once it
# comes from one, and the guardrails around trusting it.
# ---------------------------------------------------------------------

class LockSiblingNaming(avocado.Test):
    def test(self):
        self.assertEqual(utils.lock_sibling("foo.yaml"), "foo.lock.yaml")
        self.assertEqual(utils.lock_sibling("dir/foo.yml"), "dir/foo.lock.yml")
        self.assertEqual(utils.lock_sibling("foo"), "foo.lock.yaml")
        # A lock file does not get a lock of its own.
        self.assertIsNone(utils.lock_sibling("foo.lock.yaml"))

class MergeVendorDictGoesToVendorLock(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                vendor:
                    - name: openssl
        """)
        build.loads("""
                vendor:
                    bookworm:
                        digest: abc123
                        sources:
                            openssl:
                                version: "3.0.11-1"
        """)
        # The declared ask is untouched -- every existing reader of
        # 'vendor:' (parse(), exclusions(), ...) still sees a plain list.
        self.assertEqual(build.spec["vendor"], [{"name": "openssl"}])
        self.assertEqual(build.spec["_vendor_lock"]["bookworm"]["digest"],
                         "abc123")
        self.assertEqual(
            build.spec["_vendor_lock"]["bookworm"]["sources"]["openssl"]["version"],
            "3.0.11-1")

class LoadAllSplicesSiblingLockFile(avocado.Test):
    def test(self):
        spec_dir = tempfile.mkdtemp(prefix="seine-tests-lock-")
        self.addCleanup(shutil.rmtree, spec_dir, ignore_errors=True)
        spec_path = os.path.join(spec_dir, "foo.yaml")
        lock_path = os.path.join(spec_dir, "foo.lock.yaml")
        with open(spec_path, "w") as f:
            f.write("vendor:\n    - name: openssl\n")
        with open(lock_path, "w") as f:
            f.write("vendor:\n    bookworm:\n        digest: abc123\n"
                    "        sources: {}\n")

        build = BuildCmd()
        build.load_all([spec_path])
        self.assertEqual(build.spec["vendor"], [{"name": "openssl"}])
        self.assertEqual(build.spec["_vendor_lock"]["bookworm"]["digest"],
                         "abc123")

        # No sibling: nothing extra happens, same as before this existed.
        spec_path2 = os.path.join(spec_dir, "bare.yaml")
        with open(spec_path2, "w") as f:
            f.write("vendor:\n    - name: git\n")
        build2 = BuildCmd()
        build2.load_all([spec_path2])
        self.assertNotIn("_vendor_lock", build2.spec)

class VendorLockSuiteIsTrustedWithoutResolving(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.vendor import VendorCmd, manifest_digest

        digest = manifest_digest(self.distro(), [], [], "bookworm")
        locked_sources = {"openssl": {"version": "3.0.11-1", "direct": True,
                                      "binaries": {}}}
        vendor_lock = {"bookworm": {"digest": digest, "sources": locked_sources}}

        resolve_called = []
        def fake_resolve_tasks(*a, **k):
            resolve_called.append(True)
            return []

        fetched = {}
        def fake_fetch_tasks(distro, suite, manifest, options, hostBootstrap,
                             archs=None):
            fetched[suite] = manifest
            return []

        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def task(self):
                return None

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable, display=None: None
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor.resolve_tasks", fake_resolve_tasks), \
             patch("seine.vendor.fetch_tasks", fake_fetch_tasks), \
             patch("seine.vendor.index_tasks", lambda *a, **k: []):
            code = cmd._run(self.distro(), [], [], ["bookworm"], False,
                            vendor_lock=vendor_lock)
        self.assertEqual(code, 0)
        self.assertEqual(resolve_called, [])
        self.assertEqual(fetched["bookworm"], locked_sources)

# The actual bug this whole refactor was found chasing: index() used to
# call load_manifest(suite) itself, straight off the cache directory --
# so a suite served entirely from a committed lock, on a machine that
# never ran a real resolve for it (a fresh checkout, exactly what the
# lock exists for), indexed an empty repository despite fetch_tasks()
# (which does take the manifest as an argument) having fetched the
# right files. No save_manifest() call anywhere in this test -- that is
# the point.
class VendorLockIndexesWithoutAnyCacheManifest(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.tasks import Task
        from seine.vendor import (VendorCmd, manifest_digest, repository,
                                  deploy_repository)

        suite = "lock-index-test-%d" % os.getpid()
        fetched = repository(suite)
        deployed = deploy_repository(suite)
        self.addCleanup(shutil.rmtree, fetched, ignore_errors=True)
        self.addCleanup(shutil.rmtree, deployed, ignore_errors=True)
        open(os.path.join(fetched, "openssl_3.0.11-1.dsc"), "w").close()

        entries = vendor.parse({"vendor": [{"name": "openssl"}]})
        digest = manifest_digest(self.distro(), entries, [], suite)
        locked_sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                                      "files": ["openssl_3.0.11-1.dsc"]}}
        vendor_lock = {suite: {"digest": digest, "sources": locked_sources}}

        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def task(self):
                return Task("bootstrap-host", lambda: None)

        class FakeBuilder:
            options = {}
            def exec(self, args, **kwargs):
                pass

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable, display=None: [
            t.run() for t in wave_tasks]
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor._builder_for", lambda *a, **k: FakeBuilder()):
            code = cmd._run(self.distro(), entries, [], [suite], False,
                            vendor_lock=vendor_lock)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(
            os.path.join(deployed, "pool", "main", "openssl_3.0.11-1.dsc")))

class VendorLockDigestMismatchRefusesOutright(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.vendor import VendorCmd

        vendor_lock = {"bookworm": {"digest": "stale", "sources": {}}}

        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def task(self):
                return None

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap):
            with self.assertRaises(ValueError) as ctx:
                cmd._run(self.distro(), [], [], ["bookworm"], False,
                         vendor_lock=vendor_lock)
        self.assertIn("out of date", str(ctx.exception))

class RefreshAlwaysWritesTheLockFile(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.vendor import VendorCmd, load_lock, load_manifest

        fresh = {"openssl": {"version": "3.0.11-1", "direct": True,
                             "binaries": {}, "files": [],
                             "build_dep_bins": ["libssl-dev"]}}

        def fake_resolve_tasks(distro, entries, suites_wanted, options,
                              hostBootstrap, exclude, results, extra_archs=()):
            for suite in suites_wanted:
                results[suite] = (
                    fresh, {"edges": [], "reverse": {},
                           "pruned": {"base_chroot": [], "excluded": []}})
            return []

        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def task(self):
                return None

        lock_path = os.path.join(tempfile.mkdtemp(prefix="seine-tests-lock-"),
                                 "spec.lock.yaml")
        self.addCleanup(shutil.rmtree, os.path.dirname(lock_path),
                        ignore_errors=True)

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable, display=None: None
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor.resolve_tasks", fake_resolve_tasks), \
             patch("seine.vendor.fetch_tasks", lambda *a, **k: []), \
             patch("seine.vendor.index_tasks", lambda *a, **k: []):
            code = cmd._run(self.distro(), [], [], ["bookworm"], True,
                            vendor_lock={}, lock_path=lock_path)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(lock_path))
        written = load_lock(lock_path)
        locked_entry = written["bookworm"]["sources"]["openssl"]
        self.assertEqual(locked_entry["version"], "3.0.11-1")
        # Internal bookkeeping ('direct', 'build_dep_bins') never
        # reaches the committed lock -- only the cache manifest, which
        # 'seine vendor' actually needs to keep classifying/promoting
        # correctly on its own next run, keeps them in full.
        self.assertNotIn("direct", locked_entry)
        self.assertNotIn("build_dep_bins", locked_entry)
        cached_entry = load_manifest("bookworm")["sources"]["openssl"]
        self.assertEqual(cached_entry["direct"], True)
        self.assertEqual(cached_entry["build_dep_bins"], ["libssl-dev"])

class CheckReportsDriftWithoutWritingAnything(avocado.Test):
    def distro(self):
        return {"source": "debian", "release": "bookworm", "architecture": "amd64",
               "uri": "http://example.com/debian",
               "feeds": [{"suite": "bookworm"}]}

    def test(self):
        from seine.vendor import VendorCmd

        old_sources = {"openssl": {"version": "3.0.11-1"}}
        new_sources = {"openssl": {"version": "3.0.12-1"}}

        def fake_resolve_tasks(distro, entries, suites_wanted, options,
                              hostBootstrap, exclude, results, extra_archs=()):
            for suite in suites_wanted:
                results[suite] = (
                    new_sources, {"edges": [], "reverse": {},
                                 "pruned": {"base_chroot": [], "excluded": []}})
            return []

        class FakeHostBootstrap:
            def __init__(self, distro, options, force_online=False):
                pass
            def task(self):
                return None

        cmd = VendorCmd()
        cmd.options["jobs"] = 1
        cmd._run_wave = lambda wave_tasks, retryable, display=None: None
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor.resolve_tasks", fake_resolve_tasks):
            code = cmd._run(
                self.distro(), [], [], ["bookworm"], False,
                vendor_lock={"bookworm": {"digest": "whatever",
                                          "sources": old_sources}},
                check=True)
        self.assertEqual(code, 1)

        # A matching resolve reports clean and exits 0.
        def fake_resolve_tasks_same(distro, entries, suites_wanted, options,
                                    hostBootstrap, exclude, results,
                                    extra_archs=()):
            for suite in suites_wanted:
                results[suite] = (
                    old_sources, {"edges": [], "reverse": {},
                                 "pruned": {"base_chroot": [], "excluded": []}})
            return []
        with patch("seine.vendor.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor.resolve_tasks", fake_resolve_tasks_same):
            code = cmd._run(
                self.distro(), [], [], ["bookworm"], False,
                vendor_lock={"bookworm": {"digest": "whatever",
                                          "sources": old_sources}},
                check=True)
        self.assertEqual(code, 0)

class RefreshAndCheckNeedExactlyOneSpecFile(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd
        spec_a = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False)
        self.addCleanup(os.unlink, spec_a.name)
        spec_a.write("vendor:\n    - name: openssl\n")
        spec_a.close()
        spec_b = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False)
        self.addCleanup(os.unlink, spec_b.name)
        spec_b.write("vendor:\n    - name: git\n")
        spec_b.close()

        cmd = VendorCmd()
        with self.assertRaises(SystemExit) as ctx:
            cmd.main(["--refresh", spec_a.name, spec_b.name])
        self.assertEqual(ctx.exception.code, 1)

        cmd2 = VendorCmd()
        with self.assertRaises(SystemExit) as ctx:
            cmd2.main(["--check", spec_a.name, spec_b.name])
        self.assertEqual(ctx.exception.code, 1)

# ---------------------------------------------------------------------
# snapshot.debian.org: '--refresh's own enrichment (_enrich_for_lock()),
# and the plain-vendor direct-download path it feeds
# (fetch_source()/fetch_binary()/fetch_tasks()). seine/snapshot.py's
# own client is covered in tests/vendor/snapshot.py; these are about how
# vendor.py uses it, so a fake session (matching that file's own) is
# enough -- no real network here either.
# ---------------------------------------------------------------------

class _FakeSnapshotResponse:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body
    def raise_for_status(self):
        pass
    def json(self):
        return self._json

class _FakeSnapshotSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []
    def get(self, url, timeout=None):
        self.requested.append(url)
        return self.responses.get(url, _FakeSnapshotResponse(status_code=404))

# A '--refresh' resolving thousands of sources spends the longest,
# quietest stretch of its own run right here -- one network call at a
# time against an external service, nothing else to show for it. Worth
# a line of its own under '--verbose', same as every other
# made/reused artifact already gets.
# A VendorCmd whose enrichment tasks run one at a time -- 'jobs=1' plus
# 'verbose=True' is what makes _run_wave() install a live-following
# display (_LiveFollower), the same thing a real terminal gets, which
# tees a task's own log file (see tasks.py's own 'output()' docstring
# on why a task's prints go to a file, not straight to stdout, once
# more than one can run at a time) back to sys.stdout synchronously
# before _run_wave() returns -- so patching sys.stdout around a call
# still sees what a task itself printed, exactly as a real '--verbose'
# run watched live would.
def _vendor_cmd(jobs=1, verbose=True):
    from seine.vendor import VendorCmd
    cmd = VendorCmd()
    cmd.options["jobs"] = jobs
    cmd.options["verbose"] = verbose
    return cmd

# The actual point of routing this through _run_wave() at all: '--jobs'
# governs a snapshot.debian.org lookup wave the exact same way it
# already governs a fetch wave. A session whose own 'get()' holds the
# GIL-releasing 'time.sleep()' just long enough to widen the window,
# and counts how many calls were ever in flight at once, is the
# straightforward way to prove real concurrency rather than trust that
# building Task objects implies it.
class EnrichForLockRunsLookupsConcurrentlyUpToJobs(avocado.Test):
    def test(self):
        import threading
        import time
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-concurrency-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)

        lock = threading.Lock()
        state = {"inflight": 0, "max_inflight": 0}

        class SlowSession:
            def get(self, url, timeout=None):
                with lock:
                    state["inflight"] += 1
                    state["max_inflight"] = max(state["max_inflight"],
                                                state["inflight"])
                time.sleep(0.2)
                with lock:
                    state["inflight"] -= 1
                return _FakeSnapshotResponse(status_code=404)

        sources = {}
        for i in range(4):
            name = "pkg%d" % i
            fname = "%s_1.0-1.dsc" % name
            with open(os.path.join(where, fname), "wb") as f:
                f.write(b"content")
            sources[name] = {"version": "1.0-1", "binaries": {}, "files": [fname]}

        cmd = _vendor_cmd(jobs=4, verbose=False)
        with patch("seine.vendor.snapshot.session", lambda: SlowSession()):
            cmd._enrich_for_lock(suite, sources)
        # Four independent sources, each good for one blocking request
        # (a 404 -- source_files() makes exactly one GET) -- run one at
        # a time this would take >= 0.8s and never show more than one
        # in flight; run concurrently up to 'jobs', more than one
        # really was in flight at once.
        self.assertGreater(state["max_inflight"], 1)
class EnrichForLockSaysMadeOnAQueryAndReusedOnACacheHit(avocado.Test):
    def test(self):
        import hashlib
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-verbose-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        content = b"dsc content"
        fname = "openssl_3.0.11-1.dsc"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(content)
        local_sha1 = hashlib.sha1(content).hexdigest()

        url = snapshot.BASE_URL + "/mr/package/openssl/3.0.11-1/srcfiles?fileinfo=1"
        body = {"result": [{"hash": local_sha1}],
               "fileinfo": {local_sha1: [
                   {"name": fname, "archive_name": "debian", "path": "/x",
                    "size": len(content), "first_seen": "20260101T000000Z"}]}}
        sess = _FakeSnapshotSession({url: _FakeSnapshotResponse(json_body=body)})
        sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                               "files": [fname]}}

        cmd = _vendor_cmd()
        with patch("seine.vendor.snapshot.session", lambda: sess), \
             patch("sys.stdout", new=io.StringIO()) as out:
            cmd._enrich_for_lock(suite, sources)
        self.assertIn("vendor snapshot openssl=3.0.11-1 made", out.getvalue())

        # Same source again, now cached -- no network call needed (the
        # 404-everything session proves it, same trick
        # EnrichForLockCachesAMatchAndNeverAsksSnapshotAgain uses), and
        # the line says so instead of staying silent about it. This one
        # is printed synchronously in the pre-pass, before any task
        # ever runs, so it needs none of the live-follow machinery
        # above to reach stdout.
        quiet_sess = _FakeSnapshotSession({})
        with patch("seine.vendor.snapshot.session", lambda: quiet_sess), \
             patch("sys.stdout", new=io.StringIO()) as out:
            cmd._enrich_for_lock(suite, sources)
        self.assertEqual(quiet_sess.requested, [])
        self.assertIn("vendor snapshot openssl=3.0.11-1 reused", out.getvalue())

        # Quiet without '--verbose', same as every other say() call --
        # a different, not-yet-cached source (still a real lookup, not
        # a cache hit that would have said nothing regardless), also a
        # match, so the only other thing this could print (the
        # mismatch warning, deliberately unconditional -- see this
        # function's own body) never fires either.
        fname2 = "bash_5.2-1.dsc"
        with open(os.path.join(where, fname2), "wb") as f:
            f.write(content)
        url2 = snapshot.BASE_URL + "/mr/package/bash/5.2-1/srcfiles?fileinfo=1"
        sess.responses[url2] = _FakeSnapshotResponse(json_body={
            "result": [{"hash": local_sha1}],
            "fileinfo": {local_sha1: [
                {"name": fname2, "archive_name": "debian", "path": "/x",
                 "size": len(content), "first_seen": "20260101T000000Z"}]}})
        sources2 = {"bash": {"version": "5.2-1", "binaries": {},
                             "files": [fname2]}}
        quiet_cmd = _vendor_cmd(verbose=False)
        with patch("seine.vendor.snapshot.session", lambda: sess), \
             patch("sys.stdout", new=io.StringIO()) as out:
            quiet_cmd._enrich_for_lock(suite, sources2)
        self.assertEqual(out.getvalue(), "")

    # A binary's own version churns (binNMUs) far more than its
    # source's, so its cache misses far more too under real archive
    # movement -- not a caching bug, but indistinguishable from one
    # without a 'made' line to say a real query actually happened, on
    # a specific binpkg:arch=version rather than the whole source.
    def test_binaries_get_their_own_made_and_reused_lines(self):
        import hashlib
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-verbose-binary-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        content = b"deb bytes"
        deb_name = "libssl3_3.0.11-1_amd64.deb"
        with open(os.path.join(where, deb_name), "wb") as f:
            f.write(content)
        local_sha1 = hashlib.sha1(content).hexdigest()

        url = (snapshot.BASE_URL +
              "/mr/package/openssl/3.0.11-1/binfiles/libssl3/3.0.11-1?fileinfo=1")
        body = {"result": [{"hash": local_sha1, "architecture": "amd64"}],
               "fileinfo": {local_sha1: [
                   {"name": deb_name, "archive_name": "debian", "path": "/x",
                    "size": len(content), "first_seen": "20260101T000000Z"}]}}
        sess = _FakeSnapshotSession({url: _FakeSnapshotResponse(json_body=body)})
        sources = {"openssl": {"version": "3.0.11-1", "files": [],
                               "binaries": {"libssl3": {"amd64": "3.0.11-1"}}}}

        cmd = _vendor_cmd()
        with patch("seine.vendor.snapshot.session", lambda: sess), \
             patch("sys.stdout", new=io.StringIO()) as out:
            cmd._enrich_for_lock(suite, sources)
        self.assertIn("vendor snapshot libssl3:amd64=3.0.11-1 made", out.getvalue())

        quiet_sess = _FakeSnapshotSession({})
        with patch("seine.vendor.snapshot.session", lambda: quiet_sess), \
             patch("sys.stdout", new=io.StringIO()) as out:
            cmd._enrich_for_lock(suite, sources)
        self.assertEqual(quiet_sess.requested, [])
        self.assertIn("vendor snapshot libssl3:amd64=3.0.11-1 reused", out.getvalue())

class EnrichForLockRecordsASnapshotUrlOnHashMatch(avocado.Test):
    def test(self):
        import hashlib
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        content = b"dsc content"
        fname = "openssl_3.0.11-1.dsc"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(content)
        local_sha1 = hashlib.sha1(content).hexdigest()

        url = snapshot.BASE_URL + "/mr/package/openssl/3.0.11-1/srcfiles?fileinfo=1"
        body = {"result": [{"hash": local_sha1}],
               "fileinfo": {local_sha1: [
                   {"name": fname, "archive_name": "debian", "path": "/x",
                    "size": len(content), "first_seen": "20260101T000000Z"}]}}
        sess = _FakeSnapshotSession({url: _FakeSnapshotResponse(json_body=body)})

        sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                               "files": [fname]}}
        with patch("seine.vendor.snapshot.session", lambda: sess):
            enriched = _vendor_cmd()._enrich_for_lock(suite, sources)
        # sha1 alone, not the url built from it -- see _enrich_for_lock()'s
        # own comment on why the lock never stores one.
        self.assertEqual(enriched["openssl"]["snapshot"], {fname: local_sha1})
        self.assertEqual(enriched["openssl"]["file_hashes"][fname],
                         hashlib.sha256(content).hexdigest())

# The actual point of caching a match on the same artifact key
# fetch_source() already uses: a second '--refresh' of a suite whose
# packages have not moved must not ask snapshot.debian.org about every
# one of them again. Proven here by handing the second run a session
# that 404s everything -- if it were actually queried, the recorded
# 'snapshot' entry would vanish, not survive.
class EnrichForLockCachesAMatchAndNeverAsksSnapshotAgain(avocado.Test):
    def test(self):
        import hashlib
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-cache-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        content = b"dsc content"
        fname = "openssl_3.0.11-1.dsc"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(content)
        local_sha1 = hashlib.sha1(content).hexdigest()

        url = snapshot.BASE_URL + "/mr/package/openssl/3.0.11-1/srcfiles?fileinfo=1"
        body = {"result": [{"hash": local_sha1}],
               "fileinfo": {local_sha1: [
                   {"name": fname, "archive_name": "debian", "path": "/x",
                    "size": len(content), "first_seen": "20260101T000000Z"}]}}
        sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                               "files": [fname]}}

        cmd = _vendor_cmd()
        first = _FakeSnapshotSession({url: _FakeSnapshotResponse(json_body=body)})
        with patch("seine.vendor.snapshot.session", lambda: first):
            cmd._enrich_for_lock(suite, sources)
        self.assertEqual(len(first.requested), 1)

        second = _FakeSnapshotSession({})
        with patch("seine.vendor.snapshot.session", lambda: second):
            enriched = cmd._enrich_for_lock(suite, sources)
        self.assertEqual(second.requested, [])
        self.assertEqual(enriched["openssl"]["snapshot"], {fname: local_sha1})

# The other half of the same guarantee: a miss (snapshot.debian.org
# has never heard of this name/version) is asked again on every run,
# never cached -- a freshly uploaded version lags behind snapshot's own
# indexing, and caching "not found" forever would mean a later refresh,
# run once the mirror has caught up, never noticing.
class EnrichForLockNeverCachesAMiss(avocado.Test):
    def test(self):
        from seine.vendor import repository

        suite = "enrich-nocache-miss-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        fname = "openssl_3.0.11-1.dsc"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(b"content")
        sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                               "files": [fname]}}

        cmd = _vendor_cmd()
        first = _FakeSnapshotSession({})
        with patch("seine.vendor.snapshot.session", lambda: first):
            cmd._enrich_for_lock(suite, sources)
        self.assertEqual(len(first.requested), 1)

        second = _FakeSnapshotSession({})
        with patch("seine.vendor.snapshot.session", lambda: second):
            cmd._enrich_for_lock(suite, sources)
        self.assertEqual(len(second.requested), 1)

# The cross-check this whole enrichment exists for: snapshot.debian.org
# knowing *a* file under this exact name/version is not enough -- its
# own declared checksum has to actually match what apt just fetched, or
# recording the URL would let a later plain 'seine vendor' silently pull
# different bytes than the ones '--refresh' verified.
class EnrichForLockSkipsOnHashMismatch(avocado.Test):
    def test(self):
        import hashlib
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-mismatch-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        fname = "openssl_3.0.11-1.dsc"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(b"local content")

        url = snapshot.BASE_URL + "/mr/package/openssl/3.0.11-1/srcfiles?fileinfo=1"
        other_sha1 = hashlib.sha1(b"different content").hexdigest()
        body = {"result": [{"hash": other_sha1}],
               "fileinfo": {other_sha1: [
                   {"name": fname, "archive_name": "debian", "path": "/x",
                    "size": 1, "first_seen": "20260101T000000Z"}]}}
        sess = _FakeSnapshotSession({url: _FakeSnapshotResponse(json_body=body)})

        sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                               "files": [fname]}}
        with patch("seine.vendor.snapshot.session", lambda: sess):
            enriched = _vendor_cmd()._enrich_for_lock(suite, sources)
        self.assertNotIn("snapshot", enriched["openssl"])
        # The hash still gets recorded -- only the snapshot URL is
        # withheld, not the lock's own no-deviation guarantee.
        self.assertIn(fname, enriched["openssl"]["file_hashes"])

# The real bug this guards: snapshot.debian.org can carry more than one
# upload under the exact same name/version (a maintainer re-uploading
# without a version bump -- 'golang-github-grpc-ecosystem-go-grpc-
# middleware_1.3.0-1' is a real example, found live). An earlier
# version of source_files() collapsed those down to one candidate
# before _enrich_for_lock() ever compared a hash, so whichever upload
# was actually fetched only matched by chance -- this checks every
# candidate snapshot.debian.org names for the filename, not just the
# first.
class EnrichForLockChecksEveryCandidateNotJustTheFirst(avocado.Test):
    def test(self):
        import hashlib
        from seine.vendor import repository
        from seine import snapshot

        suite = "enrich-multi-upload-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        fname = "golang-foo_1.0-1.dsc"
        actual_content = b"the upload apt actually fetched this time"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(actual_content)
        actual_sha1 = hashlib.sha1(actual_content).hexdigest()
        other_sha1 = hashlib.sha1(b"an earlier, different upload").hexdigest()

        url = snapshot.BASE_URL + "/mr/package/golang-foo/1.0-1/srcfiles?fileinfo=1"
        body = {"result": [{"hash": other_sha1}, {"hash": actual_sha1}],
               "fileinfo": {
                   # Both entries share 'archive_name: debian' -- two
                   # real uploads under the same version, not two
                   # archives, so ARCHIVE_ORDER cannot pick between
                   # them; only the actual local content can.
                   other_sha1: [{"name": fname, "archive_name": "debian",
                                "path": "/x", "size": 1,
                                "first_seen": "20220101T000000Z"}],
                   actual_sha1: [{"name": fname, "archive_name": "debian",
                                 "path": "/x", "size": 1,
                                 "first_seen": "20220201T000000Z"}]}}
        sess = _FakeSnapshotSession({url: _FakeSnapshotResponse(json_body=body)})

        sources = {"golang-foo": {"version": "1.0-1", "binaries": {},
                                  "files": [fname]}}
        with patch("seine.vendor.snapshot.session", lambda: sess):
            enriched = _vendor_cmd()._enrich_for_lock(suite, sources)
        self.assertEqual(enriched["golang-foo"]["snapshot"][fname], actual_sha1)

# Not an error, matching seine/snapshot.py's own docstring: '--refresh'
# still succeeds, this source just has nothing to fall back to later.
class EnrichForLockSkipsWhenSnapshotHasNothing(avocado.Test):
    def test(self):
        from seine.vendor import repository

        suite = "enrich-404-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        fname = "openssl_3.0.11-1.dsc"
        with open(os.path.join(where, fname), "wb") as f:
            f.write(b"content")
        sess = _FakeSnapshotSession({})

        sources = {"openssl": {"version": "3.0.11-1", "binaries": {},
                               "files": [fname]}}
        with patch("seine.vendor.snapshot.session", lambda: sess):
            enriched = _vendor_cmd()._enrich_for_lock(suite, sources)
        self.assertNotIn("snapshot", enriched["openssl"])
        self.assertIn(fname, enriched["openssl"]["file_hashes"])

# The real bug this guards: two sources in the same suite's manifest
# can each name the same (binpkg, arch) in their own 'binaries' dict
# (build-dep closures overlap -- 'ecj' is a real example found live),
# and before _dedup_binaries() existed, _enrich_for_lock() queued a
# 'snapshot-bin:<suite>:ecj:amd64' task for each of them, which
# task_runner.run()'s own tasks.ordered() rejects outright
# ("duplicate task"). This must not raise, and must only query
# snapshot.debian.org once for the pair.
class EnrichForLockDedupsABinaryReachableFromTwoSources(avocado.Test):
    def test(self):
        from seine.vendor import repository

        suite = "enrich-dedup-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        deb_name = "ecj_1.0-1_amd64.deb"
        with open(os.path.join(where, deb_name), "wb") as f:
            f.write(b"deb bytes")
        sess = _FakeSnapshotSession({})

        sources = {
            "a": {"version": "1.0-1", "files": [],
                 "binaries": {"ecj": {"amd64": "1.0-1"}}},
            "b": {"version": "2.0-1", "files": [],
                 "binaries": {"ecj": {"amd64": "1.0-1"}}},
        }
        with patch("seine.vendor.snapshot.session", lambda: sess):
            enriched = _vendor_cmd()._enrich_for_lock(suite, sources)
        binfiles_requests = [u for u in sess.requested if "/binfiles/" in u]
        self.assertEqual(len(binfiles_requests), 1)
        self.assertIn("a", enriched)
        self.assertIn("b", enriched)

# Same real bug, on fetch_tasks()'s own copy of the dedup (it builds
# its fetch queue independently of _enrich_for_lock() -- see this
# module's own comment on 'seen_bins'/'queued_bins' above).
class FetchTasksDedupsABinaryReachableFromTwoSources(avocado.Test):
    def test(self):
        from seine.bootstrap import HostBootstrap
        distro = {"source": "debian", "release": "bookworm", "architecture": "amd64",
                 "uri": "http://example.com/debian",
                 "feeds": [{"suite": "bookworm"}]}
        manifest = {
            "a": {"version": "1.0-1", "files": [],
                 "binaries": {"ecj": {"amd64": "1.0-1"}}},
            "b": {"version": "2.0-1", "files": [],
                 "binaries": {"ecj": {"amd64": "1.0-1"}}},
        }
        tasks = vendor.fetch_tasks(distro, "bookworm", manifest, {},
                                   HostBootstrap(distro, {}))
        names = [t.name for t in tasks]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len([n for n in names if n.startswith("fetch-bin:")]), 1)

class FetchSourceUsesSnapshotDirectlyWhenRecorded(avocado.Test):
    def test(self):
        import hashlib
        from seine.vendor import fetch_source, repository

        suite = "fetch-snapshot-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)
        content = b"dsc bytes"
        expected = hashlib.sha256(content).hexdigest()

        def fake_download(sess, url, dest):
            with open(dest, "wb") as f:
                f.write(content)
            return hashlib.sha256(content).hexdigest()

        with patch("seine.vendor.snapshot.download", fake_download):
            fetch_source(None, suite, "openssl", "3.0.11-1",
                        snapshot_hashes={"openssl_3.0.11-1.dsc": "abc123"},
                        expected_hashes={"openssl_3.0.11-1.dsc": expected},
                        options={})
        with open(os.path.join(where, "openssl_3.0.11-1.dsc"), "rb") as f:
            self.assertEqual(f.read(), content)

# The hard-error/no-fallback guarantee: a snapshot download that does
# not match the lock's own recorded hash is refused outright, and the
# bad file is not left behind for a later run to trust by accident.
class FetchSourceRefusesAMismatchedSnapshotFile(avocado.Test):
    def test(self):
        from seine.vendor import fetch_source, repository

        suite = "fetch-snapshot-mismatch-test-%d" % os.getpid()
        where = repository(suite)
        self.addCleanup(shutil.rmtree, where, ignore_errors=True)

        def fake_download(sess, url, dest):
            with open(dest, "wb") as f:
                f.write(b"wrong bytes")
            return "not-the-expected-hash"

        with patch("seine.vendor.snapshot.download", fake_download):
            with self.assertRaises(ValueError):
                fetch_source(None, suite, "openssl", "3.0.11-1",
                            snapshot_hashes={"openssl_3.0.11-1.dsc": "abc123"},
                            expected_hashes={"openssl_3.0.11-1.dsc": "expected-hash"},
                            options={})
        self.assertFalse(os.path.isfile(os.path.join(where, "openssl_3.0.11-1.dsc")))

# The actual point of routing a locked source's fetch through
# snapshot.debian.org at all: no builder/container, ever, on this path
# -- apt was never going to have this exact version (that is why
# '--refresh' recorded a snapshot sha1 in the first place).
class FetchTasksNeverBuildsAContainerForASnapshotPinnedSource(avocado.Test):
    def test(self):
        from seine.bootstrap import HostBootstrap
        distro = {"source": "debian", "release": "bookworm", "architecture": "amd64",
                 "uri": "http://example.com/debian",
                 "feeds": [{"suite": "bookworm"}]}
        manifest = {
            "openssl": {"version": "3.0.11-1", "binaries": {},
                       "files": ["openssl_3.0.11-1.dsc"],
                       "snapshot": {"openssl_3.0.11-1.dsc": "abc123"},
                       "file_hashes": {"openssl_3.0.11-1.dsc": "abc"}},
        }
        def boom(*a, **k):
            raise AssertionError("_builder_for() must not run for a snapshot-pinned source")
        downloaded = []
        def fake_download(sess, url, dest):
            downloaded.append((url, dest))
            with open(dest, "wb") as f:
                f.write(b"x")
            return "abc"
        with patch("seine.vendor._builder_for", boom), \
             patch("seine.vendor.snapshot.download", fake_download):
            tasks = vendor.fetch_tasks(distro, "bookworm", manifest, {},
                                       HostBootstrap(distro, {}))
            self.assertEqual(len(tasks), 1)
            tasks[0].run()
        self.assertEqual(len(downloaded), 1)

# The lock is meant to be reviewed as a diff, so a change in Python
# dict insertion order (which nothing here controls -- resolve() builds
# its own dicts off a BFS walk, not alphabetically) must never show up
# as a spurious reorder. save_lock() relies entirely on
# 'sort_keys=True' recursing through every nesting level for this --
# worth a test of its own, since it is easy to lose silently (a
# reformat that drops the kwarg, a future field built from a raw
# set() and never sorted).
class SaveLockOutputIsDeterministicRegardlessOfDictOrder(avocado.Test):
    def test(self):
        from seine.vendor import save_lock, load_lock

        a = {"bookworm": {"digest": "d", "sources": {
            "openssl": {"version": "3.0.11-1",
                       "binaries": {"libssl3": {"amd64": "3.0.11-1"},
                                   "libcrypto3": {"amd64": "3.0.11-1"}},
                       "file_hashes": {"b.dsc": "2", "a.dsc": "1"}},
            "bash": {"version": "5.2-1", "binaries": {}}}}}
        # Same content, every dict rebuilt in the opposite key order.
        b = {"bookworm": {"sources": {
            "bash": {"binaries": {}, "version": "5.2-1"},
            "openssl": {"file_hashes": {"a.dsc": "1", "b.dsc": "2"},
                       "binaries": {"libcrypto3": {"amd64": "3.0.11-1"},
                                   "libssl3": {"amd64": "3.0.11-1"}},
                       "version": "3.0.11-1"}}, "digest": "d"}}

        workdir = tempfile.mkdtemp(prefix="seine-tests-lock-order-")
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        path_a = os.path.join(workdir, "a.lock.yaml")
        path_b = os.path.join(workdir, "b.lock.yaml")
        save_lock(path_a, a)
        save_lock(path_b, b)
        with open(path_a) as fa, open(path_b) as fb:
            self.assertEqual(fa.read(), fb.read())
        self.assertEqual(load_lock(path_a), load_lock(path_b))

# A binary's own value collapses to a bare hash when its version
# matches its source's -- the version is then implied, not repeated --
# and only ever becomes a small '{hash, version}' mapping for a
# genuine divergence (a binNMU, kept per architecture: see
# _compress_binaries()'s own comment on why a per-package override
# would be unsafe). 'binary_hashes' is folded in and dropped entirely.
class CompressBinariesMergesHashAndOnlyKeepsAVersionOnDivergence(avocado.Test):
    def test(self):
        from seine.vendor import _compress_binaries, _expand_binaries

        sources = {"openssl": {"version": "3.0.11-1", "binaries": {
            "libssl3": {"amd64": "3.0.11-1", "arm64": "3.0.11-1"},
            "libssl3-udeb": {"amd64": "3.0.11-1+b2"}},
            "binary_hashes": {
                "libssl3": {"amd64": "aaa", "arm64": "bbb"},
                "libssl3-udeb": {"amd64": "ccc"}}}}
        compressed = _compress_binaries(sources)
        self.assertNotIn("binary_hashes", compressed["openssl"])
        self.assertEqual(compressed["openssl"]["binaries"]["libssl3"],
                         {"amd64": "aaa", "arm64": "bbb"})
        self.assertEqual(compressed["openssl"]["binaries"]["libssl3-udeb"],
                         {"amd64": {"hash": "ccc", "version": "3.0.11-1+b2"}})
        self.assertEqual(_expand_binaries(compressed), sources)

# The same fold, for a binary that also carries a snapshot.debian.org
# sha1 -- 'binary_snapshot' drops out entirely too, joined into the same
# scalar as the sha256 (64 hex chars, never ambiguous against a sha1's
# 40) when the version matches, or into the divergence mapping otherwise.
class CompressBinariesFoldsInASnapshotSha1(avocado.Test):
    def test(self):
        from seine.vendor import _compress_binaries, _expand_binaries

        sha256 = "a" * 64
        sha1 = "b" * 40
        sources = {"openssl": {"version": "3.0.11-1", "binaries": {
            "libssl3": {"amd64": "3.0.11-1"},
            "libssl3-udeb": {"amd64": "3.0.11-1+b2"}},
            "binary_hashes": {"libssl3": {"amd64": sha256},
                              "libssl3-udeb": {"amd64": sha256}},
            "binary_snapshot": {"libssl3": {"amd64": sha1},
                                "libssl3-udeb": {"amd64": sha1}}}}
        compressed = _compress_binaries(sources)
        self.assertNotIn("binary_snapshot", compressed["openssl"])
        self.assertEqual(compressed["openssl"]["binaries"]["libssl3"]["amd64"],
                         "%s:%s" % (sha256, sha1))
        self.assertEqual(compressed["openssl"]["binaries"]["libssl3-udeb"]["amd64"],
                         {"hash": sha256, "version": "3.0.11-1+b2", "snapshot": sha1})
        self.assertEqual(_expand_binaries(compressed), sources)

# The same fold, aligned onto a source's own 'files:' -- 'file_hashes'/
# 'snapshot' drop out entirely, each filename's sha256 (and, when found,
# its snapshot.debian.org sha1) merged into the one 'files:' mapping
# instead of three separate structures each repeating every filename.
class CompressFilesFoldsHashAndSnapshotIntoOneScalar(avocado.Test):
    def test(self):
        from seine.vendor import _compress_files, _expand_files

        sha256_a = "a" * 64
        sha256_b = "b" * 64
        sha1 = "c" * 40
        sources = {"openssl": {"version": "3.0.11-1",
                               "files": ["openssl_3.0.11-1.dsc",
                                        "openssl_3.0.11.orig.tar.gz"],
                               "file_hashes": {"openssl_3.0.11-1.dsc": sha256_a,
                                              "openssl_3.0.11.orig.tar.gz": sha256_b},
                               "snapshot": {"openssl_3.0.11-1.dsc": sha1}}}
        compressed = _compress_files(sources)
        self.assertNotIn("file_hashes", compressed["openssl"])
        self.assertNotIn("snapshot", compressed["openssl"])
        self.assertEqual(compressed["openssl"]["files"],
                         {"openssl_3.0.11-1.dsc": "%s:%s" % (sha256_a, sha1),
                          "openssl_3.0.11.orig.tar.gz": sha256_b})
        self.assertEqual(_expand_files(compressed), sources)

class SaveLockMergesAMatchingBinaryIntoItsOwnHash(avocado.Test):
    def test(self):
        from seine.vendor import save_lock, load_lock

        suites = {"bookworm": {"digest": "d", "sources": {"openssl": {
            "version": "3.0.11-1",
            "binaries": {"libssl3": {"amd64": "3.0.11-1"},
                        "libssl3-udeb": {"amd64": "3.0.11-1+b2"}},
            "binary_hashes": {"libssl3": {"amd64": "a" * 64},
                              "libssl3-udeb": {"amd64": "b" * 64}}}}}}
        workdir = tempfile.mkdtemp(prefix="seine-tests-lock-compress-")
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        path = os.path.join(workdir, "spec.lock.yaml")
        save_lock(path, suites)
        with open(path) as f:
            raw = f.read()
        # A matching version is a bare hash on disk, not a version
        # string, and no separate 'binary_hashes:' key survives at all.
        self.assertIn("amd64: " + "a" * 64, raw)
        self.assertNotIn("amd64: 3.0.11-1\n", raw)
        self.assertNotIn("binary_hashes", raw)
        loaded = load_lock(path)
        openssl = loaded["bookworm"]["sources"]["openssl"]
        self.assertEqual(openssl["binaries"]["libssl3"]["amd64"], "3.0.11-1")
        self.assertEqual(openssl["binary_hashes"]["libssl3"]["amd64"], "a" * 64)
        self.assertEqual(openssl["binaries"]["libssl3-udeb"]["amd64"], "3.0.11-1+b2")
        self.assertEqual(openssl["binary_hashes"]["libssl3-udeb"]["amd64"], "b" * 64)

# The trickier of the two loading paths a lock's data ever takes (see
# _expand_binary_versions()'s own comment): an ordinary 'seine vendor'
# run reaches spec["_vendor_lock"] through BuildCmd's own generic YAML/
# jinja loader, never through load_lock() at all -- this proves
# VendorCmd.main() expands it there too, by handing a compressed lock
# straight to a stubbed _run() and inspecting what it actually
# received.
class MainExpandsACompressedLockLoadedThroughBuildCmd(avocado.Test):
    def test(self):
        from seine.vendor import VendorCmd

        spec_dir = tempfile.mkdtemp(prefix="seine-tests-lock-expand-")
        self.addCleanup(shutil.rmtree, spec_dir, ignore_errors=True)
        spec_path = os.path.join(spec_dir, "foo.yaml")
        lock_path = os.path.join(spec_dir, "foo.lock.yaml")
        with open(spec_path, "w") as f:
            f.write("distribution:\n    release: bookworm\n"
                    "    feeds:\n        - suite: bookworm\n"
                    "vendor:\n    - name: openssl\n")
        with open(lock_path, "w") as f:
            f.write("vendor:\n"
                    "  bookworm:\n"
                    "    digest: whatever\n"
                    "    sources:\n"
                    "      openssl:\n"
                    "        version: '3.0.11-1'\n"
                    "        binaries:\n"
                    "          libssl3:\n"
                    "            amd64: null\n")

        seen = {}
        cmd = VendorCmd()
        cmd._run = lambda distro, entries, exclude, wanted, refresh, archs=None, \
                          extra_archs=(), **kwargs: (
            seen.update(vendor_lock=kwargs["vendor_lock"]) or 0)
        with self.assertRaises(SystemExit) as ctx:
            cmd.main([spec_path])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(
            seen["vendor_lock"]["bookworm"]["sources"]["openssl"]
                ["binaries"]["libssl3"]["amd64"],
            "3.0.11-1")
