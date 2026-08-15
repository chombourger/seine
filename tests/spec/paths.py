#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.image import Image
from seine.utils import ContainerEngine
from seine import utils

# Nothing is set, so everything defaults to ./build under the working
# directory -- and each variable moves what it says it moves, and only
# that.
class WhereSeineKeepsThings(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        for name in ["SEINE_CACHE_DIR", "SEINE_BUILD_DIR", "SEINE_DL_DIR",
                     "SEINE_LOG_DIR", "SEINE_DEPLOY_DIR", "SEINE_TMP_DIR",
                     "XDG_CACHE_HOME", "TMPDIR"]:
            os.environ.pop(name, None)
        self.cwd = os.getcwd()
        os.chdir(self.workdir)

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self.environment)

    def test_nothing_set(self):
        build = os.path.join(self.workdir, "build")
        self.assertEqual(ContainerEngine.cache("chroots"),
                         os.path.join(build, "cache", "chroots"))
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(build, "containers"))
        self.assertEqual(ContainerEngine.downloads_root(),
                         os.path.join(build, "downloads"))
        self.assertEqual(ContainerEngine.logs_root(),
                         os.path.join(build, "logs"))
        self.assertEqual(ContainerEngine.deploy_root(),
                         os.path.join(build, "deploy"))
        self.assertEqual(ContainerEngine.scratch(),
                         os.path.join(build, "tmp"))

    # XDG_CACHE_HOME and TMPDIR are honoured only by tools that have
    # nowhere of their own; seine always has ./build, so neither moves
    # anything here.
    def test_xdg_cache_home_and_tmpdir_are_not_honoured(self):
        os.environ["XDG_CACHE_HOME"] = "/drive/caches"
        os.environ["TMPDIR"] = "/tmp/elsewhere"
        build = os.path.join(self.workdir, "build")
        self.assertEqual(ContainerEngine.cache("chroots"),
                         os.path.join(build, "cache", "chroots"))
        self.assertEqual(ContainerEngine.scratch(),
                         os.path.join(build, "tmp"))

    def test_seine_cache_dir_wins_over_the_build_directory(self):
        os.environ["SEINE_CACHE_DIR"] = "/drive/seine-caches"
        self.assertEqual(ContainerEngine.cache("chroots"),
                         "/drive/seine-caches/chroots")
        # The caches move; what a build makes for itself does not.
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(self.workdir, "build", "containers"))

    def test_the_build_directory_moves_everything(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(self.workdir, "drive", "containers"))
        self.assertEqual(ContainerEngine.scratch(),
                         os.path.join(self.workdir, "drive", "tmp"))
        # Without an override of their own, the caches, downloads and logs
        # move too: one drive holds everything a build makes or keeps.
        self.assertEqual(ContainerEngine.cache(),
                         os.path.join(self.workdir, "drive", "cache"))
        self.assertEqual(ContainerEngine.downloads_root(),
                         os.path.join(self.workdir, "drive", "downloads"))
        self.assertEqual(ContainerEngine.logs_root(),
                         os.path.join(self.workdir, "drive", "logs"))
        self.assertEqual(ContainerEngine.deploy_root(),
                         os.path.join(self.workdir, "drive", "deploy"))

    # A relative SEINE_BUILD_DIR still resolves to one place regardless of
    # what a step changes its own working directory to.
    def test_a_relative_build_directory_is_made_absolute(self):
        os.environ["SEINE_BUILD_DIR"] = "relative-drive"
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(self.workdir, "relative-drive", "containers"))

    # Each specific variable still moves its one thing on its own, and
    # wins over SEINE_BUILD_DIR when both are set.
    def test_the_specific_variables_win_over_the_build_directory(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "caches")
        os.environ["SEINE_DL_DIR"] = os.path.join(self.workdir, "dl")
        os.environ["SEINE_LOG_DIR"] = os.path.join(self.workdir, "logdir")
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "out")
        os.environ["SEINE_TMP_DIR"] = os.path.join(self.workdir, "scratch")
        self.assertEqual(ContainerEngine.cache(),
                         os.path.join(self.workdir, "caches"))
        self.assertEqual(ContainerEngine.downloads_root(),
                         os.path.join(self.workdir, "dl"))
        self.assertEqual(ContainerEngine.logs_root(),
                         os.path.join(self.workdir, "logdir"))
        self.assertEqual(ContainerEngine.deploy_root(),
                         os.path.join(self.workdir, "out"))
        self.assertEqual(ContainerEngine.scratch(),
                         os.path.join(self.workdir, "scratch"))
        # Storage has no variable of its own; it still follows the build
        # directory.
        self.assertEqual(ContainerEngine.root(),
                         os.path.join(self.workdir, "drive", "containers"))

# A spec's deliverable is not scattered output like a log or a cache: a
# bare filename follows SEINE_DEPLOY_DIR/SEINE_BUILD_DIR, but a path the
# spec chose for itself is never second-guessed.
class WhereASpecsImageLands(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ.pop("SEINE_BUILD_DIR", None)
        os.environ.pop("SEINE_DEPLOY_DIR", None)
        self.cwd = os.getcwd()
        os.chdir(self.workdir)

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.clear()
        os.environ.update(self.environment)

    def _output(self, filename, release="bookworm"):
        image = Image(None, {"keep": False, "verbose": False})
        image.parse({"distribution": {"release": release},
                     "image": {"filename": filename}})
        return image._output

    def test_unset_a_relative_filename_goes_under_build_deploy(self):
        self.assertEqual(self._output("demo.img"),
                         os.path.join(self.workdir, "build", "deploy",
                                      "bookworm", "demo.img"))

    def test_the_build_directory_takes_a_relative_filename(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        self.assertEqual(self._output("demo.img"),
                         os.path.join(self.workdir, "drive", "deploy",
                                      "bookworm", "demo.img"))

    def test_deploy_dir_wins_over_the_build_directory(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "out")
        self.assertEqual(self._output("demo.img"),
                         os.path.join(self.workdir, "out", "bookworm", "demo.img"))

    # Two releases built from one checkout don't overwrite each other's
    # image.
    def test_two_releases_land_in_different_directories(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        self.assertNotEqual(self._output("demo.img", release="bookworm"),
                            self._output("demo.img", release="trixie"))

    def test_an_absolute_filename_is_never_redirected(self):
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "drive")
        absolute = os.path.join(self.workdir, "elsewhere", "demo.img")
        self.assertEqual(self._output(absolute), absolute)

# podman keeps two directories: the images and the state of what is running.
# They belong together -- seine's storage is always its own, under
# build_dir(), so it always pairs its own runroot too.
class TheRunrootGoesWithTheStorage(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ.pop("SEINE_BUILD_DIR", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

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
        # Named by the files' own digest -- no redundant "logs-" prefix,
        # already under logs_root().
        self.assertEqual(os.path.basename(os.path.dirname(first)),
                         utils.digest(["demo.yml"], 8))

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
