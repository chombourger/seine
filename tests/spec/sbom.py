#!/usr/bin/env python3

import avocado
import io
import json
import os
import shutil
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine            import sbom as sbom_module
from seine.bootstrap  import HostBootstrap, TargetBootstrap
from seine.sbom       import SBOM, installed_sizes, wanted
from seine.utils      import ContainerEngine, HOST_ARCH

# A root file-system as 'container export' hands one over: the few files
# debsbom reads, and a good deal of what it does not.
ROOTFS = [
    "var/lib/dpkg/status",
    "var/lib/dpkg/status-old",
    "var/lib/dpkg/arch-native",
    "var/lib/dpkg/info/bash.list",
    "var/lib/apt/extended_states",
    "var/lib/apt/lists/deb.debian.org_dists_trixie_InRelease",
    "usr/bin/dpkg",
]

def tarball(path, names=ROOTFS):
    with tarfile.open(path, "w") as tar:
        for name in names:
            payload = name.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return path

# The distribution the real root file-system is bootstrapped from. The
# same one the examples use, and the machine's own architecture, so the
# bootstrap images a build already left behind get reused rather than a
# second set produced for the tests alone.
DISTRO = {"source": "debian", "release": "bookworm", "architecture": HOST_ARCH,
          "uri": "http://ftp.debian.org/debian"}

# Bootstraps a root file-system the way a build does and exports it as the
# tarball an SBOM is made from.
def bootstrap(distro, path):
    options = {"verbose": False}
    host = HostBootstrap(distro, options)
    target = TargetBootstrap(distro, options)
    host.create()
    target.create(host)

    cid = ContainerEngine.check_output(
        ["container", "create", target.name, "true"]).decode().strip()
    try:
        ContainerEngine.run(["container", "export", "-o", path, cid], check=True)
    finally:
        ContainerEngine.run(["container", "rm", cid], check=False)
    return path

def tree(root):
    found = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)

# Stands in for the container engine so the tests can see what debsbom
# would have been run with, without pulling its image or running it.
class Engine:
    def __init__(self, testcase, on_run=None):
        self.testcase = testcase
        self.on_run = on_run
        self.commands = []

    def run(self, cmd, check=False):
        self.commands.append(cmd)
        if self.on_run:
            self.on_run(cmd)

    def scratch(self):
        return self.testcase.workdir

    # The engine is reached through a module-global, so it is swapped in
    # place and put back rather than injected.
    def __enter__(self):
        self.saved = sbom_module.ContainerEngine
        sbom_module.ContainerEngine = self
        return self

    def __exit__(self, *args):
        sbom_module.ContainerEngine = self.saved

def mount(cmd, target):
    # The host side of the '-v host:container:options' argument mounted at
    # 'target' in the container.
    for index, arg in enumerate(cmd):
        if arg == "-v" and cmd[index + 1].split(":")[1] == target:
            return cmd[index + 1].split(":")[0]
    return None

class DebsbomInputsAreTakenFromTheTarball(avocado.Test):
    def test(self):
        # Both spellings 'container export' may use, and the apt lists
        # taken as a directory rather than a single file.
        self.assertTrue(wanted("var/lib/dpkg/status"))
        self.assertTrue(wanted("./var/lib/apt/extended_states"))
        self.assertTrue(wanted("var/lib/apt/lists/deb.debian.org_dists_trixie_InRelease"))

class TheRestOfTheRootFileSystemIsLeftAlone(avocado.Test):
    def test(self):
        # Including the architecture dpkg recorded: it is not in every
        # root file-system and debsbom is told the architecture instead.
        self.assertFalse(wanted("var/lib/dpkg/arch-native"))
        self.assertFalse(wanted("usr/bin/dpkg"))
        self.assertFalse(wanted("var/lib/dpkg/info/bash.list"))
        self.assertFalse(wanted("var/lib/dpkg/status-old"))

class TheSBOMSitsNextToTheImageItDescribes(avocado.Test):
    def test(self):
        # debsbom appends '.spdx.json' to what it is given.
        self.assertEqual(SBOM(DISTRO, {"sbom": True})._output_file("/tmp/pc-image.img"),
                         "/tmp/pc-image-sbom")
        self.assertIsNone(SBOM(DISTRO, {"sbom": False})._output_file("/tmp/pc-image.img"))

class OnlyWhatDebsbomReadsIsUnpacked(avocado.Test):
    def test(self):
        # Unpacking a whole root file-system reads far more than the three
        # files this needs, so only those three are taken.
        root = os.path.join(self.workdir, "root")
        SBOM(DISTRO)._extract(tarball(os.path.join(self.workdir, "root.tar")), root)
        self.assertEqual(tree(root), [
            "var/lib/apt/extended_states",
            "var/lib/apt/lists/deb.debian.org_dists_trixie_InRelease",
            "var/lib/dpkg/status",
        ])

