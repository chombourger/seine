# seine - Singular Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import grp
import os
import subprocess
import sys
import tempfile

from seine.bootstrap import Bootstrap
from seine.qemu      import Qemu
from seine.utils     import ContainerEngine

class Imager(Bootstrap):
    TARGET_DIR = "/tmp/image"
    PACKAGES = [
        "btrfs-progs",
        "dosfstools",
        "linux-image-amd64",
        "live-boot",
        "lvm2",
        "nilfs-tools",
        "parted",
        "policycoreutils",
    ]

    def __init__(self, source):
        self.source = source
        self.imageName = "imager.iso"
        self.debug = source.options["debug"]
        self.keep = source.options["keep"]
        self.qemu = Qemu(source)
        self.verbose = source.options["verbose"]
        super().__init__(source.spec["distribution"], source.options)

    def _unlink(self, path, descr):
        if self.keep:
            print("keeping '%s' (%s) as requested" % (path, descr))
        else:
            os.unlink(path)

    def container_id(self):
        return self.image_id().replace("/", "-")

    def image_id(self):
        return self.name

    def defaultName(self):
        return os.path.join("imager", self.distro["source"], self.distro["release"], "all")

    def build_imager(self):
        hostBootstrap = self.source.hostBootstrap

        unitfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        unitfile.write(IMAGER_SYSTEMD_UNIT)
        unitfile.close()

        scriptfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        scriptfile.write(IMAGER_SYSTEMD_SCRIPT)
        scriptfile.close()

        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {0} AS bootstrap
            RUN                                                                    \
                apt-get update -qqy &&                                             \
                apt-get install -qqy squashfs-tools xorriso &&                     \
                export container=lxc;                                              \
                debootstrap --include={1} {2} rootfs {3}                           \
                && cp /host-tmp/{4} rootfs/etc/systemd/system/imager.service       \
                && install -m 755 /host-tmp/{5} rootfs/usr/sbin/imager             \
                && chroot rootfs systemctl disable systemd-timedated               \
                && chroot rootfs systemctl disable systemd-update-utmp             \
                && chroot rootfs systemctl enable imager                           \
                && echo root:root | chroot rootfs chpasswd                         \
                && cp rootfs/vmlinuz vmlinuz                                       \
                && cp rootfs/initrd.img initrd.img                                 \
                && rm -rf rootfs/boot                                              \
                          rootfs/lib/modules/*/kernel/drivers/android              \
                          rootfs/lib/modules/*/kernel/drivers/bluetooth            \
                          rootfs/lib/modules/*/kernel/drivers/firewire             \
                          rootfs/lib/modules/*/kernel/drivers/gpu                  \
                          rootfs/lib/modules/*/kernel/drivers/infiniband           \
                          rootfs/lib/modules/*/kernel/drivers/isdn                 \
                          rootfs/lib/modules/*/kernel/drivers/media                \
                          rootfs/lib/modules/*/kernel/drivers/parport              \
                          rootfs/lib/modules/*/kernel/drivers/pcmcia               \
                          rootfs/lib/modules/*/kernel/drivers/thunderbolt          \
                          rootfs/lib/modules/*/kernel/drivers/video                \
                          rootfs/lib/modules/*/kernel/sound                        \
                          rootfs/usr/share/doc                                     \
                          rootfs/usr/share/info                                    \
                          rootfs/usr/share/man                                     \
                && mkdir -p iso/live                                               \
                && mksquashfs rootfs iso/live/filesystem.squashfs                  \
                && rm -rf rootfs                                                   \
                && xorriso -as mkisofs -iso-level 3 -full-iso9660-filenames        \
                           -output {6} -graft-points iso                           \
                && rm -rf iso
            FROM scratch AS image
            COPY --from=bootstrap {6} rootfs
            COPY --from=bootstrap /vmlinuz vmlinuz
            COPY --from=bootstrap /initrd.img initrd.img
            CMD /bin/true
        """
        .format(
            hostBootstrap.name,
            ",".join(Imager.PACKAGES),
            self.distro["release"],
            self.distro["uri"],
            os.path.basename(unitfile.name),
            os.path.basename(scriptfile.name),
            self.imageName
        ))
        dockerfile.close()

        imageCreated = False
        try:
            ContainerEngine.run([
                "build", "--rm", "--squash",
                "-t", self.image_id(),
                "-v", "/tmp:/host-tmp:ro",
                "-f", dockerfile.name], check=True)
            imageCreated = True
            ContainerEngine.run([
                "container", "create",
                "--name", self.container_id(), self.image_id()], check=True)
        except subprocess.CalledProcessError:
            if imageCreated is True:
                ContainerEngine.run(["image", "rm", self.image_id()], check=False)
            raise
        finally:
            self._unlink(dockerfile.name, "dockerfile for the imager")
            self._unlink(scriptfile.name, "imager script")
            self._unlink(unitfile.name, "systemd unit file for the imager")

    def get_file(self, name):
        output_file = tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=os.getcwd())
        try:
            podman_proc = ContainerEngine.Popen(
                [ "container", "export", self.container_id() ],
                stdout=subprocess.PIPE)
            tar_proc = subprocess.Popen(
                [ "tar", "-Oxf", "-", name ],
                stdin=podman_proc.stdout,
                stdout=output_file)
            out, err = tar_proc.communicate(input=300)
            podman_proc.wait()
            return output_file.name
        except:
            os.unlink(output_file.name)
            raise

    def get_kernel(self):
        return self.get_file("vmlinuz")

    def get_initrd(self):
        return self.get_file("initrd.img")

    def get_imager(self):
        return self.get_file("rootfs")

    def build_script(self, script, targetdir):
        script_file = tempfile.NamedTemporaryFile(mode="w", delete=False, dir=os.getcwd())
        script_file.write("#!/bin/bash\n")
        script_file.write("set -e\n")
        if self.debug:
            script_file.write("set -x\n")
        script_file.write(script)
        script_file.write("\ncd %s\n" % targetdir)
        script_file.write("tar -xf /mnt${tarball}\n")
        script_file.write("df -h|grep %s\n" % targetdir)
        script_file.write("update_fstab >etc/fstab\n")
        script_file.write(IMAGER_POST_INSTALL_SCRIPT)
        script_file.write(IMAGER_SELINUX_SETUP_SCRIPT)
        script_file.write(IMAGER_GRUB_INSTALL_SCRIPT)
        script_file.write("copy_bootlets\n")
        script_file.close()
        return script_file.name

    def create(self, script, targetdir):
        imager_kernel = None
        imager_initrd = None
        imager_rootfs = None
        script_file = None
        try:
            print("Creating imager script...")
            script_file = self.build_script(script, targetdir)

            print("Preparing imager...")
            if ContainerEngine.hasImage(self.image_id()) is False:
                self.build_imager()
            imager_kernel = self.get_kernel()
            imager_initrd = self.get_initrd()
            imager_rootfs = self.get_imager()

            user_groups = [grp.getgrgid(g).gr_name for g in os.getgroups()]
            if 'kvm' in user_groups:
                imager_proc = subprocess
                imager_args = []
                imager_vm = "kvm"
            else:
                self.qemu.create()
                imager_proc = ContainerEngine
                imager_args = ['run']
                imager_vm = "qemu-system-x86_64"
                imager_dirs = []
                for f in [script_file, imager_kernel, imager_initrd, imager_rootfs]:
                    d = os.path.dirname(f)
                    if d not in imager_dirs:
                        imager_dirs.append(d)
                for d in imager_dirs:
                    imager_args.append('-v')
                    imager_args.append('{}:{}:z'.format(d, d))
                imager_args.append(self.qemu.image_id())

            print("Starting imager using %s..." % imager_vm)

            # boot the live image with SELinux disabled
            kernel_cmd = 'boot=live console=ttyS0 selinux=0'

            # quiet the kernel and systemd
            kernel_cmd += ' quiet loglevel=0 systemd.mask=getty.target systemd.show_status=false'

            # let imager script know where to find the image tarball and imager script
            kernel_cmd += ' tarball={} script={}'.format(self.source._tarball, script_file)

            imager_cmd = [
                imager_vm,
                "-m", "512",
                "-kernel", imager_kernel,
                "-initrd", imager_initrd,
                "-append", kernel_cmd,
                "-drive", "file={},index=0,media=disk,format=raw".format(imager_rootfs),
                "-drive", "file={},index=1,media=disk,format=raw".format(self.source._image),
                "-fsdev", "local,id=hostfs_dev,path=/,security_model=none",
                "-device", "virtio-9p-pci,fsdev=hostfs_dev,mount_tag=hostfs_mount",
                "-display", "none",
                "-serial", "stdio",
                "-no-reboot"
            ]

            if self.verbose is True:
                if imager_args:
                    print(' '.join(imager_args))
                print(' '.join(imager_cmd))
            proc = imager_proc.Popen([*imager_args, *imager_cmd], stdout=subprocess.PIPE)

            # Extract exit code from logs
            result = None
            for log in proc.stdout:
                log = log.decode()
                if self.verbose is True:
                    print(log.strip())
                if log.startswith("IMAGER EXIT ="):
                    result = int(log.split("=")[1].strip())
                    if result != 0:
                        if self.verbose is False:
                            lines = stdout[-20:]
                            for line in lines:
                                sys.stderr.write(line)
                    break
            rc = proc.wait()
            if result is None:
                result = rc
            if result != 0:
                raise subprocess.CalledProcessError(result, imager_cmd)
            else:
                print("Done.")
        finally:
            if imager_kernel:
                self._unlink(imager_kernel, "imager's kernel")
            if imager_kernel:
                self._unlink(imager_initrd, "imager's initrd")
            if imager_rootfs:
                self._unlink(imager_rootfs, "imager's root file-system")
            if script_file:
                self._unlink(script_file, "imager's script")

IMAGER_SYSTEMD_UNIT = """[Unit]
Description=Seine Imager Service
After=network.target

