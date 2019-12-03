# seine - Singular Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import subprocess
import sys
import tempfile

from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine

class Imager(Bootstrap):
    TARGET_DIR = "/tmp/image"

    def __init__(self, source):
        self.source = source
        self.imageName = "imager.ext4"
        self.debug = source.options["debug"]
        self.keep = source.options["keep"]
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
            RUN                                                                             \
                apt-get update -qqy &&                                                      \
                export container=lxc;                                                       \
                debootstrap --include=dosfstools,lvm2,parted,policycoreutils {1} rootfs {2} \
                && cp /host-tmp/{3} rootfs/etc/systemd/system/imager.service                \
                && install -m 755 /host-tmp/{4} rootfs/usr/sbin/imager                      \
                && chroot rootfs systemctl disable systemd-timesyncd                        \
                && chroot rootfs systemctl disable systemd-update-utmp                      \
                && chroot rootfs systemctl enable imager                                    \
                && rm -rf rootfs/usr/share/doc                                              \
                          rootfs/usr/share/info                                             \
                          rootfs/usr/share/man                                              \
                && echo '/dev/ubda / ext4 errors=remount-ro 0 1' >rootfs/etc/fstab          \
                && du -sh rootfs                                                            \
                && mkfs.ext4 -d rootfs {5} 384m                                             \
                && rm -rf rootfs
            FROM scratch AS image
            COPY --from=bootstrap {5} {5}
            CMD /bin/true
        """
        .format(
            hostBootstrap.name,
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
                "build", "--rm",
                "-t", self.image_id(),
                "-v", "/tmp:/host-tmp:ro",
                "-f", dockerfile.name], check=True)
            imageCreated = True
            ContainerEngine.run([
                "container", "create",
                "--name", self.container_id(), self.image_id()], check=True)
        except subprocess.CalledProcessError:
            if imageCreated == True:
                ContainerEngine.run(["image", "rm", self.image_id()], check=False)
            raise
        finally:
            self._unlink(dockerfile.name, "dockerfile for the imager")
            self._unlink(scriptfile.name, "imager script")
            self._unlink(unitfile.name, "systemd unit file for the imager")
        return self

    def get_imager(self):
        output_file = tempfile.NamedTemporaryFile(mode="wb", delete=False)
        try:
            podman_proc = ContainerEngine.Popen(
                [ "container", "export", self.container_id() ],
                stdout=subprocess.PIPE)
            tar_proc = subprocess.Popen(
                [ "tar", "-Oxf", "-" ],
                stdin=podman_proc.stdout,
                stdout=output_file)
            out, err = tar_proc.communicate(input=300)
            podman_proc.wait()
            return output_file.name
        except:
            self._unlink(output_file.name, "imager's rootfs")
            raise

    def build_script(self, script, targetdir):
        script_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
        script_file.write("#!/bin/bash\n")
        script_file.write("set -e\n")
        if self.debug:
            script_file.write("set -x\n")
        script_file.write(script)
        script_file.write("\ncd %s\n" % targetdir)
        script_file.write("tar -xf /mnt${tarball}\n")
        script_file.write("update_fstab >etc/fstab\n")
        script_file.write("df -h|grep %s\n" % targetdir)
        script_file.write(IMAGER_POST_INSTALL_SCRIPT)
        script_file.write(IMAGER_SELINUX_SETUP_SCRIPT)
        script_file.write(IMAGER_GRUB_INSTALL_SCRIPT)
        script_file.close()
        return script_file.name

    def create(self, script, targetdir):
        imager_rootfs = None
        log_file = None
        script_file = None
        try:
            print("Creating imager script...")
            script_file = self.build_script(script, targetdir)

            print("Preparing imager...")
            if ContainerEngine.hasImage(self.image_id()) == False:
                self.build_imager()
            imager_rootfs = self.get_imager()

            print("Starting imager...")
            log_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
            subprocess.run([
                "linux",
                "ubd0=%s" % imager_rootfs,
                "ubd1=%s" % self.source._image,
                "con=pty", "quiet", "root=/dev/ubda",
                "mem=512M", "selinux=0",
                "log=%s" % log_file.name,
                "tarball=%s" % self.source._tarball,
                "script=%s" % script_file], check=True)

            # Extract exit code from log file
            log_file.close()
            with open(log_file.name, "r") as f:
                for log in f.readlines():
                    if self.verbose:
                        print(log.strip())
                    if log.startswith("IMAGER EXIT ="):
                        result = int(log.split("=")[1].strip())
                        if result != 0:
                            if self.verbose == False:
                                f.seek(0)
                                lines = f.readlines()[-20:]
                                for line in lines:
                                    sys.stderr.write(line)
                            raise subprocess.CalledProcessError(result, [
                                script_file,
                                "log=%s" % log_file.name,
                                "tarball=%s" % self.source._tarball])
            print("Done.")
        except subprocess.CalledProcessError:
            raise
        finally:
            if imager_rootfs:
                self._unlink(imager_rootfs, "imager's root file-system")
            if log_file:
                self._unlink(log_file.name, "imager's log")
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
mount none /mnt -t hostfs
mount none /tmp -t tmpfs
for x in $(cat /proc/cmdline); do
    if [[ ${x} =~ ^log=.* ]] || [[ ${x} =~ ^script=.* ]] || [[ ${x} =~ ^tarball=.* ]]; then
        eval ${x}
    fi
done
if [ -n "${log}" ]; then
    exec 1>/mnt${log}
    exec 2>&1
fi
result=1
if [ -n "${script}" ] && [ -e /mnt${script} ]; then
    export log script tarball
    bash /mnt${script}
    result=${?}
fi
echo "IMAGER EXIT = ${result}"
halt
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
        options="--target x86_64-efi"
    fi
    chroot . /usr/sbin/grub-install ${options} /dev/ubdb
    if [ -d usr/lib/grub/x86_64-efi ]; then
        mkdir boot/efi/EFI/boot
        mv boot/efi/EFI/debian/grubx64.efi boot/efi/EFI/boot/bootx64.efi
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
