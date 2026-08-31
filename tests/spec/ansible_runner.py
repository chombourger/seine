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

    def test_online_keeps_every_feed_but_the_first(self):
        cmd = runner(online_distro())
        written = []
        with patch.object(cmd, "_exec", lambda args, check=True: written.append(args)):
            cmd._configure_feeds()
        self.assertEqual(len(written), 0)
