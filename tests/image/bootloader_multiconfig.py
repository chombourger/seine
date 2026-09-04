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

# As tests/image/multiconfig.py's own: a real build, so this does not run
# unless asked for. GrubBootloader's EFI support is x86_64-only, so this
# is amd64-only rather than HOST_ARCH.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# One disk, two independently-packaged operating systems -- as
# tests/image/multiconfig.py's own TwoSourcesOnOneDisk, plus a real kernel
# and grub in each, so the disk this produces has one grub.cfg on the
# ESP, with one seine-authored menuentry per group pointing at that
# group's own kernel/initrd and root PARTUUID. Static content only -- no
# QEMU/mtda in this environment to actually boot either entry, so that
# part is not covered here.
class OneBootEntryPerGroup(avocado.Test):
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

    # 'boot_owner' also gets a 'GRUB_CMDLINE_LINUX' edit, the same shape
    # as examples/pc-image/grub-serial-console.yaml -- what is under
    # test is that it reaches its own group's menuentry and no other's,
    # not the serial console itself.
    def _group(self, name, marker, boot_owner):
        path = os.path.join(self.workdir, "%s.yaml" % name)
        packages = [marker, "linux-image-amd64"]
        if boot_owner:
            packages.append("grub-efi-amd64")
        extra = ""
        if boot_owner:
            extra = (
                "    - name: %s serial console\n"
                "      tasks:\n"
                "          - name: add console=ttyS0 to the kernel command line\n"
                "            lineinfile:\n"
                "                path: /etc/default/grub\n"
                "                regexp: '^GRUB_CMDLINE_LINUX='\n"
                "                line: 'GRUB_CMDLINE_LINUX=\"console=ttyS0\"'\n"
                % name)
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: amd64\n"
                "playbook:\n"
                "    - name: %s packages\n"
                "      tasks:\n"
                "          - name: install %s packages\n"
                "            apt:\n"
                "                name: [%s]\n"
                "                state: present\n"
                "%s"
                % (name, name, ", ".join(packages), extra))
        return path

    def specification(self):
        main = self._group("main", "busybox-static", boot_owner=True)
        recovery = self._group("recovery", "dropbear-bin", boot_owner=False)
        disk = os.path.join(self.workdir, "disk.img")
        outer = os.path.join(self.workdir, "outer.yaml")
        with open(outer, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: amd64\n"
                "multiconfig:\n"
                "    main:\n"
                "        - %s\n"
                "    recovery:\n"
                "        - %s\n"
                "image:\n"
                "    filename: %s\n"
                "    table: gpt\n"
                "    partitions:\n"
                "        - label: esp\n"
                "          type: vfat\n"
                "          size: 64MiB\n"
                "          source: main\n"
                "          where: /efi\n"
                "          flags: [boot, primary]\n"
                "        - label: main-root\n"
                "          type: ext4\n"
                "          source: main\n"
                "          where: /\n"
                "          size: 900MiB\n"
                "        - label: recovery-root\n"
                "          type: ext4\n"
                "          source: recovery\n"
                "          where: /\n"
                "          size: 900MiB\n"
                % (main, recovery, disk))
        return outer, disk

    def test_one_menuentry_per_group_on_the_esp(self):
        outer, disk = self.specification()

        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            built = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v", outer],
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
            # order, both roots at default priority (PartitionHandler.parse()).
            # Lower-cased to match Imager._partuuid()'s own -- see its comment.
            main_partuuid = g.part_get_gpt_guid("/dev/sda", 2).lower()
            recovery_partuuid = g.part_get_gpt_guid("/dev/sda", 3).lower()

            g.mount_ro("/dev/sda1", "/")
            self.assertTrue(g.is_file("/grub/grub.cfg"), "no grub.cfg on the ESP")
            # Never a group's own kernel/initrd -- GRUB reads those
            # cross-partition, straight off each group's own root.
            self.assertFalse(g.exists("/vmlinuz") or g.exists("/EFI/main"),
                             "a group's own kernel/initrd leaked onto the ESP")
            cfg = g.read_file("/grub/grub.cfg").decode()
            g.umount("/")

            self.assertEqual(cfg.count("menuentry"), 2,
                             "expected exactly one menuentry per group:\n%s" % cfg)
            self.assertLess(cfg.index("menuentry 'main'"), cfg.index("menuentry 'recovery'"),
                            "'main' (the boot owner) must be the static default")
            self.assertIn("set default=0", cfg)

            for label, root_label, partuuid in (
                ("main", "main-root", main_partuuid),
                ("recovery", "recovery-root", recovery_partuuid),
            ):
                entry = re.search(r"menuentry '%s' \{(.*?)\}" % label, cfg, re.DOTALL)
                self.assertIsNotNone(entry, "no menuentry for '%s':\n%s" % (label, cfg))
                body = entry.group(1)
                self.assertIn("search --no-floppy --set=root --label %s" % root_label, body)
                self.assertIn("root=PARTUUID=%s" % partuuid, body)
                self.assertRegex(body, r"linux /boot/vmlinuz-\S+ root=PARTUUID=%s ro" % partuuid)
                self.assertRegex(body, r"initrd /boot/initrd\.img-\S+")
                # Only 'main' edited its own GRUB_CMDLINE_LINUX -- its
                # menuentry carries it, 'recovery's does not.
                if label == "main":
                    self.assertIn("console=ttyS0", body)
                else:
                    self.assertNotIn("console=ttyS0", body)

            g.mount_ro("/dev/sda2", "/")
            main_boot = g.ls("/boot")
            g.umount("/")
            g.mount_ro("/dev/sda3", "/")
            recovery_boot = g.ls("/boot")
            g.umount("/")
            self.assertEqual(len([e for e in main_boot if e.startswith("vmlinuz-")]), 1)
            self.assertEqual(len([e for e in recovery_boot if e.startswith("vmlinuz-")]), 1)
        finally:
            g.close()
        os.remove(disk)

if __name__ == "__main__":
    avocado.main()
