#!/usr/bin/env python3

import avocado
import os
import sys
import tarfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.image import Image
from seine.partition import PartitionHandler

# podman exiting zero is not evidence that a root file-system came out of
# it. What an empty or cut-short export produced used to be written into
# the image and reported by libguestfs three steps later, as a missing
# /etc/fstab.
class AnExportIsCheckedForARootFileSystem(avocado.Test):
    def setUp(self):
        self.image = Image(PartitionHandler(),
                           {"verbose": False, "keep": False})

    def tar(self, name, members):
        path = os.path.join(self.workdir, name)
        with tarfile.open(path, "w") as tar:
            for member in members:
                info = tarfile.TarInfo(member)
                info.size = 0
                tar.addfile(info)
        return path

    def test_a_root_file_system_is_taken(self):
        tarball = self.tar("root.tar", ["./bin/sh", "./etc/hostname", "./usr/lib"])
        self.assertEqual(self.image._exported(tarball), tarball)

    # Whatever podman writes when it exports nothing useful.
    def test_an_empty_tar_is_refused(self):
        tarball = self.tar("empty.tar", [])
        with self.assertRaises(RuntimeError) as caught:
            self.image._exported(tarball)
        self.assertIn("/etc", str(caught.exception))

    def test_a_tar_without_an_etc_is_refused(self):
        tarball = self.tar("no-etc.tar", ["./bin/sh", "./usr/lib/x"])
        with self.assertRaises(RuntimeError) as caught:
            self.image._exported(tarball)
        self.assertIn(tarball, str(caught.exception))

    # Named without the leading './', as some tars do.
    def test_either_way_of_naming_a_member_counts(self):
        tarball = self.tar("plain.tar", ["bin/sh", "etc/hostname"])
        self.assertEqual(self.image._exported(tarball), tarball)

    # The walk is bounded, so a tar of something else does not read a
    # gigabyte before saying so.
    def test_the_search_gives_up_rather_than_reading_everything(self):
        tarball = self.tar("many.tar",
                           ["./var/%d" % n for n in range(Image.ROOT_EVIDENCE + 50)])
        with self.assertRaises(RuntimeError):
            self.image._exported(tarball)

if __name__ == "__main__":
    avocado.main()
