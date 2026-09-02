#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.bootstrap import QEMU_ARCHS, _qemu_fetch

# What HostBootstrap's own qemu-user-static footprint reduction fetches:
# only the interpreter this host needs to cross-bootstrap the other
# architectures, never the whole package. A pure function of HOST_ARCH,
# so this is tested directly rather than through a real podman build --
# HostBootstrap has no dockerfile()-without-building split the way
# TargetBootstrap does (see bootstrap.py's own comment on that), but the
# fetch fragment itself needs no podman to read.
class AnAmd64HostFetchesTheInterpretersItNeeds(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("amd64")
        self.assertIn("qemu-aarch64-static", fetched)
        self.assertIn("qemu-arm-static", fetched)
        # Native CPU compat, no interpreter fetched for either.
        self.assertNotIn("qemu-i386-static", fetched)
        self.assertNotIn("qemu-x86_64-static", fetched)

class AnArm64HostFetchesTheInterpretersItNeeds(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("arm64")
        self.assertIn("qemu-x86_64-static", fetched)
        self.assertIn("qemu-i386-static", fetched)
        # Native CPU compat, no interpreter fetched for either.
        self.assertNotIn("qemu-arm-static", fetched)
        self.assertNotIn("qemu-aarch64-static", fetched)

class AnEmulatedHostAlsoFetchesItsCompatInterpreter(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("arm64", emulated=True)
        self.assertIn("qemu-x86_64-static", fetched)
        self.assertIn("qemu-i386-static", fetched)
        # No real silicon behind an emulated host, so its compat
        # architecture (armhf on arm64) needs an interpreter too.
        self.assertIn("qemu-arm-static", fetched)

        fetched = _qemu_fetch("amd64", emulated=True)
        self.assertIn("qemu-aarch64-static", fetched)
        self.assertIn("qemu-arm-static", fetched)
        self.assertIn("qemu-i386-static", fetched)

class NeitherHostFetchesItsOwnArchitecture(avocado.Test):
    def test(self):
        self.assertNotIn("amd64", QEMU_ARCHS["arm64"])
        self.assertNotIn("arm64", QEMU_ARCHS["amd64"])

class AnUnlistedHostArchitectureFetchesNothing(avocado.Test):
    def test(self):
        self.assertEqual(_qemu_fetch("riscv64"), "true")

class BothPackageNamesAreDownloadedNeverInstalled(avocado.Test):
    def test(self):
        fetched = _qemu_fetch("amd64")
        self.assertIn("apt-get download qemu-user-static qemu-user", fetched)
        self.assertNotIn("apt-get install", fetched)
