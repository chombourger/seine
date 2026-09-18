#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import os
import shutil
import sys
import tempfile

from unittest.mock import patch

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_DEPLOY_DIR"] = tempfile.mkdtemp(prefix="seine-tests-deploy-")
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"], ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_DEPLOY_DIR"], ignore_errors=True)

from seine.ansible_runner import AnsibleContainerRunner
from seine.utils import locale_purge_script
from seine import vendor

def offline_distro():
    return {"source": "debian", "release": "bookworm", "architecture": "amd64",
           "uri": "http://example.com/debian", "apt-pull-mode": "offline",
           "feeds": [{"suite": "bookworm"}, {"suite": "bookworm-security"}]}

def online_distro():
    return {"source": "debian", "release": "bookworm", "architecture": "amd64",
           "uri": "http://example.com/debian",
           "feeds": [{"suite": "bookworm"}]}

def runner(distro):
    return AnsibleContainerRunner(None, distro, {})

# _volumes() -- pure once constructed (no container/podman call of its own),
# so it is exercised directly rather than through a real run().
class VolumesMountTheDeliveredVendorRepository(avocado.Test):
    def test(self):
        cmd = runner(offline_distro())
        volumes = cmd._volumes()
        expected = vendor.deploy_repository("bookworm")
        self.assertIn("%s:%s:ro" % (expected, "/vendor-repo/bookworm"), volumes)
        # Never the cache -- see vendor.repository()'s own comment on why
        # it carries no pool/dists view for this to read back.
        self.assertNotIn("%s:%s:ro" % (vendor.repository("bookworm"),
                                       "/vendor-repo/bookworm"), volumes)

    def test_nothing_offline_mounts_no_vendor_at_all(self):
        cmd = runner(online_distro())
        volumes = cmd._volumes()
        self.assertNotIn("/vendor-repo/bookworm", " ".join(volumes))

# _configure_feeds() -- offline mode replaces every apt source with a
# single vendor entry for the build's own release: one deb line and one
# deb-src line, both naming 'main extra' together, never one pair per
# suite/component. The rebuild that used to sit in
# AnsibleContainerRunner._refresh_vendor_deploy() is gone: 'rootfs' now
# waits on the 'vendor' task (see image.py's own task graph), which has
# already built deploy_repository(release) by the time this runs, so
# there is nothing left here to refresh just-in-time.
class ConfigureFeedsWritesOneVendorEntryForTheRelease(avocado.Test):
    def test(self):
        cmd = runner(offline_distro())
        written = []
        with patch.object(cmd, "_exec", lambda args, check=True: written.append(args)):
            cmd._configure_feeds()

        self.assertEqual(len(written), 1)
        script = written[0][-1]
        self.assertEqual(script.count("deb "), 1)
        self.assertEqual(script.count("deb-src "), 1)
        self.assertIn("file:/vendor-repo/bookworm bookworm main extra", script)

    def test_online_keeps_every_feed_but_base_feed(self):
        cmd = runner(online_distro())
        written = []
        with patch.object(cmd, "_exec", lambda args, check=True: written.append(args)):
            cmd._configure_feeds()
        self.assertEqual(len(written), 0)

    # base_feed() can be anywhere in the list, not just first (e.g. another
    # release's suites listed before it) -- it must still drop out of 'extra'.
    def test_online_base_feed_not_first_is_still_excluded(self):
        distro = online_distro()
        distro["feeds"] = [{"suite": "trixie"}, {"suite": "bookworm"},
                           {"suite": "bookworm-security"}]
        cmd = runner(distro)
        written = []
        with patch.object(cmd, "_exec", lambda args, check=True: written.append(args)):
            cmd._configure_feeds()
        self.assertEqual(len(written), 1)
        script = written[0][-1]
        self.assertIn(" trixie ", script)
        self.assertIn("bookworm-security", script)
        self.assertNotIn(" bookworm ", script)

# _finalize() calls locale_purge_script(self.locales) once, after every
# install is done -- a dpkg extraction filter alone would miss the base
# rootfs's own packages, unpacked before any filter could exist.
class FinalizeSweepsLocalesOnceEverythingIsInstalled(avocado.Test):
    def test_the_locales_default_is_used(self):
        cmd = runner(online_distro())
        self.assertEqual(cmd.locales, ["en"])

    def test_a_spec_can_ask_for_more(self):
        cmd = AnsibleContainerRunner(None, online_distro(), {}, locales=["en", "fr"])
        self.assertEqual(cmd.locales, ["en", "fr"])

# locale_purge_script() -- keeps both the bare and regional form of
# whatever is asked for, since a package's own translations live under
# the bare code (e.g. /usr/share/locale/fr/, not .../fr_FR/).
class LocalePurgeScriptKeepsOnlyWhatWasAskedFor(avocado.Test):
    def test_a_regional_code_keeps_its_bare_language_too(self):
        script = locale_purge_script(["fr_FR"])
        self.assertIn('case "$d" in fr|fr_FR)', script)

    def test_default_is_english_alone(self):
        script = locale_purge_script(["en"])
        self.assertIn('case "$d" in en)', script)
