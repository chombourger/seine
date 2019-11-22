#!/usr/bin/python3

from abc import ABC, abstractmethod

import getopt
import glob
import math
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import yaml

class ContainerEngine:
    def hasImage(name):
        result = subprocess.run(["podman", "image", "exists", name], check=False)
        return result.returncode == 0

class Bootstrap(ABC):
    def __init__(self, distro, options):
        self._name = None
        self.distro = distro
        self.options = options
        super().__init__()

    @abstractmethod
    def create(self):
        pass

    @abstractmethod
    def defaultName(self):
        pass

    def getName(self):
        if self._name is None:
            self._name = self.defaultName()
        return self._name

    def setName(self, name):
        self._name = name

    name = property(getName, setName)

class HostBootstrap(Bootstrap):
    def create(self):
        dockerfile = tempfile.NamedTemporaryFile(delete=False)
        dockerfile.write(str.encode("""
            FROM {0}:{1} AS base
            RUN                                                       \
                 apt-get update -qqy &&                               \
                 apt-get install -qqy debootstrap qemu-user-static && \
                 apt-get clean -qqy
            FROM base AS clean-base
            RUN rm -rf /usr/share/doc /usr/share/info /usr/share/man
        """
        .format(self.distro["source"], self.distro["release"])))
        dockerfile.close()

        try:
            subprocess.run([
                "podman", "build", "--rm", "--squash",
                "-t", self.name, "-f", dockerfile.name],
                check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            os.unlink(dockerfile.name)
        return self

    def defaultName(self):
        return os.path.join("seine", "bootstrap", self.distro["source"], self.distro["release"], "all")

class TargetBootstrap(Bootstrap):
    def create(self, hostBootstrap):
        self.hostBootstrap = hostBootstrap
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {0} AS bootstrap
            RUN                                           \
                export container=lxc;                     \
                debootstrap --arch {1} {2} rootfs {3} &&  \
                cp /usr/bin/qemu-*-static rootfs/usr/bin/
            FROM scratch AS base
            COPY --from=bootstrap rootfs/ /
            FROM base AS ansible
            RUN apt-get update -qqy &&          \
                apt-get install -qqy ansible && \
                apt-get clean -qqy
            FROM base AS clean-base
            RUN rm -rf rootfs /usr/share/doc /usr/share/info /usr/share/man
            FROM ansible AS final
        """
        .format(
            self.hostBootstrap.name,
            self.distro["architecture"],
            self.distro["release"],
            self.distro["uri"]
        ))
        dockerfile.close()

        try:
            subprocess.run([
                "podman", "build", "--rm",
                "-t", self.name,
                "-f", dockerfile.name], check=True)
        except subprocess.CalledProcessError:
            raise
        finally:
            os.unlink(dockerfile.name)
        return self

    def defaultName(self):
        return os.path.join("seine", "bootstrap", self.distro["source"], self.distro["release"], self.distro["architecture"])

class Imager(Bootstrap):
    def __init__(self, source):
        self.source = source
        self.imageName = "imager.squashfs"
        super().__init__(source.spec["distribution"], source.options)

    def container_id(self):
        return self.image_id().replace("/", "-")

    def image_id(self):
        return self.name

    def defaultName(self):
        return os.path.join("seine", "imager", self.distro["source"], self.distro["release"], "all")

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
            RUN                                                              \
                apt-get update -qqy &&                                       \
                apt-get install -qqy squashfs-tools &&                       \
                export container=lxc;                                        \
                debootstrap --include=lvm2,parted                            \
                    {1} rootfs {2} &&                                        \
                cp /host-tmp/{3} rootfs/etc/systemd/system/imager.service && \
                install -m 755 /host-tmp/{4} rootfs/usr/sbin/imager &&       \
                chroot rootfs systemctl disable systemd-timesyncd &&         \
                chroot rootfs systemctl disable systemd-update-utmp &&       \
                chroot rootfs systemctl enable imager &&                     \
                rm -rf rootfs/usr/share/doc                                  \
                       rootfs/usr/share/info                                 \
                       rootfs/usr/share/man                                  \
                && mksquashfs rootfs {5} &&                                  \
                rm -rf rootfs
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
            subprocess.run([
                "podman", "build", "--rm",
                "-t", self.image_id(),
                "-v", "/tmp:/host-tmp:ro",
                "-f", dockerfile.name], check=True)
            imageCreated = True
            subprocess.run([
                "podman", "container", "create",
                "--name", self.container_id(), self.image_id()], check=True)
        except subprocess.CalledProcessError:
            if imageCreated == True:
                subprocess.run(["podman", "image", "rm", self.image_id()], check=False)
            raise
        finally:
            os.unlink(dockerfile.name)
            os.unlink(scriptfile.name)
            os.unlink(unitfile.name)
        return self

    def get_imager(self):
        output_file = tempfile.NamedTemporaryFile(mode="wb", delete=False)
        try:
            podman_proc = subprocess.Popen(
                [ "podman", "container", "export", self.container_id() ],
                stdout=subprocess.PIPE)
            tar_proc = subprocess.Popen(
                [ "tar", "-Oxf", "-" ],
                stdin=podman_proc.stdout,
                stdout=output_file)
            out, err = tar_proc.communicate(input=300)
            podman_proc.wait()
            return output_file.name
        except:
            os.unlink(output_file.name)
            raise

    def build_script(self, script, targetdir):
        script_file = tempfile.NamedTemporaryFile(mode="w", delete=False)
        script_file.write("#!/bin/bash\n")
        script_file.write("set -e\n")
        script_file.write("set -x\n")
        script_file.write(script)
        script_file.write("\ntar -C %s -xf /mnt${tarball}\n" % targetdir)
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
                "mem=512M", "selinux=disable",
                "log=%s" % log_file.name,
                "tarball=%s" % self.source._tarball,
                "script=%s" % script_file], check=True)

            # Extract exit code from log file
            log_file.close()
            with open(log_file.name, "r") as f:
                for log in f.readlines():
                    if log.startswith("IMAGER EXIT ="):
                        result = int(log.split("=")[1].strip())
                        if result != 0:
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
                os.unlink(imager_rootfs)
            if log_file:
                os.unlink(log_file.name)
            if script_file:
                os.unlink(script_file)

class Image:
    TARGET_DIR = "/dev/image"

    def __init__(self, partitionHandler, options={}):
        self.partitionHandler = partitionHandler
        self.options = options
        self.hostBootstrap = None
        self.iid = None
        self.targetBootstrap = None
        self._image = None
        self._output = None
        self._tarball = None

    def __del__(self):
        if self._tarball:
            os.unlink(self._tarball)

    def parse(self, spec):
        self.spec = spec
        if "distribution" not in self.spec:
            raise ValueError("'distribution' not found in provided specification!")
        if "image" not in self.spec:
            raise ValueError("'image' not found in provided specification!")
        if "playbook" not in self.spec:
            raise ValueError("'playbook' not found in provided specification!")

        distro = self.spec["distribution"]
        if "source" not in distro:
            distro["source"] = "debian"
        if "release" not in distro:
            distro["release"] = "buster"
        if "architecture" not in distro:
            distro["architecture"] = "amd64"
        if "uri" not in distro:
            distro["uri"] = "http://ftp.debian.org/debian"

        image = self.spec["image"]
        if "filename" not in image:
            raise ValueError("output 'filename' not specified in 'image' section!")
        self._output = image["filename"]
        return self.spec

    def rootfs(self):
        ansible = self.spec["playbook"]
        for playbook in ansible:
            playbook["hosts"] = "localhost"
        ansiblefile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(ansible, ansiblefile)
        ansiblefile.close()

        iidfile = tempfile.NamedTemporaryFile(mode="r", delete=False)

        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {0} AS playbooks
            RUN ansible-playbook /host-tmp/{1}
            FROM playbooks as clean
            RUN apt-get autoremove -qy ansible && \
                apt-get clean -y &&               \
                rm -f /usr/bin/qemu-*-static
            CMD /bin/true
        """
        .format(self.targetBootstrap.name, os.path.basename(ansiblefile.name)))
        dockerfile.close()

        try:
            self.iid = None
            subprocess.run([
                "podman", "build", "--rm",
                "--iidfile", iidfile.name,
                "-v", "/tmp:/host-tmp:ro",
                "-f", dockerfile.name],
                check=True)
            iidfile.seek(0)
            self.iid = iidfile.readline()
        except subprocess.CalledProcessError:
            raise
        finally:
            os.unlink(ansiblefile.name)
            os.unlink(dockerfile.name)
            os.unlink(iidfile.name)

    def build_tarball(self):
        try:
            self._tarball = None
            image = tempfile.NamedTemporaryFile(mode="w", delete=False, dir=os.getcwd())
            self.cid = subprocess.check_output(["podman", "container", "create", self.iid]).strip()
            subprocess.run(["podman", "container", "export", "-o", image.name, self.cid], check=True)
            self._tarball = image.name
        except subprocess.CalledProcessError:
            os.unlink(image.name)
            raise
        finally:
            if self.cid:
                subprocess.run(["podman", "container", "rm", self.cid], check=False)
                self.cid = None
            if self.iid:
                subprocess.run(["podman", "image", "rm", self.iid], check=False)
                self.iid = None

    def _size_partitions(self):
        tar = tarfile.open(self._tarball, "r")
        files = tar.getmembers()
        for f in files:
            self.partitionHandler.distribute(f)
        tar.close()
        self.partitionHandler.compute_sizes()
        self.partitionHandler.print_stats()

    def _empty_disk(self):
        size = self.partitionHandler.disk_size()
        image = tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=os.getcwd())
        image.truncate(size)
        image.close()
        self._image = image.name

    def build(self):
        try:
            # Create required bootstrap images
            distro = self.spec["distribution"]
            self.hostBootstrap = HostBootstrap(distro, self.options)
            self.targetBootstrap = TargetBootstrap(distro, self.options)
            if ContainerEngine.hasImage(self.hostBootstrap.name) == False:
                self.hostBootstrap.create()
            if ContainerEngine.hasImage(self.targetBootstrap.name) == False:
                self.targetBootstrap.create(self.hostBootstrap)

            # Assemble the root file-system
            self.rootfs()
            self.build_tarball()

            # Prepare target partitions and disk image
            imager = Imager(self)
            self._size_partitions()
            script = self.partitionHandler.script("/dev/ubdb", Image.TARGET_DIR)
            self._empty_disk()

            # Produce the target image
            imager.create(script, Image.TARGET_DIR)

            # Rename the image
            os.rename(self._image, self._output)

        except:
            if self._image is not None:
                os.unlink(self._image)
            raise

