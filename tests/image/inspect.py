#!/usr/bin/env python3

import avocado
import copy
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.inspect import Inspector

TEMPLATE = {
    "distribution": {"release": "trixie", "architecture": "amd64",
                     "source": "debian", "uri": "http://ftp.debian.org/debian"},
    "image": {
        "filename": "inspect-test.img",
        "table": "gpt",
        "partitions": [{"label": "rootfs", "where": "/"}],
    },
}

# 'PartitionHandler.parse()' mutates the spec it is given -- a fresh copy
# per Inspector, so one test's parse never bleeds into another's.
def SPEC():
    return copy.deepcopy(TEMPLATE)

# Checked before 'guestfs' is even imported (Inspector.__init__), so this
# needs no appliance and runs everywhere.
class AMissingImage(avocado.Test):
    def test_is_an_oserror_not_a_crash(self):
        self.assertRaises(OSError, Inspector, SPEC(), "/does/not/exist.img")

# The partition/device-mapping logic ('PartitionHandler' parsed, matched
# against 'ph.partitions'/'ph.volumes') is plain Python, no guestfs
# involved -- worth its own hermetic check apart from the real-appliance
# tests below.
class DeviceMapping(avocado.Test):
    def test_a_single_root_partition_is_found(self):
        image = os.path.join(self.workdir, "fake.img")
        with open(image, "wb"):
            pass
        inspector = Inspector(SPEC(), image)
        self.assertEqual(len(inspector.ph.partitions), 1)
        self.assertEqual(inspector.ph.mounts[0]["where"], "/")

# A real, small image, read back through a real 'guestfs' appliance --
# the one thing that cannot be faked here. Cancels rather than fails if
# the appliance cannot launch (no kvm, no readable kernel for supermin,
# etc.), the same as every other guestfs-dependent test in this suite
# (tests/image/images.py).
class AnActualImage(avocado.Test):
    def setUp(self):
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)
        self.image = os.path.join(self.workdir, "inspect-test.img")
        with open(self.image, "wb") as f:
            f.truncate(200 * 1024 * 1024)
        try:
            g = guestfs.GuestFS(python_return_dict=True)
            g.add_drive_opts(self.image, format="raw", readonly=False)
            g.launch()
        except RuntimeError as e:
            self.cancel("guestfs could not launch its appliance: %s" % e)
        g.part_init("/dev/sda", "gpt")
        g.part_add("/dev/sda", "primary", 2048, 200 * 1024 * 1024 // 512 - 2048)
        g.mkfs("ext4", "/dev/sda1")
        g.mount("/dev/sda1", "/")
        g.mkdir("/etc")
        g.write("/etc/hostname", b"inspect-test\n")
        g.ln_sf("/etc/hostname", "/etc/hostname-link")
        g.umount("/")
        g.close()

    def test_lists_a_directory(self):
        with Inspector(SPEC(), self.image) as inspector:
            names = {name for name, kind, size, target in inspector.ls("/")}
        self.assertIn("etc", names)

    def test_a_files_kind_and_size(self):
        with Inspector(SPEC(), self.image) as inspector:
            entries = {name: (kind, size)
                      for name, kind, size, target in inspector.ls("/etc")}
        self.assertEqual(entries["hostname"], ("r", 13))

    def test_a_symlinks_target(self):
        with Inspector(SPEC(), self.image) as inspector:
            entries = {name: target
                      for name, kind, size, target in inspector.ls("/etc")}
        self.assertEqual(entries["hostname-link"], "/etc/hostname")

    def test_is_dir(self):
        with Inspector(SPEC(), self.image) as inspector:
            self.assertTrue(inspector.is_dir("/etc"))
            self.assertFalse(inspector.is_dir("/etc/hostname"))

    # 'read_file', not 'cat': binary-safe, does not stop at an embedded
    # NUL the way 'cat' would.
    def test_cat_reads_the_real_bytes(self):
        with Inspector(SPEC(), self.image) as inspector:
            self.assertEqual(inspector.cat("/etc/hostname"), b"inspect-test\n")

    def test_readlink(self):
        with Inspector(SPEC(), self.image) as inspector:
            self.assertEqual(inspector.readlink("/etc/hostname-link"),
                             "/etc/hostname")

if __name__ == "__main__":
    avocado.main()
