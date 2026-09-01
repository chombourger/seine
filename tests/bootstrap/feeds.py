#!/usr/bin/env python3

import atexit
import avocado
import os
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import apt_sources, base_feed, feeds, offline_apt_script

# Nothing under here may write into the machine's own cache. These build
# Builder objects directly, and asking one for a stamp or an index makes
# the directory it would live in -- so a plain unit test run leaves
# directories in ~/.cache/seine, and a run at an older commit leaves them
# in a layout the current code no longer uses.
#
# One cache per test process, thrown away with it. Set rather than
# defaulted so it holds however the suite was invoked; the tests that
# build images for real pass their own to the seine they run.
os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
# vendor.deploy_repository() is a directory the same way, under deploy/
# rather than cache/ -- same reasoning, same fix.
os.environ["SEINE_DEPLOY_DIR"] = tempfile.mkdtemp(prefix="seine-tests-deploy-")
# And no key either: what a build signs with is read from the environment,
# so a developer who signs their own builds would otherwise run a
# different suite than everyone else.
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_DEPLOY_DIR"],
                ignore_errors=True)

DISTRO = {"source": "debian", "release": "bookworm", "architecture": "amd64",
          "uri": "http://example.com/debian"}

def distro(feeds):
    return dict(DISTRO, feeds=feeds)

class ReleaseIsTheOnlyFeedByDefault(avocado.Test):
    def test(self):
        self.assertEqual(apt_sources(DISTRO, sources=True),
                         ["deb http://example.com/debian bookworm main",
                          "deb-src http://example.com/debian bookworm main"])

class ExpiredFeedsCarryTheirOwnOption(avocado.Test):
    def test(self):
        # The option belongs to the feed that asked for it and to both of
        # its lines: a snapshot expires its sources along with its binaries.
        sources = apt_sources(distro([
            {"suite": "bookworm", "valid-until": False},
            {"suite": "bookworm-updates"},
        ]), sources=True)
        self.assertEqual(sources, [
            "deb [check-valid-until=no] http://example.com/debian bookworm main",
            "deb-src [check-valid-until=no] http://example.com/debian bookworm main",
            "deb http://example.com/debian bookworm-updates main",
            "deb-src http://example.com/debian bookworm-updates main",
        ])

class FeedsOverrideWhatTheDistributionSaid(avocado.Test):
    def test(self):
        # Everything but the suite has a default, and the default is the
        # distribution's -- a feed served from elsewhere says so itself.
        self.assertEqual(apt_sources(distro([
            {"suite": "bookworm"},
            {"suite": "bookworm-security",
             "uri": "http://security.debian.org/debian-security",
             "components": "main contrib"},
        ])), [
            "deb http://example.com/debian bookworm main",
            "deb http://security.debian.org/debian-security bookworm-security main contrib",
        ])

class SourcesAreAskedForAndDeclared(avocado.Test):
    def test(self):
        feeds = [{"suite": "bookworm"},
                 {"suite": "vendor", "sources": False}]
        # No deb-src at all unless the caller wants one: the image is built
        # from binaries and has no use for them.
        self.assertEqual(apt_sources(distro(feeds)),
                         ["deb http://example.com/debian bookworm main",
                          "deb http://example.com/debian vendor main"])
        # And none for a feed that has said it carries none, which would
        # fail every 'apt-get update' the builder runs.
        self.assertEqual(apt_sources(distro(feeds), sources=True),
                         ["deb http://example.com/debian bookworm main",
                          "deb-src http://example.com/debian bookworm main",
                          "deb http://example.com/debian vendor main"])

