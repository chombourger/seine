#!/usr/bin/env python3

import avocado
import os
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import ansible_runner
from seine.ansible_runner import AnsibleContainerRunner

DISTRO = {"source": "debian", "release": "trixie", "architecture": "amd64",
          "uri": "http://example.com/debian"}

# The runner's own shell, run here rather than in a container: the two
# directories are told to it, so what is tested is the script that seeds
# apt and the one that copies back.
class Runner(AnsibleContainerRunner):
    def __init__(self, archives, cache):
        super().__init__(None, DISTRO, {})
        self.archives, self.cache = archives, cache

    def _exec(self, args, check=True):
        script = args[-1].replace(ansible_runner.ARCHIVES, self.archives) \
                         .replace(ansible_runner.DOWNLOADS, self.cache)
        return subprocess.run(["sh", "-c", script], check=False)

class TheCacheIsNotAptsOwnDirectory(avocado.Test):
    def setUp(self):
        self.archives = os.path.join(self.workdir, "archives")
        self.cache = os.path.join(self.workdir, "cache")
        for path in [self.archives, self.cache]:
            os.makedirs(path, exist_ok=True)
        self.runner = Runner(self.archives, self.cache)

    def deb(self, where, name, content):
        with open(os.path.join(where, name), "w") as f:
            f.write(content)

    def names(self, where):
        return sorted(os.listdir(where))

    # Two builds mounting one directory put two apts on one lock, and the
    # second fails with 'Unable to lock directory'.
    def test_the_shared_cache_is_mounted_somewhere_else(self):
        volumes = self.runner._volumes()
        self.assertTrue(any(v.endswith(":%s" % ansible_runner.DOWNLOADS)
                            for v in volumes), volumes)
        self.assertFalse(any(ansible_runner.ARCHIVES in v for v in volumes),
                         "apt's own archives directory is mounted again")

    def test_what_another_build_fetched_is_seeded(self):
        self.deb(self.cache, "one_1.0_amd64.deb", "one")
        self.deb(self.cache, "two_2.0_amd64.deb", "two")

        self.runner._seed_downloads()
        self.assertEqual(self.names(self.archives),
                         ["one_1.0_amd64.deb", "two_2.0_amd64.deb"])

    def test_an_empty_cache_seeds_nothing_and_says_nothing(self):
        self.runner._seed_downloads()
        self.assertEqual(self.names(self.archives), [])

    def test_what_this_build_fetched_is_kept(self):
        self.deb(self.cache, "one_1.0_amd64.deb", "the cached one")
        self.deb(self.archives, "one_1.0_amd64.deb", "this build's one")
        self.deb(self.archives, "new_3.0_amd64.deb", "new")

        self.runner._save_downloads()
        self.assertEqual(self.names(self.cache),
                         ["new_3.0_amd64.deb", "one_1.0_amd64.deb"])
        # A name the cache already holds is left alone rather than written
        # over, so a build never rewrites what another may be reading.
        with open(os.path.join(self.cache, "one_1.0_amd64.deb")) as f:
            self.assertEqual(f.read(), "the cached one")

    # Nothing half-written is left under a name the next build would read.
    def test_the_copy_leaves_no_temporary_behind(self):
        self.deb(self.archives, "new_3.0_amd64.deb", "new")

        self.runner._save_downloads()
        self.assertEqual(self.names(self.cache), ["new_3.0_amd64.deb"])

    def test_an_empty_archives_keeps_nothing(self):
        self.runner._save_downloads()
        self.assertEqual(self.names(self.cache), [])

if __name__ == "__main__":
    avocado.main()

# What a running system keeps in memory rather than on disk. This
# container's file-system becomes the image, and podman leaves a tmpfs out
# of what it exports -- so what a maintainer script writes to /run or /tmp
# stops shipping, podman's own /run/.containerenv included.
class RuntimeDirectoriesAreNotPartOfTheImage(avocado.Test):
    def command(self):
        runner = AnsibleContainerRunner(None, DISTRO, {})
        return runner.container_command("rootfs/debian/trixie/amd64")

    def test_both_are_mounted_as_tmpfs(self):
        command = self.command()
        for where in ["/run", "/tmp"]:
            self.assertIn(where, command)
            self.assertEqual(command[command.index(where) - 1], "--tmpfs",
                             "%s is not asked for as a tmpfs" % where)

    def test_the_image_is_still_what_is_run(self):
        # The mounts go in front of the image, where podman takes its
        # options: behind it they would be arguments to 'sleep'.
        command = self.command()
        image = command.index("rootfs/debian/trixie/amd64")
        self.assertLess(command.index("--tmpfs"), image)
        self.assertEqual(command[image + 1:], ["sleep", "infinity"])
