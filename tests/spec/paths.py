#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.image import Image
from seine.utils import ContainerEngine

# Nothing is set, so seine's caches and its storage are where they have
# always been -- and each variable moves what it says it moves, and only
# that.
class WhereSeineKeepsThings(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        for name in ["SEINE_CACHE_DIR", "SEINE_BUILD_DIR", "XDG_CACHE_HOME",
                     "TMPDIR"]:
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_nothing_set(self):
        home = os.path.expanduser("~")
        self.assertEqual(ContainerEngine.cache("chroots"),
                         os.path.join(home, ".cache", "seine", "chroots"))
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(home, ".local", "share", "seine"))

    def test_the_standard_cache_variable_is_honoured(self):
        os.environ["XDG_CACHE_HOME"] = "/drive/caches"
        self.assertEqual(ContainerEngine.cache("chroots"),
                         "/drive/caches/seine/chroots")

    def test_seines_own_wins_over_it(self):
        os.environ["XDG_CACHE_HOME"] = "/drive/caches"
        os.environ["SEINE_CACHE_DIR"] = "/drive/seine-caches"
        self.assertEqual(ContainerEngine.cache("chroots"),
                         "/drive/seine-caches/chroots")
        # The caches move; what a build makes for itself does not.
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(os.path.expanduser("~"), ".local",
                                      "share", "seine"))

    def test_the_build_directory_moves_storage_and_scratch(self):
        os.environ["TMPDIR"] = "/tmp/ignored"
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(self.workdir, "drive", "storage"))
        self.assertEqual(ContainerEngine.scratch(),
                         os.path.join(self.workdir, "drive", "tmp"))
        # The caches are not build output and stay where they were.
        self.assertTrue(ContainerEngine.cache().startswith(
            os.path.expanduser("~")))

    # The scratch space follows TMPDIR when nothing says otherwise, which
    # is what keeps a kernel tree off a tmpfs.
    def test_the_scratch_space_follows_tmpdir(self):
        os.environ["TMPDIR"] = os.path.join(self.workdir, "elsewhere")
        self.assertEqual(ContainerEngine.scratch(),
                         os.path.join(self.workdir, "elsewhere", "seine"))

# podman keeps two directories: the images and the state of what is running.
# They belong together -- a storage of one's own with the default runroot is
# a build sharing podman's idea of what is mounted with every other seine on
# the machine.
class TheRunrootGoesWithTheStorage(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ.pop("SEINE_BUILD_DIR", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

    # The storage that is already in place keeps the runroot it was created
    # with: podman records it and refuses to open a storage with another.
    def test_the_default_storage_is_left_as_it_is(self):
        self.assertIsNone(ContainerEngine.runroot())
        self.assertNotIn("--runroot", ContainerEngine._podman_cmd(["images"]))

    def test_it_moves_with_the_build_directory(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        self.assertTrue(ContainerEngine.runroot().startswith(self.workdir))
        self.assertNotEqual(ContainerEngine.runroot(), ContainerEngine.root())

    # Every podman seine runs is told both, or the pair is pointless.
    def test_podman_is_told_both(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        cmd = ContainerEngine._podman_cmd(["images"])
        self.assertEqual(cmd[0], "podman")
        self.assertIn("--root", cmd)
        self.assertIn("--runroot", cmd)
        self.assertEqual(cmd[cmd.index("--root") + 1], ContainerEngine.root())
        self.assertEqual(cmd[cmd.index("--runroot") + 1],
                         ContainerEngine.runroot())



# A container that exists only to be read and removed again takes a name of
# its own: it was the image's, so two builds extracting from one image
# collided and podman failed the second with 'name already in use'.
class ScratchContainersDoNotShareAName(avocado.Test):
    def test_two_readers_of_one_image_differ(self):
        image = "imager-kernel/debian/bookworm/amd64"
        names = {ContainerEngine._scratch_name(image) for _ in range(20)}
        self.assertEqual(len(names), 20, "two readers picked one name")

    # Still recognisable as what it is reading, for anyone looking at
    # 'podman ps' while a build runs.
    def test_the_name_says_which_image_it_is(self):
        name = ContainerEngine._scratch_name("imager-kernel/debian/bookworm/amd64")
        self.assertTrue(name.startswith("imager-kernel-debian-bookworm-amd64-"),
                        name)
        self.assertNotIn("/", name)

# One directory per specification, a run of it per build: what a build wrote
# sits beside what the builds before it wrote.
class WhereAStepsOutputGoes(avocado.Test):
    def setUp(self):
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.image = Image(None, {"files": ["demo.yml"], "keep": False,
                                  "verbose": False})

    def tearDown(self):
        os.environ.pop("SEINE_BUILD_DIR", None)

    def test_runs_of_one_specification_share_a_directory(self):
        first = self.image._logs()
        second = self.image._logs()
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.dirname(first), os.path.dirname(second))
        self.assertTrue(os.path.basename(os.path.dirname(first)).startswith("logs-"))

    def test_another_specification_is_another_directory(self):
        other = Image(None, {"files": ["other.yml"], "keep": False,
                             "verbose": False})
        self.assertNotEqual(os.path.dirname(self.image._logs()),
                            os.path.dirname(other._logs()))

    # The digest is of the names, so an edit does not scatter a
    # specification's logs across directories.
    def test_the_same_files_name_the_same_directory(self):
        again = Image(None, {"files": ["demo.yml"], "keep": False,
                             "verbose": False})
        self.assertEqual(os.path.dirname(self.image._logs()),
                         os.path.dirname(again._logs()))

    def test_without_files_it_still_has_somewhere_to_write(self):
        logs = Image(None, {"keep": False, "verbose": False})._logs()
        self.assertTrue(os.path.isdir(logs))

if __name__ == "__main__":
    avocado.main()