class OfflineFeedsReadFromTheLocalVendorInstead(avocado.Test):
    def test(self):
        from seine.utils import vendor_mountpoint, offline_suites
        offline = dict(distro([
            {"suite": "bookworm"},
            {"suite": "bookworm-security",
             "uri": "http://security.debian.org/debian-security"},
        ]), **{"apt-pull-mode": "offline"})
        self.assertEqual(apt_sources(offline, sources=True, offline=True), [
            "deb [trusted=yes] file:%s bookworm main" % vendor_mountpoint("bookworm"),
            "deb [trusted=yes] file:%s bookworm extra" % vendor_mountpoint("bookworm"),
            "deb-src [trusted=yes] file:%s bookworm main" % vendor_mountpoint("bookworm"),
            "deb-src [trusted=yes] file:%s bookworm extra" % vendor_mountpoint("bookworm"),
            "deb [trusted=yes] file:%s bookworm-security main" % vendor_mountpoint("bookworm-security"),
            "deb [trusted=yes] file:%s bookworm-security extra" % vendor_mountpoint("bookworm-security"),
            "deb-src [trusted=yes] file:%s bookworm-security main" % vendor_mountpoint("bookworm-security"),
            "deb-src [trusted=yes] file:%s bookworm-security extra" % vendor_mountpoint("bookworm-security"),
        ])
        self.assertEqual(offline_suites(offline),
                         ["bookworm", "bookworm-security"])
        self.assertEqual(offline_suites(DISTRO), [])

    # A signed vendor is verified rather than trusted outright -- the whole
    # point of signing one is a rebuild on another machine, years later,
    # being able to tell its packages were not tampered with since.
    def test_a_signed_vendor_is_verified_not_trusted(self):
        from seine import vendor
        from seine.utils import vendor_mountpoint
        where = vendor.deploy_repository("bookworm")
        for name in ["InRelease", "ABCD1234.gpg"]:
            open(os.path.join(where, name), "w").close()
        try:
            offline = dict(distro([{"suite": "bookworm"}]),
                           **{"apt-pull-mode": "offline"})
            self.assertEqual(apt_sources(offline, offline=True), [
                "deb [signed-by=%s/ABCD1234.gpg] file:%s bookworm main"
                % (vendor_mountpoint("bookworm"), vendor_mountpoint("bookworm")),
                "deb [signed-by=%s/ABCD1234.gpg] file:%s bookworm extra"
                % (vendor_mountpoint("bookworm"), vendor_mountpoint("bookworm")),
            ])
        finally:
            shutil.rmtree(where, ignore_errors=True)

    # apt_sources() backs every container that ever calls apt, including
    # the ones 'seine vendor' itself uses to populate that very
    # repository -- so going offline has to be an explicit ask, never
    # inferred from the distro dict alone, or a specification combining
    # 'vendor:' with 'apt-pull-mode: offline' would have the resolve/fetch
    # containers try to read a repository that has nothing in it yet.
    def test_offline_is_never_inferred_from_the_distro_alone(self):
        offline = dict(distro([{"suite": "bookworm"}]),
                       **{"apt-pull-mode": "offline"})
        self.assertEqual(apt_sources(offline),
                         ["deb http://example.com/debian bookworm main"])

# ansible_runner.py and sbuild.py both turn a feed list into a container's
# own apt sources through this one function -- exercised directly here so
# a change to either caller's script-building is not the only thing that
# would catch a regression in it.
class OfflineAptScriptIsSharedByAnsibleAndSbuild(avocado.Test):
    def test_online_appends_without_clearing(self):
        made = offline_apt_script(distro([{"suite": "bookworm"}]),
                                  feeds(distro([{"suite": "bookworm"}])),
                                  "/etc/apt/sources.list.d/seine.list")
        self.assertNotIn("rm -f", made)
        self.assertIn("http://example.com/debian bookworm main", made)
        self.assertIn("/etc/apt/sources.list.d/seine.list", made)

    def test_offline_clears_first_and_reads_the_vendor(self):
        offline = dict(distro([{"suite": "bookworm"}]),
                       **{"apt-pull-mode": "offline"})
        made = offline_apt_script(offline, feeds(offline),
                                  "/etc/apt/sources.list.d/seine.list",
                                  offline=True)
        self.assertIn("rm -f /etc/apt/sources.list", made)
        self.assertIn("file:/vendor-repo/bookworm", made)

class MalformedFeedsAreRejected(avocado.Test):
    def test(self):
        for feeds in ["bookworm",                  # not a list
                      ["bookworm"],                # not a dictionary
                      [{"uri": "http://example.com/debian"}]]:  # no suite
            try:
                apt_sources(distro(feeds))
                self.fail("parsing succeeded for feeds: %s" % feeds)
            except ValueError:
                pass

class FeedsAreValidatedWhenTheSpecIsParsed(avocado.Test):
    def test(self):
        # Reported against the file that has the mistake, rather than by
        # the container it would have failed in.
        from seine.build import BuildCmd
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: bookworm
                    feeds:
                        - suite: bookworm
                          valid_until: false
                image:
                    filename: feeds-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        try:
            build.parse()
            self.fail("parsing succeeded for a mistyped feed setting!")
        except ValueError as e:
            self.assertIn("valid_until", str(e),
                          "the feed was not what parsing complained about")

