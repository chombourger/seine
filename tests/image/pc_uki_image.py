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

SPEC = os.path.join("examples", "pc-uki-image", "main.yaml")

PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# examples/pc-uki-image/ built end to end from a single 'seine build' --
# 'multiconfig: after:' chains initrd, then UKI, then rootfs, none
# pre-built. Static content only, guestfs-inspected: no QEMU/mtda here
# to actually boot it.
class PcUkiImageBuilds(avocado.Test):
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

    # No 'SEINE_BUILD_DIR' override: that puts podman's own rootless
    # container storage under 'self.workdir' too, which avocado cannot
    # clean up afterwards (subuid-mapped files a plain rmtree can't
    # remove). A side-loaded peer file redirects just 'image: filename:'
    # instead, the same as tests/image/minimal_uki_image.py.
    def test_the_whole_pipeline_builds_from_one_call(self):
        disk = os.path.join(self.workdir, "disk.img")
        filename = os.path.join(self.workdir, "filename.yml")
        with open(filename, "w") as f:
            f.write("image:\n    filename: %s\n" % disk)

        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", SPEC, filename],
                cwd=path_to_sources, stdout=f, stderr=subprocess.STDOUT)
        self.assertEqual(built.returncode, 0,
                         "building the pipeline failed, see %s" % log)
        self.assertTrue(os.path.isfile(disk), "no disk image at %s" % disk)

        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(disk, format="raw", readonly=True)
        g.launch()
        try:
            parts = g.part_list("/dev/sda")
            guids = {p["part_num"]: g.part_get_gpt_type("/dev/sda", p["part_num"])
                     for p in parts}
            # sda1 esp, sda2 xbootldr, sda3 var, sda4 usr, sda5 usr-verity,
            # sda6 root -- declaration order, all default priority
            # (PartitionHandler.parse()).
            self.assertEqual(guids[1], "C12A7328-F81F-11D2-BA4B-00A0C93EC93B",
                             "partition 1 is not an ESP")
            self.assertEqual(guids[2], "BC13C2FF-59E6-4262-A352-B275FD6F7172",
                             "partition 2 is not an XBOOTLDR partition")
            self.assertEqual(guids[3], "4D21B016-B534-45C2-A9FB-5C16E091FD2D",
                             "partition 3 is not a DPS '/var' partition")
            self.assertEqual(guids[4], "8484680C-9521-48C6-9C11-B0720656F69E",
                             "partition 4 is not a DPS x86-64 '/usr' partition")
            self.assertEqual(guids[5], "77FF5F63-E7B6-4633-ACF4-1565B864C0E6",
                             "partition 5 is not a DPS x86-64 '/usr' Verity partition")
            self.assertEqual(guids[6], "4F68BCE3-E8CD-4DB1-96E7-FBCAF984B709",
                             "partition 6 is not a DPS x86-64 root partition")
            self.assertEqual(g.vfs_type("/dev/sda2"), "vfat",
                             "XBOOTLDR is not vfat -- UEFI firmware cannot read it")

            # 'verity: true' on 'usr' means its GPT GUID is no longer
            # parted's random default -- it's the built root hash's own
            # high 128 bits, and 'usr-verity's the low 128 bits (see
            # Imager._build_verity()).
            usr_guid = g.part_get_gpt_guid("/dev/sda", 4).replace("-", "")
            usr_verity_guid = g.part_get_gpt_guid("/dev/sda", 5).replace("-", "")
            self.assertEqual(len(usr_guid) + len(usr_verity_guid), 64,
                             "usr/usr-verity GUIDs are not two 128-bit root-hash halves")

            g.mount_ro("/dev/sda2", "/")
            self.assertTrue(g.is_file("/EFI/Linux/linux-uki-amd64.efi"),
                            "no UKI under XBOOTLDR's /EFI/Linux/")
            g.umount("/")

            g.mount_ro("/dev/sda6", "/")
            fstab = g.read_file("/etc/fstab").decode()
            self.assertIn("/var/", fstab, "no separate '/var' mount in fstab")
            self.assertNotIn("/usr ", fstab,
                             "verity-protected '/usr' should have no fstab entry "
                             "-- it's mounted by systemd-veritysetup-generator")
            g.umount("/")
        finally:
            g.close()
        os.remove(disk)

if __name__ == "__main__":
    avocado.main()