[Service]
Type=simple
ExecStart=/usr/sbin/imager

[Install]
WantedBy=multi-user.target"""

IMAGER_SYSTEMD_SCRIPT = """#!/bin/bash
set -o pipefail
mount -t 9p -o trans=virtio hostfs_mount /mnt -oversion=9p2000.L,posixacl,cache=loose,msize=16777216
mount -t tmpfs none /tmp
result=1
for x in $(cat /proc/cmdline); do
    if [[ ${x} =~ ^script=.* ]] || [[ ${x} =~ ^tarball=.* ]]; then
        eval ${x}
    fi
done
if [ -n "${script}" ] && [ -e /mnt${script} ]; then
    export script tarball
    bash /mnt${script} 2>&1 | tee /dev/ttyS0
    result=${?}
fi
echo "IMAGER EXIT = ${result}" > /dev/ttyS0
/sbin/reboot
"""

IMAGER_POST_INSTALL_SCRIPT = """
mount -o bind /dev  dev
mount -o bind /proc proc
mount -o bind /run  run
mount -o bind /sys  sys
"""

IMAGER_GRUB_INSTALL_SCRIPT = """
if [ -e usr/sbin/grub-install ]; then
    options=""
    if [ -d usr/lib/grub/x86_64-efi ]; then
        options="--target x86_64-efi --efi-directory=/efi"
    fi
    chroot . /usr/sbin/grub-install ${options} /dev/sdb
    if [ -d usr/lib/grub/x86_64-efi ]; then
        mkdir -p efi/EFI/boot
        mv efi/EFI/debian/grubx64.efi efi/EFI/boot/bootx64.efi
    fi
    chroot . /usr/sbin/update-grub
fi
"""

IMAGER_SELINUX_SETUP_SCRIPT = """
SE_FILE_CONTEXTS=/etc/selinux/default/contexts/files/file_contexts
if [ -e .${SE_FILE_CONTEXTS} ]; then
    if [ -f etc/default/grub ]; then
        sed -e 's/\(^GRUB_CMDLINE_LINUX=.*\)"$/\\1 security=selinux"/' \
            -i etc/default/grub
    fi
    setfiles -m -r ${PWD} ${PWD}${SE_FILE_CONTEXTS} ${PWD}
fi
"""
