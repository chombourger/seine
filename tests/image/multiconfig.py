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

# As tests/image/images.py's own: a real build, so this does not run
# unless asked for.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# One disk, two independently-packaged operating systems, each addressed
# from the partition table by 'source:' -- the point of the whole
# feature. Both groups' own rootfs mount at 'where: "/"', on two
# different partitions; what is under test is that each partition's
# content came from its own group and no other's.
class TwoSourcesOnOneDisk(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds an image; this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)

    # A plain apt install of a small, already-built package -- nothing
    # here is rebuilt from source, only whether the right one landed on
    # the right partition is under test.
    def _group(self, name, package):
        path = os.path.join(self.workdir, "%s.yaml" % name)
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: %s\n"
                "playbook:\n"
                "    - name: %s marker\n"
                "      tasks:\n"
                "          - name: install %s\n"
                "            apt:\n"
                "                name: %s\n"
                "                state: present\n"
                % (HOST_ARCH, name, package, package))
        return path

    def specification(self):
        main = self._group("main", "busybox-static")
        recovery = self._group("recovery", "dropbear-bin")
        disk = os.path.join(self.workdir, "disk.img")
        outer = os.path.join(self.workdir, "outer.yaml")
        with open(outer, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: %s\n"
                "multiconfig:\n"
                "    main:\n"
                "        - %s\n"
                "    recovery:\n"
                "        - %s\n"
                "image:\n"
                "    filename: %s\n"
                "    table: gpt\n"
                "    partitions:\n"
                "        - label: main-root\n"
                "          source: main\n"
                "          where: /\n"
                "          size: 768MiB\n"
                "        - label: recovery-root\n"
                "          source: recovery\n"
                "          where: /\n"
                "          size: 768MiB\n"
                % (HOST_ARCH, main, recovery, disk))
        return outer, disk

    # Read out of dpkg's own status database rather than guessed by path:
    # what matters is whether the package made it in, not where its own
    # packaging happens to put a binary.
    def _installed(self, g, package):
        status = g.read_file("/var/lib/dpkg/status").decode("utf-8", "replace")
        pattern = r"^Package: %s\n(?:[^\n]+\n)*?Status: install ok installed\n" \
            % re.escape(package)
        return re.search(pattern, status, re.MULTILINE) is not None

    def test_each_partitions_content_comes_from_its_own_group(self):
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
            # sda1 is 'main-root', sda2 'recovery-root' -- declaration
            # order, both default priority (PartitionHandler.parse()).
            g.mount_ro("/dev/sda1", "/")
            self.assertTrue(self._installed(g, "busybox-static"),
                            "main's own package never reached main's partition")
            self.assertFalse(self._installed(g, "dropbear-bin"),
                             "recovery's package leaked into main's partition")
            g.umount("/")

            g.mount_ro("/dev/sda2", "/")
            self.assertTrue(self._installed(g, "dropbear-bin"),
                            "recovery's own package never reached recovery's partition")
            self.assertFalse(self._installed(g, "busybox-static"),
                             "main's package leaked into recovery's partition")
            g.umount("/")
        finally:
            g.close()
        os.remove(disk)

if __name__ == "__main__":
    avocado.main()