class PartitionHandler:

    START_OFFSET  = 1 * 1024 * 1024
    DEFAULT_EXTRA = 16 * 1024 * 1204
    DEFAULT_TABLE = "gpt"

    def __init__(self):
        self._min_size = None
        self._table = None
        self.groups = []
        self.mounts = []
        self.partitions = []
        self.volumes = []
        self.size = None

    def _from_human_size(self, size_string):
        try:
            size_string = size_string.lower().replace(',', '')
            size = re.search('^(\d+)[a-z]i?b$', size_string).groups()[0]
            suffix = re.search('^\d+([kmgtp])i?b$', size_string).groups()[0]
        except AttributeError:
            raise ValueError("%s is not a valid size!" % size_string)
        shft = suffix.translate(str.maketrans('kmgtp', '12345')) + '0'
        return int(size) << int(shft)

    def _to_human_size(self, size):
        if (size == 0):
            return "0B"
        size_name = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB")
        i = int(math.floor(math.log(size, 1024)))
        p = math.pow(1024, i)
        s = round(size / p, 2)
        return '%s%s' % (s, size_name[i])

    def _to_rounded_mib(self, size):
        return math.ceil(size / 1024 / 1024)

    def _parse_part_flags(self, part):
        valid_flags = [ "boot", "lvm", "primary", "extended", "logical" ]
        incompatible_flags = [
            [ "primary", "extended", "logical" ]
        ]

        for f in part["flags"]:
            if f not in valid_flags:
                raise ValueError("'%s' is not a valid partition flag!" % f)
            if f == "lvm":
                part["_lvm"] = True

        for set in incompatible_flags:
            matched = []
            for f in part["flags"]:
                if f in set:
                    matched.append(f)
            if len(matched) > 1:
                 raise ValueError("the following partition flags may not be used together: %s!" % (" ".join(matched)))

    def _parse_common(self, part):
        part["_blksz"] = 4096
        part["_depth"] = 0

        if "extra" in part:
            part["_size"] = self._from_human_size(part["extra"])
        else:
            part["_size"] = PartitionHandler.DEFAULT_EXTRA

        if "where" in part:
            prefix = os.path.normpath(part["where"])
            if prefix.endswith("/") == False:
                prefix = prefix + "/"
            depth = prefix.count("/") - 1
            part["_depth"] = depth
            part["_prefix"] = prefix
        if "size" in part:
            part["size"] = self._from_human_size(part["size"])
        if "type" not in part:
            part["type"] = "ext4"

        return part

    def _parse_part(self, part):
        part = self._parse_common(part)

        part["_lvm"] = False

        if "flags" in part:
            self._parse_part_flags(part)

        if "label" not in part:
            raise ValueError("one of the partitions does not have a 'label' defined!")
        label = part["label"]

        if "where" not in part and part["_lvm"] == False:
            raise ValueError("'where' not defined in partition '%s'!" % label)
        if part["_lvm"] == True:
            if "group" not in part:
                raise ValueError("target 'group' not defined for partition '%s'!" % label)
            elif part["group"] not in self.groups:
                self.groups.append(part["group"])
            if "size" not in part:
                raise ValueError("'size' of LVM partition '%s' was not defined!" % label)
            else:
                part["_size"] = part["size"]
        return part

    def _parse_vol(self, vol):
        vol = self._parse_common(vol)
        if "label" not in vol:
            raise ValueError("one of the volumes does not have a 'label' defined!")
        label = vol["label"]
        if "group" not in vol:
            raise ValueError("no 'group' defined for volume '%s'!" % label)
        if "where" not in vol:
            raise ValueError("'where' not defined in volume '%s'!" % label)
        return vol

    def _size_file(self, f, part):
        blksz = part["_blksz"]
        size = math.floor((f.size + blksz - 1) / blksz) * blksz
        return size if size > 0 else blksz

    def disk_size(self):
        if self._min_size is None:
            raise RuntimeError("partitions sizes shall be computed first!")

        if self.size is None or self._min_size > self.size:
            return self._min_size
        else:
            return self.size

    def distribute(self, f):
        if f.name.startswith("/") == False:
            name = "/" + f.name
        else:
            name = f.name
        for mount in self.mounts:
            if mount["_prefix"] is not None and name.startswith(mount["_prefix"]):
                mount["_size"] = mount["_size"] + self._size_file(f, mount)
                return mount
        return None

    def compute_sizes(self):
        self._min_size = PartitionHandler.START_OFFSET + 1 * 1024 * 1024
        for mount in self.mounts:
            mount["_size"] = self._to_rounded_mib(mount["_size"]) * 1024 * 1024
            if "size" in mount and mount["size"] > mount["_size"]:
                mount["_size"] = mount["size"]
            self._min_size = self._min_size + mount["_size"]

    def print_stats(self):
        print("mounts:")
        print("-------")
        size = 0
        for mount in self.mounts:
            print("%s\t%s" % (mount["where"], self._to_human_size(mount["_size"])))
            size = size + mount["_size"]
        print("total\t%s\n" % self._to_human_size(size))
        print("disk\t%s" % self._to_human_size(self.disk_size()))

    def parse(self, spec):
        if "image" not in spec:
            raise ValueError("'image' not found in provided specification!")
        image = spec["image"]
        if "partitions" not in image:
            raise ValueError("no 'partitions' defined in the 'image' section of the specification!")
        if "size" in image:
            self.size = self._from_human_size(image["size"])
        if "table" in image:
            self._table = image["table"]
            if self._table not in [ "msdos", "gpt" ]:
                raise ValueError("'%s' is not a supported partition table!" % self._table)
        else:
            self._table = PartitionHandler.DEFAULT_TABLE

        partitions = image["partitions"]
        for part in partitions:
            part = self._parse_part(part)
            self.partitions.append(part)
            if "where" in part:
                self.mounts.append(part)
        image["partitions"] = self.partitions

        if "volumes" in image:
            volumes = image["volumes"]
            for vol in volumes:
                vol = self._parse_vol(vol)
                self.mounts.append(vol)
                self.volumes.append(vol)
            image["volumes"] = self.volumes

        self.mounts = sorted(self.mounts, key=lambda vol: vol["_depth"], reverse=True)
        return spec

    def _script_setup_fs(self, script, part, dev):
        options = ""
        if "label" in part:
            options = options + " -L %s" % part["label"]
        script = script + "mkfs.ext4 %s %s\n" % (options.strip(), dev)
        return script

    def script(self, device, targetdir):
        script = PARTITION_HANDLER_SCRIPT
        script = script + "targetdir=%s\n" % targetdir
        script = script + "parted %s --script mklabel %s\n" % (device, self._table)
        start = PartitionHandler.START_OFFSET / 1024 / 1024
        ndx = 1
        for part in self.partitions:
            if self._table == "msdos":
                mkpart_arg = "primary"
                if "flags" in part:
                    if "extended" in part["flags"]:
                        mkpart_arg = "extended"
                    if "logical" in part["flags"]:
                        mkpart_arg = "logical"
            elif self._table == "gpt":
                mkpart_arg = part["label"]

            if "flags" in part and "lvm" in part["flags"]:
                mkpart_type = "ext4"
            else:
                mkpart_type = part["type"]

            end = start + self._to_rounded_mib(part["_size"])
            script = script + "parted %s --script mkpart %s %s %sMiB %sMiB\n" % (device, mkpart_arg, mkpart_type, start, end)
            start = end

            if "flags" in part:
                for f in part["flags"]:
                    if f in [ "boot", "lvm" ]:
                        script = script + "parted %s --script set %d %s on\n" % (device, ndx, f)

            script = script + "dev=$(part_device %s)\n" % device
            script = script + "[ x${dev} != x ] || exit 1\n"

            if part["_lvm"] == False:
                script = script + "id=%s\n" % part["_prefix"].replace("/", "_")
                script = script + "mounts[${id}]=${dev}\n"
                script = self._script_setup_fs(script, part, "${dev}")
            else:
                script = script + "pvcreate ${dev}\n"
                script = script + "pvs=${groups[%s]}\n" % part["group"]
                script = script + "groups[%s]=\"${pvs} ${dev}\"\n" % part["group"]
            ndx = ndx + 1

        if len(self.groups) > 0:
            script = script + "cp -r /etc/lvm /dev/ && mount -o bind /dev/lvm /etc/lvm\n"

        for group in self.groups:
            script = script + "pvs=${groups[%s]}\n" % group
            script = script + "[ -n \"${pvs}\" ] || exit 1\n"
            script = script + "vgcreate %s ${pvs}\n" % group

        for vol in self.volumes:
            script = script + "vgs\n"
            script = script + "lvcreate -n %s -L %dM %s\n" % (vol["label"], self._to_rounded_mib(vol["size"]), vol["group"])
            device = "/dev/mapper/%s-%s" % (vol["group"], vol["label"])
            script = self._script_setup_fs(script, vol, device)
            script = script + "id=%s\n" % vol["_prefix"].replace("/", "_")
            script = script + "mounts[${id}]=%s\n" % (device)

        for mount in reversed(self.mounts):
            script = script + "dev=${mounts[%s]}\n" % mount["_prefix"].replace("/", "_")
            script = script + "mkdir -p ${targetdir}%s\n" % mount["_prefix"]
            script = script + "mount ${dev} ${targetdir}%s\n" % (mount["_prefix"])

        return script

