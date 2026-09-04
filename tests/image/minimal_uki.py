#!/usr/bin/env python3

import avocado
import glob
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import ContainerEngine
from seine.utils import HOST_ARCH

INITRD = os.path.join(path_to_sources, "examples", "minimal-initrd")
UKI = os.path.join(path_to_sources, "examples", "minimal-uki")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# examples/minimal-uki/ built end to end, both 'tool:' backends: each
# wraps linux-image-amd64 and the deployed initrd into a .efi, published
# to the seine package repository like any other rebuilt package.
class MinimalUkiBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds packages; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("examples/common/amd64.yaml is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build packages")
        if shutil.which("dpkg-deb") is None:
            self.cancel("dpkg-deb is needed to inspect the built package")

    def _build(self, args, log):
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build"] + args,
                cwd=path_to_sources, stdout=f, stderr=subprocess.STDOUT)
        return built.returncode

    def test_builds_and_publishes_the_uki_package(self):
        initrd_log = os.path.join(self.outputdir, "initrd.log")
        self.assertEqual(
            self._build(["-v", os.path.join(INITRD, "main.yaml")], initrd_log),
            0, "building the initrd failed, see %s" % initrd_log)

        uki_log = os.path.join(self.outputdir, "uki.log")
        self.assertEqual(
            self._build(["--packages-only", "-v",
                        os.path.join(UKI, "main.yaml")], uki_log),
            0, "building the uki packages failed, see %s" % uki_log)

        repository = ContainerEngine.packages("trixie")
        for name in ["linux-uki-amd64", "linux-uki-efibootguard-amd64"]:
            debs = glob.glob(os.path.join(repository, "%s_*.deb" % name))
            self.assertEqual(len(debs), 1,
                             "expected one %s .deb in %s, found %s"
                             % (name, repository, debs))

            extracted = tempfile.mkdtemp(dir=self.workdir)
            subprocess.run(["dpkg-deb", "-x", debs[0], extracted], check=True)
            efi = os.path.join(extracted, "boot", "EFI", "Linux",
                               "%s.efi" % name)
            self.assertTrue(os.path.isfile(efi), "no .efi at %s" % efi)

            identified = subprocess.run(
                ["file", efi], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            self.assertIn("pe32+", identified.stdout.lower(),
                          "not a PE32+ executable: %s" % identified.stdout)
