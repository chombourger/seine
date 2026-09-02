#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

import seine.bootstrap as bootstrap
from seine.bootstrap import HostBootstrap

DISTRO = {"source": "debian", "release": "bookworm", "architecture": "amd64",
          "uri": "http://example.com/debian"}

# HostBootstrap(..., host_architecture=...) is what lets debian/build-oci.py
# build a foreign host's bundle via podman's own '--platform', without a
# podman to check it against -- create()'s own build() call is
# monkeypatched instead, the same way tests/bootstrap/feeds.py's own
# offline-mode tests read what a Dockerfile/options would have been.
#
# bootstrap.py's own 'HOST_ARCH' (a plain 'from seine.utils import
# HOST_ARCH') is what create() compares against, not seine.utils's --
# patching that one instead of seine.utils.HOST_ARCH is what actually
# reaches it.
class AForeignHostArchitectureAsksForItsOwnPlatform(avocado.Test):
    def setUp(self):
        self.host_arch = bootstrap.HOST_ARCH
        bootstrap.HOST_ARCH = "amd64"

    def tearDown(self):
        bootstrap.HOST_ARCH = self.host_arch

    def create(self, host_architecture=None):
        hb = HostBootstrap(DISTRO, {}, host_architecture=host_architecture)
        captured = {}
        hb.build = lambda dockerfile, base=None, options=None: \
            captured.update(options=options, dockerfile=dockerfile)
        hb.create()
        return captured

    def test_a_foreign_host_gets_platform_and_its_own_interpreters(self):
        captured = self.create(host_architecture="arm64")
        self.assertIn("--platform", captured["options"])
        self.assertIn("linux/arm64", captured["options"])
        self.assertIn("qemu-x86_64-static", captured["dockerfile"])
        self.assertIn("qemu-i386-static", captured["dockerfile"])
        # Emulated, so no real silicon backs arm64's own armhf compat --
        # that interpreter is needed here too.
        self.assertIn("qemu-arm-static", captured["dockerfile"])

    def test_the_native_host_asks_for_no_platform(self):
        captured = self.create()
        self.assertNotIn("--platform", captured["options"])
        self.assertIn("qemu-aarch64-static", captured["dockerfile"])
        self.assertIn("qemu-arm-static", captured["dockerfile"])

    def test_asking_for_the_native_architecture_explicitly_is_the_same(self):
        captured = self.create(host_architecture="amd64")
        self.assertNotIn("--platform", captured["options"])