class UnknownFeedSettingIsRejected(avocado.Test):
    def test(self):
        try:
            apt_sources(distro([{"suite": "bookworm", "valid_until": False}]))
            self.fail("parsing succeeded for an unknown feed setting!")
        except ValueError:
            pass

# 'release:' groups a pocket back to the base release it belongs to,
# unset defaulting to the feed's own suite -- what 'seine vendor' reads
# to know which feeds a suite's own resolver may see (vendor.py's
# feeds_for_suite()), never guessed from the suite's own name.
class FeedsCarryTheirOwnRelease(avocado.Test):
    def test(self):
        parsed = feeds(distro([
            {"suite": "bookworm"},
            {"suite": "bookworm-security", "release": "bookworm"},
        ]))
        self.assertEqual([f["release"] for f in parsed], ["bookworm", "bookworm"])

    def test_unset_defaults_to_its_own_suite(self):
        parsed = feeds(distro([{"suite": "bookworm-backports"}]))
        self.assertEqual(parsed[0]["release"], "bookworm-backports")

class RebuiltWhenAFeedMoves(avocado.Test):
    def stamp(self, feeds):
        from seine.build    import BuildCmd
        from seine.packages import Builder
        from seine.sbuild   import BuilderImage
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://busybox
                image:
                    filename: feeds-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        spec = distro(feeds)
        builder = Builder(spec, {}, BuilderImage(spec, {}))
        package = build.image.packages[0]
        return os.path.basename(builder.stamp(package)).rsplit("_", 1)[1]

    def test(self):
        # A snapshot timestamp lives in the feed, not in the distribution's
        # 'uri'. Moving it changes every version the build resolves, so the
        # packages already built are not the ones this asks for.
        before = self.stamp([{"suite": "bookworm",
                              "uri": "https://snapshot.debian.org/archive/debian/20260801T000000Z",
                              "valid-until": False}])
        after  = self.stamp([{"suite": "bookworm",
                              "uri": "https://snapshot.debian.org/archive/debian/20260901T000000Z",
                              "valid-until": False}])
        self.assertNotEqual(before, after, "busybox was not invalidated")

class RebuiltWhenAptPullModeFlips(avocado.Test):
    def stamp(self, offline):
        from seine.build    import BuildCmd
        from seine.packages import Builder
        from seine.sbuild   import BuilderImage
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://busybox
                image:
                    filename: feeds-test.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()
        spec = distro([{"suite": "bookworm"}])
        if offline:
            spec = dict(spec, **{"apt-pull-mode": "offline"})
        builder = Builder(spec, {}, BuilderImage(spec, {}))
        package = build.image.packages[0]
        return os.path.basename(builder.stamp(package)).rsplit("_", 1)[1]

    def test(self):
        # fetch()/the chroot read a package's build inputs from the
        # network or the local vendor repository depending on this
        # setting -- a package already built under one is not what a
        # rebuild under the other would produce.
        online = self.stamp(offline=False)
        offline = self.stamp(offline=True)
        self.assertNotEqual(online, offline, "busybox was not invalidated")

class ComponentsAreADistributionWideDefault(avocado.Test):
    def test(self):
        # A component a board needs -- firmware, typically -- said once
        # rather than in every feed, and without naming the suites, which
        # is what lets a file that adds it stay release-agnostic.
        spec = dict(distro([
            {"suite": "trixie"},
            {"suite": "trixie-security", "uri": "http://security.example.com"},
        ]), components="main non-free-firmware")
        self.assertEqual(apt_sources(spec), [
            "deb http://example.com/debian trixie main non-free-firmware",
            "deb http://security.example.com trixie-security main non-free-firmware",
        ])

class AFeedKeepsItsOwnComponents(avocado.Test):
    def test(self):
        # The default is a default: a vendor feed carrying one component
        # is not made to claim components it does not have.
        spec = dict(distro([
            {"suite": "trixie"},
            {"suite": "vendor", "uri": "https://packages.example.com/apt",
             "components": "non-free"},
        ]), components="main non-free-firmware")
        self.assertEqual(apt_sources(spec), [
            "deb http://example.com/debian trixie main non-free-firmware",
            "deb https://packages.example.com/apt vendor non-free",
        ])

class ComponentsDefaultToMain(avocado.Test):
    def test(self):
        self.assertEqual(apt_sources(distro([{"suite": "trixie"}])),
                         ["deb http://example.com/debian trixie main"])

