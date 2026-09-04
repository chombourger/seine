#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.bootloader import GrubBootloader, detect

# Plain Python, no guestfs appliance involved -- a fake stands in for 'g'
# and records what was called against it.
class FakeGuestfs:
    def __init__(self, files=(), dirs=()):
        self.files = set(files)
        self.dirs = set(dirs)
        self.calls = []

    def is_file(self, path):
        return path in self.files

    def is_dir(self, path):
        return path in self.dirs

    def sh(self, command):
        self.calls.append(("sh", command))

    def mkdir_p(self, path):
        self.calls.append(("mkdir_p", path))

    def mv(self, src, dst):
        self.calls.append(("mv", src, dst))

class Detection(avocado.Test):
    def test_grub_is_picked_when_present(self):
        g = FakeGuestfs(files=["/usr/sbin/grub-install"])
        bootloader = detect(g, "/dev/sda")
        self.assertIsInstance(bootloader, GrubBootloader)

    def test_nothing_is_picked_when_absent(self):
        g = FakeGuestfs()
        self.assertIsNone(detect(g, "/dev/sda"))

class GrubInstall(avocado.Test):
    def test_efi_reproduces_todays_inline_block(self):
        g = FakeGuestfs(dirs=["/usr/lib/grub/x86_64-efi"])
        GrubBootloader("/dev/sda").install(g, "/efi")
        self.assertEqual(g.calls, [
            ("sh", "grub-install --target x86_64-efi --efi-directory=/efi /dev/sda"),
            ("mkdir_p", "/efi/EFI/boot"),
            ("mv", "/efi/EFI/debian/grubx64.efi", "/efi/EFI/boot/bootx64.efi"),
        ])

    def test_non_efi_passes_no_options(self):
        g = FakeGuestfs()
        GrubBootloader("/dev/sda").install(g, "/efi")
        self.assertEqual(g.calls, [("sh", "grub-install  /dev/sda")])

class GrubAddEntry(avocado.Test):
    def test_runs_update_grub_regardless_of_arguments(self):
        g = FakeGuestfs()
        GrubBootloader("/dev/sda").add_entry(
            g, group_label="main", kernel="vmlinuz", initrd="initrd.img",
            root_partuuid="1234")
        self.assertEqual(g.calls, [("sh", "update-grub")])
