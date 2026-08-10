#!/usr/bin/env python3

import avocado
import glob
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import ContainerEngine

EXAMPLES = os.path.join(path_to_sources, "examples")

# Building an image takes a kernel build with it, so these do not run
# unless asked for. Not a tag alone: a tag says which tests to select,
# and the default run selects everything.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# Which fragment builds the 6.18 kernel for a release. bookworm names a
# version because the packaging it needs is in backports; trixie takes
# what the release has. Both describe the same kernel, which is the point
# of them being two files rather than two copies.
KERNELS = {
    "bookworm": "linux-6.18/bookworm.yml",
    "trixie":   "linux-6.18/kernel.yml",
}

# An image built from the examples as they are shipped, for one release
# and one board. Nothing is copied here: the specification is the files
# under examples/, composed on the command line the way a user composes
# them, with a release chosen at the front and the kernel fragment at the
# back. What this test adds is where to write the image, so four of them
# can be built without agreeing on a filename.
#
# The kernel is part of it on purpose. A build that takes the
# distribution's kernel exercises none of what makes these examples
# interesting -- the graft, the flavour coming from the architecture, the
# cross toolchain the profile selects, the component the firmware needs.
class Image(avocado.Test):
    """
    :avocado: disable
    """

    # A kernel, a root file-system and an image, twice over for an
    # emulated architecture. Not a limit that means anything -- it is
    # there so a wedged build eventually reports rather than hangs.
    timeout = 21600

    architecture = None
    image = None
    release = None

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds images; this takes hours")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")

    @property
    def filename(self):
        return os.path.join(self.workdir, "%s-%s.img" % (self.image, self.release))

    def specification(self):
        names = [
            "common/%s.yaml" % self.release,
            "common/%s.yaml" % self.architecture,
            "common/%s.yaml" % self.image,
            "common/conf-accounts.yaml",
            "common/conf-locales.yaml",
            KERNELS[self.release],
        ]
        specs = [os.path.join(EXAMPLES, name) for name in names]
        for spec in specs:
            self.assertTrue(os.path.isfile(spec), "no such specification: %s" % spec)

        # Where to write it, which is the one thing the examples cannot
        # say for a test building four of them.
        where = os.path.join(self.workdir, "filename.yml")
        with open(where, "w") as f:
            f.write("image:\n    filename: %s\n" % self.filename)
        return specs + [where]

    def test(self):
        # ansible-playbook lives beside the python running the tests when
        # they are run from a virtual environment, and the build needs it.
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))

        build = subprocess.run(
            [sys.executable, "./seine.py", "build", "-v"] + self.specification(),
            cwd=path_to_sources, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "wb") as f:
            f.write(build.stdout)
        self.assertEqual(build.returncode, 0,
                         "building %s for %s failed, see %s"
                         % (self.image, self.release, log))

        self.assertTrue(os.path.isfile(self.filename),
                        "no image at %s" % self.filename)
        self.assertGreater(os.path.getsize(self.filename), 0, "the image is empty")

        # The kernel that came out is the grafted one rather than the
        # distribution's: an image is produced either way, and the
        # difference is only visible in what was built to make it.
        repository = ContainerEngine.packages(self.release, self.architecture)
        debs = glob.glob(os.path.join(repository,
                                      "linux-image-*unreleased*.deb"))
        self.assertNotEqual(debs, [], "no grafted kernel in %s" % repository)

class PcImageBookworm(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "amd64"
    image = "pc-image"
    release = "bookworm"

class PcImageTrixie(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "amd64"
    image = "pc-image"
    release = "trixie"

# arm64 on an amd64 machine: the kernel is cross-compiled and the imager
# appliance runs under emulation, which is the slowest of the four and the
# one that has the most to say when it breaks.
class Rpi4ImageBookworm(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "arm64"
    image = "rpi4-image"
    release = "bookworm"

class Rpi4ImageTrixie(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "arm64"
    image = "rpi4-image"
    release = "trixie"
