# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import re
import shutil
import tarfile
import tempfile

import guestfs

from seine.imager_kernel import ImagerKernel
from seine.utils         import ContainerEngine

DEVICE = "/dev/sda"

# GPT partition type GUIDs.
GPT_TYPE_ESP = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"
GPT_TYPE_LVM = "E6D6D379-F507-44C2-A23C-238F2A3DF928"

class Imager:
    def __init__(self, source):
        self.source = source
        self.keep = source.options["keep"]
        self.verbose = source.options["verbose"]

    # Filters a `getfattr -Rh -d -e hex` dump down to entries for files that
    # actually made it into the tarball (getfattr walked the ansible
    # container's live filesystem, which includes things -- like the
    # ansible/seine-ansible packages themselves -- that were later removed
    # before the tarball was exported).
    def _filter_xattr_dump(self, text, known_files):
        blocks = []
        current = []
        keep = False
        for line in text.splitlines():
            if line.startswith("# file: "):
                if keep and current:
                    blocks.append("\n".join(current))
                current = [line]
                keep = line[len("# file: "):] in known_files
            elif keep:
                current.append(line)
        if keep and current:
            blocks.append("\n".join(current))
        return "\n\n".join(blocks) + "\n"

    def _restore_xattrs(self, g):
        with tarfile.open(self.source._tarball) as tar:
            known_files = set()
            xattr_member = None
            for member in tar.getmembers():
                if member.name == "rootfs.xattr":
                    xattr_member = member
                    continue
                if member.issym() or member.isdir():
                    continue
                known_files.add(member.name)
            if xattr_member is None:
                return
            dump = tar.extractfile(xattr_member).read().decode()

        if not g.is_file("/usr/bin/setfattr"):
            print("  note: 'attr' is not installed in the target image, "
                  "skipping restore of extended attributes (e.g. file capabilities)")
            return

        # Extended attribute values (e.g. security.capability) are arbitrary
        # bytes and may contain embedded NULs, which the setxattr API calls
        # can't carry as Python str -- go through setfattr --restore instead,
        # using the target's own binary against a file we upload, since a
        # file transfer is NUL-safe where a str argument isn't.
        filtered = self._filter_xattr_dump(dump, known_files)
        g.write("/rootfs.xattr", filtered.encode())
        g.sh("setfattr --restore=/rootfs.xattr")
        g.rm("/rootfs.xattr")

    def _mkfs(self, g, part, dev):
        g.mkfs(part["type"], dev)
        if "label" in part:
            g.set_label(dev, part["label"])

    def _partition_device(self, g, table, part, index):
        start_sect = part["_start_mib"] * 2048
        end_sect = part["_end_mib"] * 2048 - 1
        flags = part.get("flags", [])
        prlogex = "primary"
        if table == "msdos":
            if "extended" in flags:
                prlogex = "extended"
            if "logical" in flags:
                prlogex = "logical"
        g.part_add(DEVICE, prlogex, start_sect, end_sect)
        if table == "gpt":
            g.part_set_name(DEVICE, index, part["label"])
            if "boot" in flags:
                g.part_set_gpt_type(DEVICE, index, GPT_TYPE_ESP)
            elif "lvm" in flags:
                g.part_set_gpt_type(DEVICE, index, GPT_TYPE_LVM)
        elif "boot" in flags:
            g.part_set_bootable(DEVICE, index, True)
        return DEVICE + str(index)

    def _prepare_kernel(self, output_dir):
        print("Preparing imager kernel...")
        imagerKernel = ImagerKernel(self.source)
        if ContainerEngine.hasImage(imagerKernel.name) is False:
            imagerKernel.create()
        vmlinuz, modules, version = imagerKernel.extract(output_dir)
        os.environ["SUPERMIN_KERNEL"] = vmlinuz
        os.environ["SUPERMIN_KERNEL_VERSION"] = version
        os.environ["SUPERMIN_MODULES"] = modules
        if self.verbose:
            print("  package: %s" % imagerKernel.package)
            print("  version: %s" % version)

    def create(self):
        ph = self.source.partitionHandler
        disk = self.source._image
        output_dir = tempfile.mkdtemp(dir=os.getcwd())
        try:
            self._prepare_kernel(output_dir)

            print("Starting imager appliance...")
            g = guestfs.GuestFS(python_return_dict=True)
            g.add_drive_opts(disk, format="raw", readonly=False)
            g.launch()

            print("Partitioning (%s)..." % ph._table)
            g.part_init(DEVICE, ph._table)
            part_devices = {}
            index = 1
            for part in ph.partitions:
                dev = self._partition_device(g, ph._table, part, index)
                part_devices[id(part)] = dev
                if part["_lvm"]:
                    g.pvcreate(dev)
                else:
                    self._mkfs(g, part, dev)
                index = index + 1

            for group in ph.groups:
                pvs = [part_devices[id(p)] for p in ph.partitions
                       if p["_lvm"] and p.get("group") == group]
                if not pvs:
                    raise RuntimeError("no physical volume found for LVM group '%s'!" % group)
                g.vgcreate(group, pvs)

            vol_devices = {}
            for vol in ph.volumes:
                g.lvcreate(vol["label"], vol["group"], ph._to_rounded_mib(vol["size"]))
                voldev = "/dev/%s/%s" % (vol["group"], vol["label"])
                self._mkfs(g, vol, voldev)
                vol_devices[id(vol)] = voldev

            print("Mounting target file-systems...")
            mount_order = sorted(ph.mounts, key=lambda m: m["_depth"])
            mount_devices = {}
            for m in mount_order:
                dev = part_devices.get(id(m)) or vol_devices.get(id(m))
                mount_devices[id(m)] = dev
                if m["_prefix"] != "/":
                    g.mkmountpoint(m["_prefix"].rstrip("/"))
                g.mount(dev, m["_prefix"])

            print("Extracting root file-system...")
            g.tar_in(self.source._tarball, "/")

            print("Restoring extended attributes...")
            self._restore_xattrs(g)

            print("Writing fstab...")
            fstab = []
            for m in mount_order:
                dev = mount_devices[id(m)]
                what = dev if m["_lvm"] else "UUID=%s" % g.vfs_uuid(dev)
                options = "defaults"
                passno = 2
                if m["_prefix"] == "/":
                    if m["type"] != "btrfs":
                        options = "errors=remount-ro"
                    passno = 1
                elif m["type"] == "vfat":
                    options = "umask=0077"
                fstab.append("%s %s %s %s 0 %d" % (what, m["_prefix"], m["type"], options, passno))
            g.write("/etc/fstab", ("\n".join(fstab) + "\n").encode())

            print("Copying bootlets...")
            for bootlet in ph.bootlets:
                data = g.read_file(bootlet["file"])
                g.pwrite_device(DEVICE, data, bootlet["_seek"] * 1024)

            se_contexts = "/etc/selinux/default/contexts/files/file_contexts"
            if g.is_file(se_contexts) and g.is_file("/usr/sbin/setfiles"):
                print("Setting file contexts for SELinux...")
                if g.is_file("/etc/default/grub"):
                    grub_cfg = g.read_file("/etc/default/grub").decode()
                    grub_cfg = re.sub(r'^(GRUB_CMDLINE_LINUX=.*)"$', r'\1 security=selinux"',
                                       grub_cfg, flags=re.MULTILINE)
                    g.write("/etc/default/grub", grub_cfg.encode())
                g.sh("setfiles -m %s /" % se_contexts)

            if g.is_file("/usr/sbin/grub-install"):
                print("Installing grub...")
                options = ""
                if g.is_dir("/usr/lib/grub/x86_64-efi"):
                    options = "--target x86_64-efi --efi-directory=/efi"
                g.sh("grub-install %s %s" % (options, DEVICE))
                if g.is_dir("/usr/lib/grub/x86_64-efi"):
                    g.mkdir_p("/efi/EFI/boot")
                    g.mv("/efi/EFI/debian/grubx64.efi", "/efi/EFI/boot/bootx64.efi")
                g.sh("update-grub")

            print("Disk usage:")
            for m in mount_order:
                st = g.statvfs(m["_prefix"])
                total = st["blocks"] * st["frsize"]
                used = total - st["bfree"] * st["frsize"]
                print("%s\t%s used / %s total" % (
                    m["_prefix"], ph._to_human_size(used), ph._to_human_size(total)))

            g.umount_all()
            g.shutdown()
            g.close()
            print("Done.")
        finally:
            if self.keep:
                print("keeping '%s' (imager kernel files) as requested" % output_dir)
            else:
                shutil.rmtree(output_dir, ignore_errors=True)
