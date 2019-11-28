
import math
import os
import re
import sys

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

        if "priority" not in part:
            part["priority"] = 500

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
        if "label" not in part:
            raise ValueError("one of the partitions does not have a 'label' defined!")
        label = part["label"]

        part = self._parse_common(part)
        part["_lvm"] = False

        if "flags" in part:
            self._parse_part_flags(part)

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
        if "label" not in vol:
            raise ValueError("one of the volumes does not have a 'label' defined!")
        label = vol["label"]

        vol = self._parse_common(vol)
        vol["_lvm"] = True

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
        if spec["image"] is None:
            raise ValueError("empty 'image' definition!")
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
        image["partitions"] = sorted(self.partitions, key=lambda p: p["priority"])

        if "volumes" in image:
            volumes = image["volumes"]
            for vol in volumes:
                vol = self._parse_vol(vol)
                self.mounts.append(vol)
                self.volumes.append(vol)
            image["volumes"] = self.volumes

        self.mounts = sorted(self.mounts, key=lambda vol: vol["_depth"], reverse=True)
        return spec

    def _script_setup_ext(self, script, part, dev):
        options = ""
        if "label" in part:
            options = options + " -L %s" % part["label"]
        script = script + "mkfs.%s %s %s\n" % (part["type"], options.strip(), dev)
        return script

    def _script_setup_vfat(self, script, part, dev):
        options = ""
        if "label" in part:
            options = options + " -n %s" % part["label"]
        script = script + "mkfs.vfat %s %s\n" % (options.strip(), dev)
        return script

    def _script_setup_fs(self, script, part, dev):
        if part["type"].startswith("ext"):
            return self._script_setup_ext(script, part, dev)
        elif part["type"] == "vfat":
            return self._script_setup_vfat(script, part, dev)
        else:
            raise NotImplementedError("'%s' is not a supported file-system!" % part["type"])

    def script(self, device, targetdir):
        fstab = ""
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
            elif part["type"] == "vfat":
                mkpart_type = "fat32"
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

        for group in self.groups:
            script = script + "pvs=${groups[%s]}\n" % group
            script = script + "[ -n \"${pvs}\" ] || exit 1\n"
            script = script + "vgcreate %s ${pvs}\n" % group

        for vol in self.volumes:
            script = script + "lvcreate -n %s -L %dM %s\n" % (vol["label"], self._to_rounded_mib(vol["size"]), vol["group"])
            device = "/dev/mapper/%s-%s" % (vol["group"], vol["label"])
            script = self._script_setup_fs(script, vol, device)
            script = script + "id=%s\n" % vol["_prefix"].replace("/", "_")
            script = script + "mounts[${id}]=%s\n" % (device)

        for mount in reversed(self.mounts):
            script = script + "dev=${mounts[%s]}\n" % mount["_prefix"].replace("/", "_")
            script = script + "mkdir -p ${targetdir}%s\n" % mount["_prefix"]
            script = script + "mount ${dev} ${targetdir}%s\n" % (mount["_prefix"])
            fstab = fstab + "    dev=${mounts[%s]}\n" % mount["_prefix"].replace("/", "_")
            if mount["_lvm"] == False:
                fstab = fstab + "    uuid=$(blkid -p -o export ${dev}|grep ^UUID)\n"
                what = "${uuid}"
            else:
                what = "${dev}"
            if mount["_prefix"] == "/":
                options = "errors=remount-ro"
                passno = 1
            else:
                options = "defaults"
                passno = 2
                if mount["type"] == "vfat":
                    options = "umask=0077"
            fstab = fstab + "    echo \"%s %s %s %s 0 %d\"\n" % (what, mount["_prefix"], mount["type"], options, passno)

        script = script + "update_fstab() {\n"
        script = script + fstab
        script = script + "}\n"

        return script

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
