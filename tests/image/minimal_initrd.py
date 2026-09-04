#!/usr/bin/env python3

import avocado
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import HOST_ARCH

EXAMPLES = os.path.join(path_to_sources, "examples", "minimal-initrd")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# examples/minimal-initrd/ built end to end: 'initrd:' pulls
# /boot/initrd.img-* out of the built root file-system and deploys it,
# with nothing else in the specification ever reaching an Imager.
class MinimalInitrdBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 1800

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("examples/common/amd64.yaml is amd64-only")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build a root file-system")
        if shutil.which("file") is None:
            self.cancel("'file' is needed to check the deployed initrd")

    def test_builds_and_deploys_exactly_one_initrd(self):
        deployed = os.path.join(self.workdir, "minimal.img")
        filename = os.path.join(self.workdir, "filename.yml")
        with open(filename, "w") as f:
            f.write("initrd:\n    filename: %s\n" % deployed)

        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(EXAMPLES, "main.yaml"), filename],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0,
                         "building the initrd failed, see %s" % log)
        self.assertTrue(os.path.isfile(deployed),
                        "no initrd deployed at %s" % deployed)

        identified = subprocess.run(
            ["file", "-z", deployed], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.assertIn("cpio", identified.stdout.lower(),
                      "not a cpio archive: %s" % identified.stdout)
