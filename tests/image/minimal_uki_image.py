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

INITRD = os.path.join(path_to_sources, "examples", "minimal-initrd")
UKI = os.path.join(path_to_sources, "examples", "minimal-uki")
IMAGE = os.path.join(path_to_sources, "examples", "minimal-uki-image")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# examples/minimal-uki-image/ built end to end: no grub, no linux-image
# -- systemd-boot's Boot Loader Spec finds the .efi on the XBOOTLDR
# partition on its own. Static content only, guestfs-inspected, as
# tests/image/bootloader_multiconfig.py's own -- no QEMU/mtda in this
# environment to actually boot it.
class MinimalUkiImageBuilds(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if HOST_ARCH != "amd64":
            self.cancel("systemd-boot's EFI support is amd64-only here")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)

    def _build(self, args, log):
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build"] + args,
                cwd=path_to_sources, stdout=f, stderr=subprocess.STDOUT)
        return built.returncode

    def test_boots_via_systemd_boot_not_grub(self):
        disk = os.path.join(self.workdir, "disk.img")
        filename = os.path.join(self.workdir, "filename.yml")
        with open(filename, "w") as f:
            f.write("image:\n    filename: %s\n" % disk)

        steps = [
            ("initrd", ["-v", os.path.join(INITRD, "main.yaml")]),
            ("uki", ["--packages-only", "-v", os.path.join(UKI, "main.yaml")]),
            ("image", ["-v", os.path.join(IMAGE, "main.yaml"), filename]),
        ]
        for name, args in steps:
            log = os.path.join(self.outputdir, "%s.log" % name)
            self.assertEqual(self._build(args, log), 0,
                             "building %s failed, see %s" % (name, log))

        self.assertTrue(os.path.isfile(disk), "no disk image at %s" % disk)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            parts = g.part_list("/dev/sda")
            guids = {p["part_num"]: g.part_get_gpt_type("/dev/sda", p["part_num"])
                     for p in parts}
            self.assertEqual(guids[1], "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                             "partition 1 is not an ESP")
            self.assertEqual(guids[2], "BC13C2FF-59E6-4262-A352-B275FD6F7172",
                             "partition 2 is not an XBOOTLDR partition")
            self.assertEqual(g.vfs_type("/dev/sda2"), "vfat",
                             "XBOOTLDR is not vfat -- UEFI firmware cannot read it")

            g.mount_ro("/dev/sda1", "/")
            self.assertTrue(g.is_file("/EFI/systemd/systemd-bootx64.efi"),
                            "systemd-boot was not installed on the ESP")
            self.assertFalse(g.is_dir("/grub"), "grub has no business on this ESP")
            g.umount("/")

            g.mount_ro("/dev/sda2", "/")
            self.assertTrue(g.is_file("/EFI/Linux/linux-uki-amd64.efi"),
                            "no UKI under XBOOTLDR's /EFI/Linux/")
            g.umount("/")

            g.mount_ro("/dev/sda3", "/")
            self.assertFalse(g.is_file("/boot/grub/grub.cfg"),
                             "grub.cfg has no business in this image")
            g.umount("/")
        finally:
            g.close()
