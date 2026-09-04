# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# ext2 is loaded explicitly: reading a *different* group's own /boot is
# the only reason this build needs it, unlike part_gpt/fat which are
# already embedded in any x86_64-efi core.img.
GRUB_CFG_HEADER = """insmod part_gpt
insmod fat
insmod ext2
set timeout=5
set default=0

"""


class Bootloader:
    def __init__(self, device):
        self.device = device

    def detect(self, g):
        raise NotImplementedError

    def install(self, g, esp_mount, **opts):
        raise NotImplementedError

    def add_entry(self, g, group_label=None, kernel=None, initrd=None,
                  root_partuuid=None, **opts):
        raise NotImplementedError


class GrubBootloader(Bootloader):
    def detect(self, g):
        return g.is_file("/usr/sbin/grub-install")

    # 'boot_directory' left unset reproduces today's single-rootfs
    # behaviour exactly (grub-install's own default: the mounted root's
    # own /boot). A 'multiconfig:' build passes the ESP itself instead.
    def install(self, g, esp_mount, **opts):
        boot_directory = opts.get("boot_directory")
        options = ""
        if g.is_dir("/usr/lib/grub/x86_64-efi"):
            options = "--target x86_64-efi --efi-directory=%s" % esp_mount
            if boot_directory:
                options += " --boot-directory=%s" % boot_directory
        g.sh("grub-install %s %s" % (options, self.device))
        if g.is_dir("/usr/lib/grub/x86_64-efi"):
            g.mkdir_p("%s/EFI/boot" % esp_mount)
            g.mv("%s/EFI/debian/grubx64.efi" % esp_mount,
                 "%s/EFI/boot/bootx64.efi" % esp_mount)
        if boot_directory:
            self._cfg_path = "%s/grub/grub.cfg" % boot_directory
            g.write(self._cfg_path, GRUB_CFG_HEADER.encode())

    # A bare call keeps update-grub's own auto-discovery. A real
    # 'root_partuuid' means a 'multiconfig:' group instead: grub finds it
    # by filesystem label, since this grub's 'search' has no
    # '--part-uuid' option. The kernel command line still gets
    # 'root=PARTUUID=...', resolved by the kernel itself.
    def add_entry(self, g, group_label=None, kernel=None, initrd=None,
                  root_partuuid=None, cmdline="", **opts):
        if root_partuuid is None:
            g.sh("update-grub")
            return
        entry = (
            "menuentry '%s' {\n"
            "    search --no-floppy --set=root --label %s\n"
            "    linux %s root=PARTUUID=%s ro%s\n"
            "    initrd %s\n"
            "}\n\n"
        ) % (group_label, opts["root_label"], kernel, root_partuuid,
             " %s" % cmdline if cmdline else "", initrd)
        g.write_append(self._cfg_path, entry.encode())


# A Unified Kernel Image needs no boot entry written anywhere: the Boot
# Loader Specification finds '/boot/EFI/Linux/*.efi' by itself, on the
# XBOOTLDR partition ('/boot') if there is one and the root filesystem
# otherwise -- so add_entry() has nothing to do, ever.
class SystemdBootBootloader(Bootloader):
    def detect(self, g):
        return g.is_file("/usr/bin/bootctl")

    def install(self, g, esp_mount, **opts):
        g.sh("bootctl install --esp-path=%s --boot-path=/boot" % esp_mount)

    def add_entry(self, g, **opts):
        pass


REGISTRY = [GrubBootloader, SystemdBootBootloader]


def detect(g, device):
    for cls in REGISTRY:
        bootloader = cls(device)
        if bootloader.detect(g):
            return bootloader
    return None
