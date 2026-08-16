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

from seine.utils import apt_sources, base_feed, feeds

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
# And no key either: what a build signs with is read from the environment,
# so a developer who signs their own builds would otherwise run a
# different suite than everyone else.
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
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
