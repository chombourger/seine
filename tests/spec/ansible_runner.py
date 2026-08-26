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

# AnsibleContainerRunner._refresh_vendor_deploy() -- the just-in-time
# rebuild that lets an offline build find its vendor repository in
# deploy/ regardless of whether it survived since 'seine vendor' last
# ran. HostBootstrap/vendor._builder_for() are faked out: what is under
# test is that this calls vendor.index() once per offline suite with a
# builder standing on one shared, created HostBootstrap -- not whether
# podman itself works.
class RefreshVendorDeployIndexesEachOfflineSuite(avocado.Test):
    def test(self):
        created = {"n": 0}
        class FakeHostBootstrap:
            def __init__(self, distro, options):
                pass
            def create(self):
                created["n"] += 1

        indexed = []
        def fake_builder_for(distro, suite, options, hostBootstrap):
            return ("builder-for", suite)
        def fake_index(builder, suite, signer):
            indexed.append((builder, suite))

        cmd = runner(offline_distro())
        with patch("seine.ansible_runner.HostBootstrap", FakeHostBootstrap), \
             patch("seine.vendor._builder_for", fake_builder_for), \
             patch("seine.vendor.index", fake_index):
            cmd._refresh_vendor_deploy()

        self.assertEqual(created["n"], 1)
        self.assertEqual(sorted(indexed),
                         [(("builder-for", "bookworm"), "bookworm"),
                          (("builder-for", "bookworm-security"), "bookworm-security")])

    def test_nothing_offline_never_touches_a_bootstrap(self):
        class FakeHostBootstrap:
            def __init__(self, distro, options):
                raise AssertionError(
                    "HostBootstrap should not be built when nothing is offline")

        cmd = runner(online_distro())
        with patch("seine.ansible_runner.HostBootstrap", FakeHostBootstrap):
            cmd._refresh_vendor_deploy()