class Cmd(ABC):
    def __init__(self):
        super().__init__()

    @abstractmethod
    def main(self, argv):
        pass

class BuildCmd(Cmd):
    def __init__(self):
        self.image = None
        self.options = {}
        self.partitionHandler = PartitionHandler()
        self.spec = None

    def load(self, yaml_file):
        with open(yaml_file, "r") as f:
            spec = yaml.load(f)
        if self.spec is None:
            self.spec = spec
        else:
            self.merge(spec)
        return self.spec

    def _merge_distro(self, spec):
        if "distribution" in self.spec and "distribution" in spec:
            for setting in spec["distribution"]:
                self.spec["distribution"][setting] = spec["distribution"][setting]
        elif "distribution" not in self.spec:
            self.spec["distribution"] = spec["distribution"]

    def _append_playbooks(self, spec):
        if "playbook" in self.spec and "playbook" in spec:
            for playbook in spec["playbook"]:
                self.spec["playbook"].append(playbook)
        elif "playbook" not in self.spec:
            self.spec["playbook"] = spec["playbook"]

    def _merge_image(self, spec):
        if "image" in self.spec and "image" in spec:
            for setting in spec["image"]:
                self.spec["image"][setting] = spec["image"][setting]
        elif "image" not in self.spec:
            self.spec["image"] = spec["image"]

    def merge(self, spec):
        self._merge_distro(spec)
        self._append_playbooks(spec)
        self._merge_image(spec)
        return self.spec

    def parse(self):
        if self.image is None:
            self.image = Image(self.partitionHandler, self.options)
        self.spec = self.partitionHandler.parse(self.spec)
        self.spec = self.image.parse(self.spec)
        return self.spec

    def build(self):
        if self.spec is None or self.image is None:
            raise RuntimeError("no specification was loaded or parsed!")
        return self.image.build()

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, "h", ["help"])
        except getopt.GetoptError as err:
            print(err)
            cmd_build_usage()
            sys.exit(1)
        for o, a in opts:
            if o in ("-h", "--help"):
                cmd_build_usage()
                sys.exit()
            else:
                assert False, "unhandled option"

        if len(args) == 0:
            sys.stderr.write("error: build command expects a YAML file\n")
            sys.exit(1)

        try:
            for spec in args:
                self.load(spec)
            self.parse()
            sys.exit(self.build())
        except OSError as e:
            sys.stderr.write("error: couldn't open build YAML file: {0}\n".format(e))
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: YAML file is invalid: {0}\n".format(e))
            sys.exit(3)
        except subprocess.CalledProcessError as e:
            sys.stderr.write("error: build failed: {0}\n".format(e))
            sys.exit(4)

def main():
    argv = sys.argv[1:]

    if len(argv) == 0:
        print("%s: error: missing command argument!" % sys.argv[0])
        sys.exit(1)

    cmd = argv[0]
    if cmd == "build":
        BuildCmd().main(argv[1:])
    else:
        print("%s: unknown command '%s'!" % (sys.argv[0], cmd))
        sys.exit(1)

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
    bash -x /mnt${script}
    result=${?}
fi
echo "IMAGER EXIT = ${result}"
halt"""

PARTITION_HANDLER_SCRIPT = """
part_device() {
    mkdir -p /dev/parts
    partprobe ${1}
    for d in ${1}*[0-9]; do
        l=/dev/parts/$(basename ${d})
        if [ ! -e ${l} ]; then
            ln -s ${d} ${l}
            echo ${d}
            return
        fi
    done
}

declare -A groups
declare -A mounts

"""

if __name__ == "__main__":
    main()