class BaseFeedIsTheFirstOne(avocado.Test):
    def test(self):
        # mmdebstrap's own convention for a mirror list: the first is what
        # it bootstraps from, everything after is only ever a source added
        # once the rootfs exists.
        self.assertEqual(base_feed(distro([
            {"suite": "bookworm"},
            {"suite": "bookworm-security"},
        ])), feeds(distro([{"suite": "bookworm"},
                           {"suite": "bookworm-security"}]))[0])

    def test_the_synthetic_default_is_the_base_feed_too(self):
        # No 'feeds:' at all is one feed, and that one is the base -- same
        # as every other specification.
        self.assertEqual(base_feed(DISTRO)["suite"], DISTRO["release"])

class EntriesOverridesWhichFeedsAreWritten(avocado.Test):
    def test(self):
        # TargetBootstrap's own use: only base_feed()'s line, not the
        # feeds after it.
        spec = distro([{"suite": "bookworm"}, {"suite": "bookworm-security"}])
        self.assertEqual(apt_sources(spec, entries=[base_feed(spec)]),
                         ["deb http://example.com/debian bookworm main"])

    def test_unset_is_still_every_feed(self):
        spec = distro([{"suite": "bookworm"}, {"suite": "bookworm-security"}])
        self.assertEqual(apt_sources(spec), [
            "deb http://example.com/debian bookworm main",
            "deb http://example.com/debian bookworm-security main",
        ])

class ExtraFeedsAreAppliedBeforePlaybooksRun(avocado.Test):
    def script(self, feed_list):
        from seine.ansible_runner import AnsibleContainerRunner

        written = []
        class Runner(AnsibleContainerRunner):
            def _exec(self, args, check=True):
                written.append(args[-1])
        runner = Runner(None, distro(feed_list), {})
        runner._configure_feeds()
        return "".join(written)

    def test_the_base_feed_is_not_repeated(self):
        made = self.script([{"suite": "bookworm"}, {"suite": "bookworm-security"}])
        self.assertIn("bookworm-security", made)
        self.assertNotIn(" bookworm main", made)

    def test_nothing_runs_for_one_feed(self):
        # base_feed() is already all TargetBootstrap gave the rootfs; there
        # is nothing left here to add.
        self.assertEqual(self.script([{"suite": "bookworm"}]), "")

    def test_written_where_ansible_runner_says(self):
        from seine import ansible_runner
        made = self.script([{"suite": "bookworm"}, {"suite": "bookworm-security"}])
        self.assertIn(ansible_runner.FEEDS_LIST, made)

class TargetBootstrapStaysOnlineRegardlessOfOfflineMode(avocado.Test):
    def test(self):
        # TargetBootstrap's own mmdebstrap step has nothing bind-mounted
        # for it to read a local vendor from -- 'apt-pull-mode: offline'
        # is documented as never reaching it (docs/specification.md), and
        # this is what keeps it that way now that apt_sources() itself
        # will not go offline unless a caller explicitly asks.
        from seine.bootstrap import HostBootstrap, TargetBootstrap
        spec = dict(distro([{"suite": "bookworm"}]),
                   **{"apt-pull-mode": "offline"})
        target = TargetBootstrap(spec, {})
        target.hostBootstrap = HostBootstrap(spec, {})
        self.assertIn("http://example.com/debian bookworm main",
                      target.dockerfile())
        self.assertNotIn("vendor-repo", target.dockerfile())

class RestoringOnlineFeedsUndoesGoingOffline(avocado.Test):
    def test(self):
        from seine.ansible_runner import AnsibleContainerRunner, FEEDS_LIST

        written = []
        class Runner(AnsibleContainerRunner):
            def _exec(self, args, check=True):
                written.append(args[-1])
        spec = dict(distro([{"suite": "bookworm"},
                            {"suite": "bookworm-security"}]),
                   **{"apt-pull-mode": "offline"})
        runner = Runner(None, spec, {})
        runner._restore_online_feeds()
        made = "".join(written)
        self.assertIn("rm -f %s" % FEEDS_LIST, made)
        self.assertIn("http://example.com/debian bookworm main", made)
        self.assertIn("http://example.com/debian bookworm-security main", made)
        self.assertNotIn("vendor-repo", made)

    def test_nothing_runs_online(self):
        from seine.ansible_runner import AnsibleContainerRunner

        written = []
        class Runner(AnsibleContainerRunner):
            def _exec(self, args, check=True):
                written.append(args[-1])
        runner = Runner(None, distro([{"suite": "bookworm"}]), {})
        runner._restore_online_feeds()
        self.assertEqual(written, [])

