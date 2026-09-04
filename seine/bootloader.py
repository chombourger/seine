# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0


class Bootloader:
    def __init__(self, device):
        self.device = device

    def detect(self, g):
        raise NotImplementedError

    def install(self, g, esp_mount):
        raise NotImplementedError

    def add_entry(self, g, group_label=None, kernel=None, initrd=None,
                  root_partuuid=None, **opts):
        raise NotImplementedError


class GrubBootloader(Bootloader):
    def detect(self, g):
        return g.is_file("/usr/sbin/grub-install")

    def install(self, g, esp_mount):
        options = ""
        if g.is_dir("/usr/lib/grub/x86_64-efi"):
            options = "--target x86_64-efi --efi-directory=%s" % esp_mount
        g.sh("grub-install %s %s" % (options, self.device))
        if g.is_dir("/usr/lib/grub/x86_64-efi"):
            g.mkdir_p("%s/EFI/boot" % esp_mount)
            g.mv("%s/EFI/debian/grubx64.efi" % esp_mount,
                 "%s/EFI/boot/bootx64.efi" % esp_mount)

    # Only one entry exists today: update-grub's own auto-discovery
    # against the single mounted root. group_label/kernel/initrd/
    # root_partuuid are accepted but unused until per-entry authoring
    # is implemented.
    def add_entry(self, g, group_label=None, kernel=None, initrd=None,
                  root_partuuid=None, **opts):
        g.sh("update-grub")


REGISTRY = [GrubBootloader]


def detect(g, device):
    for cls in REGISTRY:
        bootloader = cls(device)
        if bootloader.detect(g):
            return bootloader
    return None
