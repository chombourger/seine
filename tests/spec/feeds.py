#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import apt_sources

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