class OfflineFeedsReplaceWhatTargetBootstrapBaked(avocado.Test):
    def script(self, feed_list):
        from seine.ansible_runner import AnsibleContainerRunner

        written = []
        class Runner(AnsibleContainerRunner):
            def _exec(self, args, check=True):
                written.append(args[-1])
        spec = dict(distro(feed_list), **{"apt-pull-mode": "offline"})
        runner = Runner(None, spec, {})
        runner._configure_feeds()
        return "".join(written)

    def test_a_single_feed_is_still_rewritten(self):
        # Unlike the online case, one feed is not "nothing to do": the
        # baked-in sources.list still points at the network, and this is
        # what stops it being read at all.
        made = self.script([{"suite": "bookworm"}])
        self.assertIn("file:/vendor-repo/bookworm", made)

    def test_the_baked_in_sources_are_removed_first(self):
        made = self.script([{"suite": "bookworm"}])
        self.assertIn("rm -f /etc/apt/sources.list", made)

    # Never one mount/line per feed: the release's own vendor repository
    # (deploy_repository(release), built by the build's own 'vendor'
    # task) already covers every one of the release's feeds -- main,
    # updates, security alike -- so a single deb line and a single
    # deb-src line, both naming 'main extra' together, are what this
    # writes regardless of how many feeds the release itself lists.
    def test_one_deb_and_one_deb_src_line_for_the_release(self):
        made = self.script([{"suite": "bookworm"}, {"suite": "bookworm-security"}])
        self.assertNotIn("bookworm-security", made)
        self.assertIn("file:/vendor-repo/bookworm bookworm main extra", made)
        self.assertEqual(made.count("echo 'deb "), 1)
        self.assertEqual(made.count("echo 'deb-src "), 1)

class VendorRepositoriesAreMountedWhenOffline(avocado.Test):
    def test(self):
        from seine import vendor
        from seine.ansible_runner import AnsibleContainerRunner
        spec = dict(distro([{"suite": "bookworm"}]), **{"apt-pull-mode": "offline"})
        runner = AnsibleContainerRunner(None, spec, {})
        volumes = " ".join(runner._volumes())
        self.assertIn(vendor.deploy_repository("bookworm"), volumes)
        self.assertIn("/vendor-repo/bookworm:ro", volumes)

    def test_nothing_is_mounted_online(self):
        from seine.ansible_runner import AnsibleContainerRunner
        runner = AnsibleContainerRunner(None, distro([{"suite": "bookworm"}]), {})
        volumes = runner._volumes()
        self.assertNotIn("vendor-repo", " ".join(volumes))

class TargetBootstrapBootstrapsFromTheBaseFeedAlone(avocado.Test):
    def dockerfile(self, feed_list):
        from seine.bootstrap import HostBootstrap, TargetBootstrap
        spec = distro(feed_list)
        target = TargetBootstrap(spec, {})
        target.hostBootstrap = HostBootstrap(spec, {})
        return target.dockerfile()

    def test(self):
        # A second feed is fetched from nothing here: it costs this image
        # its sharing with every specification that differs only there.
        made = self.dockerfile([{"suite": "bookworm"},
                                {"suite": "bookworm-security"}])
        self.assertIn("bookworm main", made)
        self.assertNotIn("bookworm-security", made)

    def test_one_feed_is_unaffected(self):
        made = self.dockerfile([{"suite": "bookworm"}])
        self.assertIn("bookworm main", made)

class TargetBootstrapNameFoldsInTheBaseFeed(avocado.Test):
    # Not 'name': avocado.Test already has one of its own.
    def tag(self, feed_list):
        from seine.bootstrap import TargetBootstrap
        return TargetBootstrap(distro(feed_list), {}).name

    def test_a_different_base_feed_is_a_different_name(self):
        # 'uri'/'components' are not spelled out in the name otherwise, so
        # without this two specifications bootstrapping from different
        # mirrors would collide on one tag.
        self.assertNotEqual(
            self.tag([{"suite": "bookworm", "uri": "http://one.example.com"}]),
            self.tag([{"suite": "bookworm", "uri": "http://two.example.com"}]))

    def test_a_different_extra_feed_is_the_same_name(self):
        # base_feed() alone decides the name: a second feed is applied
        # later and has nothing to do with what this image is.
        self.assertEqual(
            self.tag([{"suite": "bookworm"}, {"suite": "bookworm-security"}]),
            self.tag([{"suite": "bookworm"}, {"suite": "bookworm-backports"}]))
