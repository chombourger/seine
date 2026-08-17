# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Read-only browsing of a *finished* disk image, via guestfs. Which
# partition is '/' comes from the same PartitionHandler mount metadata
# the imager used to write the image, not guessed via inspect_os().
# Read-only throughout: every mount is mount_ro, the drive readonly=True.

import copy
import os

from seine.partition import PartitionHandler

class Inspector:
    def __init__(self, spec, image_path):
        if not os.path.isfile(image_path):
            raise OSError("no such image: %s" % image_path)
        self.image_path = image_path
        self.ph = PartitionHandler()
        # parse() mutates the dict it's given, so a copy -- a caller may
        # hand the same spec to a new Inspector again (the TUI does, on
        # every /cd).
        self.ph.parse(copy.deepcopy(spec))
        self._g = None

    # Imported here, not at module load: a machine without
    # python3-guestfs can still import this module -- only opening an
    # image needs the real library.
    def __enter__(self):
        import guestfs
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(self.image_path, format="raw", readonly=True)
        g.launch()

        # Same device-numbering Imager.create() wrote with: Nth partition
        # is /dev/sda<N>. LVM volumes need their group activated first.
        if len(self.ph.volumes) > 0:
            g.vgscan()
            g.vg_activate_all(True)
        part_devices = {id(part): "/dev/sda%d" % index
                        for index, part in enumerate(self.ph.partitions, start=1)}
        vol_devices = {id(vol): "/dev/%s/%s" % (vol["group"], vol["label"])
                       for vol in self.ph.volumes}

        # Parents before children, exactly as the imager mounted them --
        # '/boot' has to exist under '/' before it can be mounted itself.
        for mount in sorted(self.ph.mounts, key=lambda m: m["_depth"]):
            dev = part_devices.get(id(mount)) or vol_devices.get(id(mount))
            if dev is None:
                raise ValueError(
                    "no partition or volume found for mount '%s'" % mount.get("where"))
            g.mount_ro(dev, mount["_prefix"])

        self._g = g
        return self

    def __exit__(self, *args):
        if self._g is not None:
            self._g.close()
        self._g = None
        return False

    # One entry per name: (name, kind, size, target). 'kind' matches
    # guestfs's own ftyp letters (d/l/r/...). A symlink carries its
    # target instead of a size.
    def ls(self, path="/"):
        entries = []
        for entry in sorted(self._g.readdir(path), key=lambda e: e["name"]):
            name = entry["name"]
            if name in (".", ".."):
                continue
            kind = entry["ftyp"]
            full = os.path.join(path, name)
            if kind == "l":
                entries.append((name, kind, None, self._g.readlink(full)))
            else:
                entries.append((name, kind, self._g.statns(full)["st_size"], None))
        return entries

    def is_dir(self, path):
        return bool(self._g.is_dir(path))

    # 'read_file' (bytes), not 'cat' (str) -- 'cat' stops at the first
    # embedded NUL, which a real config file or binary can easily have.
    def cat(self, path):
        return self._g.read_file(path)

    def readlink(self, path):
        return self._g.readlink(path)
