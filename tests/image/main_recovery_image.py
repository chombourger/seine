#!/usr/bin/env python3

import avocado
import os
import re
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import HOST_ARCH

EXAMPLES = os.path.join(path_to_sources, "examples", "main-recovery-image")

# As tests/image/bootloader_multiconfig.py's own: a real build, so this
# does not run unless asked for, and amd64-only for the same reason
# (GrubBootloader's EFI support is x86_64-only here).
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# examples/main-recovery-image/ built end to end -- the same two-groups-
# one-disk shape tests/image/multiconfig.py and
# tests/image/bootloader_multiconfig.py already exercise with throwaway
# specifications, this time against the real files docs/building.md
# points at and a user would actually build.
class MainRecoveryImageBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("grub's EFI support is amd64-only here")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)

    # Read out of dpkg's own status database rather than guessed by
    # path -- same as tests/image/multiconfig.py's own.
    def _installed(self, g, package):
        status = g.read_file("/var/lib/dpkg/status").decode("utf-8", "replace")
        pattern = r"^Package: %s\n(?:[^\n]+\n)*?Status: install ok installed\n" \
            % re.escape(package)
        return re.search(pattern, status, re.MULTILINE) is not None

    def test_builds_and_each_group_lands_on_its_own_partition(self):
        disk = os.path.join(self.workdir, "disk.img")
        filename = os.path.join(self.workdir, "filename.yml")
        with open(filename, "w") as f:
            f.write("image:\n    filename: %s\n" % disk)

        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 os.path.join(EXAMPLES, "disk.yaml"), filename],
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0,
                         "building the disk failed, see %s" % log)
        self.assertTrue(os.path.isfile(disk), "no disk image at %s" % disk)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            # sda1 esp, sda2 main-root, sda3 recovery-root -- declaration
            # order, both roots at default priority.
            g.mount_ro("/dev/sda1", "/")
            self.assertTrue(g.is_file("/grub/grub.cfg"), "no grub.cfg on the ESP")
            cfg = g.read_file("/grub/grub.cfg").decode()
            g.umount("/")
            self.assertEqual(cfg.count("menuentry"), 2,
                             "expected one menuentry per group:\n%s" % cfg)
            self.assertLess(cfg.index("menuentry 'main'"), cfg.index("menuentry 'recovery'"),
                            "'main' (the boot owner) must stay the static default")

            g.mount_ro("/dev/sda2", "/")
            self.assertTrue(self._installed(g, "openssh-server"),
                            "main's own sshd never reached main's partition")
            self.assertFalse(self._installed(g, "dropbear-bin"),
                             "recovery's dropbear leaked into main's partition")
            self.assertEqual(
                len([e for e in g.ls("/boot") if e.startswith("vmlinuz-")]), 1,
                "main should have exactly one kernel of its own")
            g.umount("/")

            g.mount_ro("/dev/sda3", "/")
            self.assertTrue(self._installed(g, "dropbear-bin"),
                            "recovery's own dropbear never reached recovery's partition")
            self.assertFalse(self._installed(g, "openssh-server"),
                             "main's sshd leaked into recovery's partition")
            self.assertEqual(
                len([e for e in g.ls("/boot") if e.startswith("vmlinuz-")]), 1,
                "recovery should have exactly one kernel of its own")
            g.umount("/")
        finally:
            g.close()
        os.remove(disk)

if __name__ == "__main__":
    avocado.main()
