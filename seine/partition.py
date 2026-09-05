# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import math
import os
import re

RO_FSTYPES = {"squashfs", "erofs"}

# Never mounted, never mkfs'd -- a raw dm-verity hash tree, written directly
# by the imager once the read-only image it protects ('verity-for:') has
# been built. Only paired with a '/' or '/usr' mount: those are the only
# mountpoints the Discoverable Partitions Specification defines an
# auto-discovered Verity partition type GUID for.
VERITY_HASH_TYPE = "verity-hash"

class PartitionHandler:

    START_OFFSET_KB  = 1 * 1024
    DEFAULT_EXTRA_MB = 16
    DEFAULT_TABLE    = "gpt"

    def __init__(self):
        self._min_size = None
        self._table = None
        self.bootlets = []
        self.groups = []
        self.mounts = []
        self.partitions = []
        self.secure_boot = None
        self.volumes = []
        self.size = None

    def _align_up(self, n, align):
        return math.ceil(n / align) * align

    def _from_human_size(self, size_string):
        try:
            size_string = size_string.lower().replace(',', '')
            size = re.search(r'^(\d+)[a-z]i?b$', size_string).groups()[0]
            suffix = re.search(r'^\d+([kmgtp])i?b$', size_string).groups()[0]
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
        valid_flags = [ "boot", "lvm", "xbootldr", "primary", "extended", "logical" ]
        incompatible_flags = [
            [ "primary", "extended", "logical" ]
        ]

        for f in part["flags"]:
            if f not in valid_flags:
                raise ValueError("'%s' is not a valid partition flag!" % f)
            if f == "lvm":
                part["_lvm"] = True
            if f in ("boot", "xbootldr") and part["type"] != "vfat":
                raise ValueError(
                    "partition '%s' has flag '%s', which UEFI firmware can "
                    "only read from a 'vfat' partition (this one is '%s')"
                    % (part["label"], f, part["type"]))

        for set in incompatible_flags:
            matched = []
            for f in part["flags"]:
                if f in set:
                    matched.append(f)
            if len(matched) > 1:
                 raise ValueError("the following partition flags may not be used together: %s!" % (" ".join(matched)))

    def _parse_bootlet(self, bootlet):
        if "file" not in bootlet:
            raise ValueError("one of the bootlets does not have a 'file' defined!")

        if "align" not in bootlet:
            bootlet["_align"] = 1
        else:
            bootlet["_align"] = int(bootlet["align"])

        if "priority" not in bootlet:
            bootlet["priority"] = 500

        return bootlet

    # A disk's own signing identity, not a partition's own setting -- one
    # key/cert signs whichever UKI(s) the imager anchors (currently only
    # a 'where: /usr' 'verity: true' mount). Paths are resolved relative
    # to the current working directory a build is run from, like
    # 'multiconfig: files:', not like 'patches:'.
    def _parse_secure_boot(self, secure_boot):
        if type(secure_boot) != type({}):
            raise ValueError("'image: secure-boot' shall be a mapping")
        for field in ("private-key", "public-cert"):
            if field not in secure_boot:
                raise ValueError(
                    "'image: secure-boot' needs both 'private-key' and "
                    "'public-cert' -- '%s' is missing" % field)
            if type(secure_boot[field]) != type(""):
                raise ValueError(
                    "'image: secure-boot: %s' shall be a string" % field)
        return secure_boot

    def _parse_common(self, part):
        part["_blksz"] = 4096
        part["_depth"] = 0

        if "priority" not in part:
            part["priority"] = 500

        if "extra" in part:
            part["_size"] = self._from_human_size(part["extra"])
        else:
            part["_size"] = self._from_human_size("%dMiB" % PartitionHandler.DEFAULT_EXTRA_MB)

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
        is_verity_hash = part["type"] == VERITY_HASH_TYPE

        if "flags" in part:
            self._parse_part_flags(part)

        if "where" not in part and part["_lvm"] == False and not is_verity_hash:
            raise ValueError("'where' not defined in partition '%s'!" % label)
        if is_verity_hash and "where" in part:
            raise ValueError(
                "partition '%s' has type 'verity-hash', which is never "
                "mounted -- drop its 'where'" % label)
        if part["type"] in RO_FSTYPES and part["_lvm"] == False and self._table != "gpt":
            raise ValueError(
                "partition '%s' has a read-only type ('%s'), which needs a 'gpt' "
                "partition table to be identified in /etc/fstab (this image's "
                "table is '%s')" % (label, part["type"], self._table))
        if is_verity_hash and self._table != "gpt":
            raise ValueError(
                "partition '%s' has type 'verity-hash', which needs a 'gpt' "
                "partition table (this image's table is '%s')"
                % (label, self._table))
        if is_verity_hash and part["_lvm"]:
            raise ValueError(
                "partition '%s' has type 'verity-hash' and flag 'lvm', "
                "which may not be used together -- a verity-hash partition "
                "is always a plain GPT partition" % label)
        if part["_lvm"] == True:
            if "group" not in part:
                raise ValueError("target 'group' not defined for partition '%s'!" % label)
            elif part["group"] not in self.groups:
                self.groups.append(part["group"])
            if "size" not in part:
                raise ValueError("'size' of LVM partition '%s' was not defined!" % label)
            else:
                part["_size"] = part["size"]
        if is_verity_hash:
            if "verity-for" not in part:
                raise ValueError(
                    "partition '%s' has type 'verity-hash', which needs a "
                    "'verity-for' naming the partition it protects" % label)
            if "size" not in part:
                raise ValueError(
                    "'size' of verity-hash partition '%s' was not defined "
                    "(its hash tree's size is only known once built, so it "
                    "cannot be inferred)" % label)
            part["_size"] = part["size"]
        if "verity" in part:
            if type(part["verity"]) != type(True):
                raise ValueError("partition '%s': 'verity' shall be true or false" % label)
            if part["verity"] and part["type"] not in RO_FSTYPES:
                raise ValueError(
                    "partition '%s' has 'verity: true', which needs a "
                    "read-only type ('squashfs'/'erofs') (this one is '%s')"
                    % (label, part["type"]))
            if part["verity"] and part.get("identify") == "partuuid":
                raise ValueError(
                    "partition '%s' has 'verity: true', which is never "
                    "identified in /etc/fstab (it has no fstab entry at all "
                    "-- drop 'identify: partuuid')" % label)
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

    # 'source' names which tarball 'f' came from -- 'None' for the
    # specification's own (today's only case), or a declared 'multiconfig:'
    # group otherwise. Only a mount/bootlet with the matching 'source' is a
    # candidate, so two groups' same-named files never fight over one size.
    def distribute(self, f, source=None):
        if f.name.startswith("/") == False:
            name = "/" + f.name
        else:
            name = f.name

        if source is None:
            for bootlet in self.bootlets:
                if name == bootlet["file"]:
                    bootlet["_size"] = f.size
                    break

        for mount in self.mounts:
            if mount.get("source") != source:
                continue
            if mount["_prefix"] is not None and name.startswith(mount["_prefix"]):
                mount["_size"] = mount["_size"] + self._size_file(f, mount)
                return mount
        return None

    def compute_sizes(self):
        # check if all bootlets were found
        for bootlet in self.bootlets:
            if "_size" not in bootlet:
                raise RuntimeError("bootlet '%s' was not found in the image!" % bootlet["file"])

        # start offset for bootlets/partitions
        if self._table == "msdos":
            start = 1      # MBR is 512 bytes long, round up to 1 KiB
        elif self._table == "gpt":
            start = 34 * 4 # 34 LBAs of 4KiB each
        else:
            raise RuntimeError("'%s' is not a supported partition table!" % self._table)

        # compute start offset for each bootlet (set internal "_seek" attribute)
        for bootlet in self.bootlets:
            start = self._align_up(start, bootlet["_align"]) # honor "align" setting
            bootlet["_seek"] = start                         # start of this bootlet (with requested alignment)
            size = math.ceil(bootlet["_size"] / 1024)        # size in KiB
            start = start + size                             # start of bootlet/partition following this bootlet

        # make sure partitions do not start before START_OFFSET_KB
        # (start offset still in KiB at this point)
        if start < PartitionHandler.START_OFFSET_KB:
            start = PartitionHandler.START_OFFSET_KB

        # compute offset to first partition in bytes and rounded to the next MiB
        start = self._to_rounded_mib(start)
        self._start_offset = start

        # keep 1MiB at the end of the media to hold a backup copy of the partition table
        self._min_size = (start + 1) * 1024 * 1024

        # add estimated size of each partition
        for mount in self.mounts:
            mount["_size"] = self._to_rounded_mib(mount["_size"]) * 1024 * 1024
            if "size" in mount and mount["size"] > mount["_size"]:
                mount["_size"] = mount["size"]
            self._min_size = self._min_size + mount["_size"]

        # A physical partition with no mount (an LVM PV container, or a
        # verity-hash partition) skips the loop above -- its '_size' is
        # already final from _parse_part(), so just add it here. Missing
        # this under-sized the disk for one of these to actually fit,
        # caught by a real verity build.
        mounted = {id(m) for m in self.mounts}
        for part in self.partitions:
            if id(part) not in mounted:
                self._min_size = self._min_size + self._to_rounded_mib(part["_size"]) * 1024 * 1024

        # compute the physical placement (start/end, in MiB) of each partition
        # on the device now that every partition's final _size is known (note
        # self.mounts and self.partitions share the same dicts for mountable
        # partitions, so the rounding above already updated part["_size"] too)
        layout_start = self._start_offset
        for part in self.partitions:
            part["_start_mib"] = layout_start
            layout_start = layout_start + self._to_rounded_mib(part["_size"])
            part["_end_mib"] = layout_start

    def print_stats(self):
        print("prologue:\t%s" % self._to_human_size(self._start_offset))
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

        if "bootlets" in image:
            bootlets = image["bootlets"]
            for bootlet in bootlets:
                bootlet = self._parse_bootlet(bootlet)
                self.bootlets.append(bootlet)
            self.bootlets = sorted(self.bootlets, key=lambda b: b["priority"])
        image["bootlets"] = self.bootlets

        if "secure-boot" in image:
            self.secure_boot = self._parse_secure_boot(image["secure-boot"])

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
            image["volumes"] = sorted(self.volumes, key=lambda p: p["priority"])

        self.mounts = sorted(self.mounts, key=lambda vol: vol["_depth"], reverse=True)
        self._validate_sources(spec)
        self._validate_verity(spec)
        return spec

    # A partition/volume's 'source:' routes its content to a declared
    # 'multiconfig:' group's rootfs instead of this specification's own
    # ('source' absent, the default). A group nothing yet names is left
    # alone; a group some mount does name needs exactly one root
    # ('where: "/"') among them -- groups are side-by-side OSes, not
    # partitions of one, so zero or more than one is an error here.
    def _validate_sources(self, spec):
        groups = spec.get("multiconfig") or {}
        referenced = {}
        for mount in self.mounts:
            source = mount.get("source")
            if source is None:
                continue
            if source not in groups:
                raise ValueError(
                    "'%s' names 'source: %s', which is not one of the "
                    "declared 'multiconfig:' groups (%s)"
                    % (mount["label"], source,
                       ", ".join(sorted(groups)) if groups else "none"))
            referenced.setdefault(source, []).append(mount)
        for name, mounts in referenced.items():
            roots = [m for m in mounts if m["_prefix"] == "/"]
            if len(roots) != 1:
                raise ValueError(
                    "'multiconfig:' group '%s' needs exactly one partition "
                    "or volume with 'source: %s' and 'where: \"/\"' (found %d)"
                    % (name, name, len(roots)))

    # Every 'verity: true' partition needs exactly one 'verity-hash'
    # partition naming it back via 'verity-for:', sharing its 'source:',
    # and mounted at '/' or '/usr' -- the only mountpoints DPS defines an
    # auto-discovered Verity partition type GUID for (see imager.py's
    # GPT_TYPE_ROOT_VERITY/GPT_TYPE_USR_VERITY).
    def _validate_verity(self, spec):
        by_label = {p["label"]: p for p in self.partitions}
        protected = {p["label"] for p in self.partitions if p.get("verity")}
        paired = set()
        for part in self.partitions:
            if part["type"] != VERITY_HASH_TYPE:
                continue
            label = part["label"]
            target = part["verity-for"]
            data = by_label.get(target)
            if data is None:
                raise ValueError(
                    "partition '%s' names 'verity-for: %s', which is not "
                    "one of the declared partitions" % (label, target))
            if not data.get("verity"):
                raise ValueError(
                    "partition '%s' names 'verity-for: %s', which does not "
                    "have 'verity: true' set" % (label, target))
            if target in paired:
                raise ValueError(
                    "partition '%s' has more than one 'verity-hash' "
                    "partition naming it in 'verity-for:'" % target)
            paired.add(target)
            if data.get("source") != part.get("source"):
                raise ValueError(
                    "partition '%s' and '%s' must share the same 'source:' "
                    "to be verity-paired" % (label, target))
            if data["_prefix"] not in ("/", "/usr/"):
                raise ValueError(
                    "'verity: true' is only supported on '/' or '/usr' "
                    "partitions (DPS defines no auto-discovered Verity "
                    "partition type for '%s')" % data["_prefix"])
            if data["_lvm"]:
                raise ValueError(
                    "'verity: true' is not supported on '%s': it is an LVM "
                    "logical volume, which has no GPT partition UUID for "
                    "DPS auto-discovery to pair a verity-hash partition "
                    "against" % target)
        missing = protected - paired
        if missing:
            raise ValueError(
                "the following partitions have 'verity: true' but no "
                "'verity-hash' partition names them in 'verity-for:': %s"
                % ", ".join(sorted(missing)))