class DebsbomIsRunOnTheExtractedRootFileSystem(avocado.Test):
    def test(self):
        image = os.path.join(self.workdir, "pc-image.img")
        seen = {}

        def on_run(cmd):
            # The extracted root file-system is only there for as long as
            # debsbom runs, so it is inspected from inside the run.
            root = mount(cmd, "/rootfs")
            self.assertIsNotNone(root, "root file-system not mounted: %s" % cmd)
            seen["root"] = root
            seen["status"] = os.path.exists(
                os.path.join(root, "var/lib/dpkg/status"))

        with Engine(self, on_run) as engine:
            SBOM(DISTRO, {"sbom": True}).generate(
                tarball(os.path.join(self.workdir, "root.tar")), image)

        self.assertEqual(len(engine.commands), 1)
        cmd = engine.commands[0]
        self.assertTrue(seen["status"], "dpkg status not extracted for debsbom")
        self.assertFalse(os.path.exists(seen["root"]),
                         "extracted root file-system outlived the build")

        # The image's directory is mounted under its own path so the SBOM
        # debsbom writes lands next to the image itself.
        self.assertIn(self.workdir + ":" + self.workdir + ":z", cmd)
        self.assertEqual(cmd[cmd.index(sbom_module.DEBSBOM_IMAGE) + 1:],
                         ["debsbom", "generate", "-r", "/rootfs", "-t", "spdx",
                          "--distro-arch", DISTRO["architecture"],
                          "-o", os.path.join(self.workdir, "pc-image-sbom")])

class AnSBOMIsActuallyBuilt(avocado.Test):
    """
    :avocado: tags=container
    """
    # Bootstrapping the root file-system and pulling the debsbom image on
    # top of it is what this has to leave room for.
    timeout = 1800

    def test(self):
        # The one test that builds an SBOM for real, from a root
        # file-system bootstrapped the way a build makes one: everything
        # above stops at the command line and would not notice debsbom
        # reading the root file-system differently than it used to.
        if shutil.which("podman") is None:
            self.cancel("podman is needed to bootstrap a root file-system")

        image = os.path.join(self.workdir, "pc-image.img")
        root = bootstrap(DISTRO, os.path.join(self.workdir, "root.tar"))
        SBOM(DISTRO, {"sbom": True}).generate(root, image)

        with open(os.path.join(self.workdir, "pc-image-sbom.spdx.json")) as f:
            spdx = json.load(f)
        packages = {p["name"]: p for p in spdx["packages"]}

        # Packages every Debian root file-system has, and the source
        # package one of them was built from -- which is what having moved
        # off a generic scanner bought us, since that is what CVEs are
        # filed against.
        self.assertIn("base-files", packages)
        self.assertIn("libc6", packages)
        self.assertIn("glibc", packages)

class NoSBOMWasAskedForSoNoneIsBuilt(avocado.Test):
    def test(self):
        with Engine(self) as engine:
            SBOM(DISTRO, {"sbom": False}).generate(
                tarball(os.path.join(self.workdir, "root.tar")),
                os.path.join(self.workdir, "pc-image.img"))
        self.assertEqual(engine.commands, [])

# A real 'var/lib/dpkg/status', not the name-echoing 'tarball()' above --
# 'installed_sizes()' reads its content, not just whether the path exists.
def status_tarball(path, status):
    with tarfile.open(path, "w") as tar:
        payload = status.encode()
        info = tarfile.TarInfo("./var/lib/dpkg/status")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return path

STATUS = """Package: bash
Status: install ok installed
Installed-Size: 7000
Version: 5.2

Package: libc6
Status: install ok installed
Installed-Size: 12000
Version: 2.37

Package: no-size-known
Status: install ok installed
Version: 1.0
"""

class InstalledSizesAreReadFromDpkgStatus(avocado.Test):
    def test_largest_first(self):
        path = status_tarball(os.path.join(self.workdir, "root.tar"), STATUS)
        self.assertEqual(installed_sizes(path), [("libc6", 12000), ("bash", 7000)])

    # No 'Installed-Size:' at all is a package this can say nothing
    # about, not a crash and not a zero.
    def test_a_package_with_no_recorded_size_is_left_out(self):
        path = status_tarball(os.path.join(self.workdir, "root.tar"), STATUS)
        names = [name for name, _ in installed_sizes(path)]
        self.assertNotIn("no-size-known", names)

    def test_no_status_file_at_all_is_empty_not_an_error(self):
        path = tarball(os.path.join(self.workdir, "root.tar"), names=["usr/bin/dpkg"])
        self.assertEqual(installed_sizes(path), [])

    def test_a_missing_or_unreadable_tarball_is_empty_not_an_error(self):
        self.assertEqual(installed_sizes("/does/not/exist.tar"), [])
        garbage = os.path.join(self.workdir, "garbage.tar")
        with open(garbage, "w") as f:
            f.write("this is not a tarball")
        self.assertEqual(installed_sizes(garbage), [])
