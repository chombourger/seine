# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import grp
import os
import subprocess
import sys
import tarfile
import tempfile

from seine.bootstrap import Bootstrap
from seine.qemu      import Qemu
from seine.utils     import ContainerEngine

class Imager(Bootstrap):
    TARGET_DIR = "/tmp/image"
    PACKAGES = [
        "attr",
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

    def get_file(self, name, output_dir):
        output = os.path.join(output_dir, name)
        with open(output, "w") as output_file:
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
                return output
            except:
                os.unlink(output)
                raise

    def get_kernel(self, output_dir):
        return self.get_file("vmlinuz", output_dir)

    def get_initrd(self, output_dir):
        return self.get_file("initrd.img", output_dir)

    def get_imager(self, output_dir):
        return self.get_file("rootfs", output_dir)

    def build_script(self, script, targetdir):
        script_file = tempfile.NamedTemporaryFile(mode="w", delete=False, dir=os.getcwd())
        script_file.write("#!/bin/bash\n")
        script_file.write("set -e\n")
        if self.debug:
            script_file.write("set -x\n")
        script_file.write(script)
        script_file.write("\ncd %s\n" % targetdir)
        script_file.write("echo '# Extracting rootfs'\n")
        script_file.write("tar -xf /mnt${tarball}\n")
        script_file.write("update_fstab >etc/fstab\n")
        script_file.write(IMAGER_POST_INSTALL_SCRIPT)
        script_file.write(IMAGER_SELINUX_SETUP_SCRIPT)
        script_file.write(IMAGER_GRUB_INSTALL_SCRIPT)
        script_file.write("copy_bootlets\n")
        script_file.write("df -h|grep -e '^Filesystem' -e {0}|sed -e 's,{0},/,g'|sed -e 's,^,# ,g' -e 's,//,/,g'\n".format(targetdir))
        script_file.close()
        return script_file.name

    def _process_xattrs(self, output_dir):
        output = os.path.join(output_dir, "rootfs.xattr")
        with tarfile.open(self.source._tarball) as tar:
            files = []
            for f in tar.getmembers():
                if f.issym() or f.isdir():
                    continue
                files.append(f.name)
            content = tar.extractfile('rootfs.xattr').readlines()
            f = open(output, "w")
            lines = []
            present = False
            for line in content:
                line = line.decode().strip()
                if line.startswith("# file: "):
                    if lines:
                        f.write("\n".join(lines))
                        f.write("\n")
                        lines = []
                    target = line[8:]
                    present = (target in files)
                if present is True:
                    lines.append(line)
            f.close()
            return output

    def create(self, script, targetdir):
        output_dir = None
        imager_kernel = None
        imager_initrd = None
        imager_rootfs = None
        script_file = None
        xattrs = None
        try:
            output_dir = tempfile.mkdtemp(dir=os.getcwd())

            print("Processing extended attributes...")
            xattrs = self._process_xattrs(output_dir)

            print("Creating imager script...")
            script_file = self.build_script(script, targetdir)

            print("Preparing imager...")
            if ContainerEngine.hasImage(self.image_id()) is False:
                self.build_imager()
            imager_kernel = self.get_kernel(output_dir)
            imager_initrd = self.get_initrd(output_dir)
            imager_rootfs = self.get_imager(output_dir)

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
                for f in [script_file, imager_kernel, imager_initrd, imager_rootfs, xattrs]:
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
            kernel_cmd += ' tarball={} script={} xattrs={}'.format(self.source._tarball, script_file, xattrs)

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
                print_log = self.verbose
                if log.startswith('# '):
                    log = log[2:]
                    print_log = True
                if print_log is True:
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
            if self.keep is False:
                for f in [imager_kernel, imager_initrd, imager_rootfs, script_file, xattrs]:
                    if f is not None:
                        os.unlink(f)
                if output_dir:
                    os.rmdir(output_dir)

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
    if [[ ${x} =~ ^script=.* ]] || [[ ${x} =~ ^tarball=.* ]] || [[ ${x} =~ ^xattrs=.* ]]; then
        eval ${x}
    fi
done
if [ -n "${script}" ] && [ -e /mnt${script} ]; then
    export script tarball xattrs
    bash /mnt${script} 2>&1 | tee /dev/ttyS0
    result=${?}
fi
echo "IMAGER EXIT = ${result}" > /dev/ttyS0
/sbin/reboot
"""

IMAGER_POST_INSTALL_SCRIPT = """
if test -e /mnt${xattrs}; then
    echo '# Restoring extended attributes'
    setfattr --restore=/mnt${xattrs}
    rm -f rootfs.xattr
fi
mount -o bind /dev  dev
mount -o bind /proc proc
mount -o bind /run  run
mount -o bind /sys  sys
"""

IMAGER_GRUB_INSTALL_SCRIPT = """
if [ -e usr/sbin/grub-install ]; then
    options=""
    echo "# Installing grub"
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
    echo "# Setting file contexts for SELinux"
    if [ -f etc/default/grub ]; then
        sed -e 's/\(^GRUB_CMDLINE_LINUX=.*\)"$/\\1 security=selinux"/' \
            -i etc/default/grub
    fi
    setfiles -m -r ${PWD} ${PWD}${SE_FILE_CONTEXTS} ${PWD}
fi
"""
