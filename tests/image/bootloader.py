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
        self.written = {}

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

    def write(self, path, content):
        self.written[path] = content

    def write_append(self, path, content):
        self.written[path] = self.written.get(path, b"") + content

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
        self.assertEqual(g.written, {})

    def test_non_efi_passes_no_options(self):
        g = FakeGuestfs()
        GrubBootloader("/dev/sda").install(g, "/efi")
        self.assertEqual(g.calls, [("sh", "grub-install  /dev/sda")])

    def test_boot_directory_relocates_grub_cfg_to_it(self):
        g = FakeGuestfs(dirs=["/usr/lib/grub/x86_64-efi"])
        GrubBootloader("/dev/sda").install(g, "/efi", boot_directory="/efi")
        self.assertEqual(g.calls[0], (
            "sh", "grub-install --target x86_64-efi --efi-directory=/efi "
                  "--boot-directory=/efi /dev/sda"))
        self.assertIn("/efi/grub/grub.cfg", g.written)

class GrubAddEntry(avocado.Test):
    def test_bare_call_runs_update_grub(self):
        g = FakeGuestfs()
        GrubBootloader("/dev/sda").add_entry(g)
        self.assertEqual(g.calls, [("sh", "update-grub")])

    def test_a_group_entry_is_appended_to_grub_cfg(self):
        g = FakeGuestfs(dirs=["/usr/lib/grub/x86_64-efi"])
        bootloader = GrubBootloader("/dev/sda")
        bootloader.install(g, "/efi", boot_directory="/efi")
        bootloader.add_entry(
            g, group_label="main", kernel="/boot/vmlinuz-6.1", initrd="/boot/initrd.img-6.1",
            root_partuuid="1234-5678", root_label="main-root")
        cfg = g.written["/efi/grub/grub.cfg"].decode()
        self.assertIn("menuentry 'main'", cfg)
        self.assertIn("search --no-floppy --set=root --label main-root", cfg)
        self.assertIn("linux /boot/vmlinuz-6.1 root=PARTUUID=1234-5678 ro", cfg)
        self.assertIn("initrd /boot/initrd.img-6.1", cfg)
        self.assertEqual(g.calls, [
            ("sh", "grub-install --target x86_64-efi --efi-directory=/efi "
                   "--boot-directory=/efi /dev/sda"),
            ("mkdir_p", "/efi/EFI/boot"),
            ("mv", "/efi/EFI/debian/grubx64.efi", "/efi/EFI/boot/bootx64.efi"),
        ])

    def test_two_group_entries_both_land_in_the_same_cfg(self):
        g = FakeGuestfs(dirs=["/usr/lib/grub/x86_64-efi"])
        bootloader = GrubBootloader("/dev/sda")
        bootloader.install(g, "/efi", boot_directory="/efi")
        bootloader.add_entry(
            g, group_label="main", kernel="/boot/vmlinuz-main", initrd="/boot/initrd.img-main",
            root_partuuid="aaaa", root_label="main-root")
        bootloader.add_entry(
            g, group_label="recovery", kernel="/boot/vmlinuz-rec", initrd="/boot/initrd.img-rec",
            root_partuuid="bbbb", root_label="recovery-root")
        cfg = g.written["/efi/grub/grub.cfg"].decode()
        self.assertEqual(cfg.count("menuentry"), 2)
        self.assertLess(cfg.index("menuentry 'main'"), cfg.index("menuentry 'recovery'"))
        self.assertIn("set default=0", cfg)

if __name__ == "__main__":
    avocado.main()
