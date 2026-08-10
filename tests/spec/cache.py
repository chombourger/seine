#!/usr/bin/env python3

import avocado
import io
import contextlib
import json
import os
import sys
import tarfile

path_to_self   = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine       import cache
from seine       import cache_index
from seine.cache import CacheCmd, CACHES, IMAGES, PORTABLE

# A cache with something in it, under the test's own directory rather than
# the user's -- said the way a user would say it, with the two environment
# variables, so what is tested is where seine really looks.
#
# The images are the exception: they are podman's, and none of these tests
# needs podman to answer what a directory can. A machine with a real one is
# what the round trip in tests/spec/images.py is for.
class Caches(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "build")

        self.podman = (cache.images, cache.images_size,
                       CacheCmd._clear_images, CacheCmd._export_images)
        cache.images = lambda: []
        cache.images_size = lambda: 0
        CacheCmd._clear_images = lambda self: None
        CacheCmd._export_images = lambda self, tar, where, rootfs=False: None

        self.paths = {}
        for name in CACHES:
            if CACHES[name] is None:
                continue
            path = CACHES[name]()
            self.assertTrue(path.startswith(self.workdir),
                            "%s is not under the test's directory: %s"
                            % (name, path))
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "blob"), "wb") as f:
                f.write(b"x" * 2048)
            self.paths[name] = path

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)
        (cache.images, cache.images_size,
         CacheCmd._clear_images, CacheCmd._export_images) = self.podman

    def run_cmd(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                CacheCmd().main(argv)
            except SystemExit as e:
                self.assertEqual(e.code, 0)
        return out.getvalue()

class WhatIsCachedIsReported(Caches):
    def test(self):
        shown = self.run_cmd(["info"])
        for name in CACHES:
            self.assertIn(name, shown)
        for name, path in self.paths.items():
            self.assertIn(path, shown)
        # Four directories of 2KiB each, and podman holding nothing.
        self.assertIn("8.0 KiB", shown)

class OneCacheIsClearedWithoutTheOthers(Caches):
    def test(self):
        self.run_cmd(["clear", "chroots"])
        self.assertFalse(os.path.isdir(self.paths["chroots"]))
        self.assertTrue(os.path.isdir(self.paths["downloads"]))

class EveryCacheIsClearedWhenNoneIsNamed(Caches):
    def test(self):
        self.run_cmd(["clear"])
        for path in self.paths.values():
            self.assertFalse(os.path.isdir(path))

    def test_all_says_the_same_thing(self):
        self.run_cmd(["clear", "all"])
        for path in self.paths.values():
            self.assertFalse(os.path.isdir(path))

class ACacheIsCarriedToAnotherMachine(Caches):
    def test(self):
        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        # Scratch is not worth carrying and is left out of the tar. The
        # images are podman's here, so this machine has none.
        with tarfile.open(where) as tar:
            tops = set(name.split("/")[0] for name in tar.getnames())
        self.assertEqual(tops, set(PORTABLE) - {IMAGES})

        self.run_cmd(["clear"])
        self.run_cmd(["import", where])
        for name in set(PORTABLE) - {IMAGES}:
            with open(os.path.join(self.paths[name], "blob"), "rb") as f:
                self.assertEqual(len(f.read()), 2048)
        self.assertFalse(os.path.isdir(self.paths["scratch"]))

    def test_one_cache_is_taken_from_the_tar(self):
        where = os.path.join(self.workdir, "caches.tar.gz")
        self.run_cmd(["export", where])
        self.run_cmd(["clear"])
        self.run_cmd(["import", where, "chroots"])
        self.assertTrue(os.path.isfile(os.path.join(self.paths["chroots"], "blob")))
        self.assertFalse(os.path.isdir(self.paths["downloads"]))

    # A cache holds links as well as files -- sbuild names the latest of a
    # package's build logs with one -- and a link that stays inside its
    # cache is carried like anything else.
    def test_a_link_inside_a_cache_survives(self):
        link = os.path.join(self.paths["packages"], "linux-latest.deb")
        os.symlink("blob", link)
        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        self.run_cmd(["clear"])
        self.run_cmd(["import", where])
        self.assertEqual(os.readlink(link), "blob")

    # What a real packages cache holds beyond the .debs. The stamps are
    # carried: they say the .debs are current and which of them belong to
    # which source package, which is how an import knows what it supersedes.
    # The index is not -- it is made from whatever the directory holds, so
    # the machine that receives the .debs writes its own. Nor is a lock, a
    # hash cache, or a build log belonging to another machine.
    def test_only_what_another_machine_needs_is_carried(self):
        repository = self.paths["packages"]
        os.makedirs(os.path.join(repository, ".stamps"), exist_ok=True)
        for name in ["Packages", "Packages.gz", ".stamps/linux_0123456789abcdef",
                     "linux_6.1_amd64.changes", "linux_6.1_amd64.buildinfo",
                     ".packages.db", "blob.lock", "lock",
                     "linux_6.1_amd64-2026-08-10T00:00:00Z.build"]:
            open(os.path.join(repository, name), "w").close()
        # sbuild's symlink to the latest log goes with the logs.
        os.symlink("linux_6.1_amd64-2026-08-10T00:00:00Z.build",
                   os.path.join(repository, "linux_6.1_amd64.build"))

        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        with tarfile.open(where) as tar:
            carried = set(name.removeprefix("packages/").rstrip("/")
                          for name in tar.getnames()
                          if name.startswith("packages/"))
        self.assertEqual(carried - {""},
                                  {".stamps", ".stamps/linux_0123456789abcdef",
                                   "blob", "linux_6.1_amd64.changes",
                                   "linux_6.1_amd64.buildinfo"})

    # apt makes its 'partial' directory as root inside the container and
    # 0700 with it, so an export that reads every directory it walks does
    # not get as far as writing a tar at all.
    def test_a_directory_that_cannot_be_read_is_not_walked(self):
        partial = os.path.join(self.paths["downloads"], "partial")
        os.makedirs(partial, exist_ok=True)
        open(os.path.join(partial, "half.deb"), "w").close()
        os.chmod(partial, 0o000)
        try:
            where = os.path.join(self.workdir, "caches.tar")
            self.run_cmd(["export", where])
            with tarfile.open(where) as tar:
                self.assertIn("downloads/blob", tar.getnames())
                self.assertNotIn("downloads/partial", tar.getnames())
        finally:
            os.chmod(partial, 0o700)

    # The image's own root file-system is the one thing an export leaves
    # behind, and the flag that says otherwise has to reach the export.
    def test_the_image_rootfs_is_asked_for_by_name(self):
        asked = []
        CacheCmd._export_images = \
            lambda self, tar, where, rootfs=False: asked.append(rootfs)

        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        self.run_cmd(["export", where, "--with-image-rootfs"])
        self.assertEqual(asked, [False, True])

    def test_the_flag_belongs_to_export_alone(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["info", "--with-image-rootfs"])
        self.assertNotEqual(caught.exception.code, 0)

    def test_scratch_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["export", os.path.join(self.workdir, "x.tar"),
                                 "scratch"])
        self.assertNotEqual(caught.exception.code, 0)

# A tar handed to seine may name anything at all; it is the one place seine
# writes files it did not make itself.
class ATarThatReachesOutOfItsCacheIsRefused(Caches):
    def craft(self, name, kind=tarfile.REGTYPE, target=None):
        where = os.path.join(self.workdir, "evil.tar")
        with tarfile.open(where, "w") as tar:
            member = tarfile.TarInfo(name)
            member.type = kind
            if target is not None:
                member.linkname = target
            member.size = 0
            tar.addfile(member, io.BytesIO(b""))
        return where

    def refuses(self, where):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    CacheCmd().main(["import", where])
        self.assertNotEqual(caught.exception.code, 0)

    def test_a_parent_directory(self):
        self.refuses(self.craft("downloads/../../evil"))

    def test_an_absolute_path(self):
        self.refuses(self.craft("/etc/evil"))

    def test_a_cache_it_does_not_know(self):
        self.refuses(self.craft("elsewhere/evil"))

    def test_a_symlink_out_of_the_cache(self):
        self.refuses(self.craft("downloads/evil", tarfile.SYMTYPE, "/etc/passwd"))
        self.assertFalse(os.path.lexists(os.path.join(self.paths["downloads"],
                                                      "evil")))

    def test_a_symlink_climbing_out_of_the_cache(self):
        self.refuses(self.craft("downloads/evil", tarfile.SYMTYPE, "../../../etc"))

    def test_a_hard_link_out_of_the_cache(self):
        self.refuses(self.craft("downloads/evil", tarfile.LNKTYPE, "/etc/passwd"))

    def test_a_device(self):
        self.refuses(self.craft("downloads/evil", tarfile.CHRTYPE))

class AnUnknownCacheIsRefused(Caches):
    def test(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["clear", "kitchen-sink"])
        self.assertNotEqual(caught.exception.code, 0)
        self.assertTrue(os.path.isdir(self.paths["downloads"]))

if __name__ == "__main__":
    avocado.main()

# What is cached, one object at a time and oldest use first, which is the
# order to read it in when the question is what to remove.
class WhatIsCachedIsListedOneByOne(Caches):
    def setUp(self):
        super().setUp()
        index = cache_index.Index()
        index.made(cache_index.CHROOT, "bookworm-amd64")
        index.made(cache_index.PACKAGE, "bookworm/amd64/busybox")
        index.hit(cache_index.PACKAGE, "bookworm/amd64/busybox")

    def test(self):
        shown = self.run_cmd(["info", "--entries"])
        self.assertIn("bookworm-amd64", shown)
        self.assertIn("bookworm/amd64/busybox", shown)
        # The one nothing reached for since it was made comes first.
        self.assertLess(shown.index("bookworm-amd64"),
                        shown.index("bookworm/amd64/busybox"))

    def test_only_the_caches_asked_about(self):
        shown = self.run_cmd(["info", "--entries", "chroots"])
        self.assertIn("bookworm-amd64", shown)
        self.assertNotIn("busybox", shown)

    # A cleared cache stops being talked about: an entry left behind would
    # be reported as a very old object and evicted a second time.
    def test_clearing_a_cache_forgets_what_was_in_it(self):
        self.run_cmd(["clear", "chroots"])
        shown = self.run_cmd(["info", "--entries"])
        self.assertNotIn("bookworm-amd64", shown)
        self.assertIn("bookworm/amd64/busybox", shown)

    def test_an_empty_index_says_so(self):
        cache_index.Index().forget()
        self.assertIn("nothing recorded yet",
                      self.run_cmd(["info", "--entries"]))

class TheEntriesFlagBelongsToInfoAlone(Caches):
    def test(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                CacheCmd().main(["clear", "--entries"])
        self.assertNotEqual(caught.exception.code, 0)

# An import brings .debs and the stamps that describe them, so a repository
# that had its own build of the same source package has to give way: two
# versions of one package in a flat repository means apt installs the higher
# of them, which is neither what a specification pinned nor what a stamp
# describes.
class AnImportSupersedesWhatItReplaces(Caches):
    def repository(self, space="cache"):
        path = os.path.join(self.workdir, space, "packages", "bookworm", "amd64")
        os.makedirs(os.path.join(path, ".stamps"), exist_ok=True)
        return path

    def built(self, repository, source, digest, files, index=True):
        for name in files:
            with open(os.path.join(repository, name), "wb") as f:
                f.write(b"deb")
        with open(os.path.join(repository, ".stamps",
                               "%s_%s" % (source, digest)), "w") as f:
            f.write("\n".join(files) + "\n")
        if index:
            for derived in ["Packages", "Packages.gz"]:
                with open(os.path.join(repository, derived), "w") as f:
                    f.write("Package: %s\n" % source)

    def theirs(self):
        # A tar written by a machine that built the same source differently.
        where = os.path.join(self.workdir, "theirs.tar")
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "theirs")
        try:
            self.built(self.repository("theirs"), "linux", "bbbbbbbbbbbbbbbb",
                       ["linux-image_6.2_amd64.deb"])
            self.run_cmd(["export", where, "packages"])
        finally:
            os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")
        return where

    def test(self):
        mine = self.repository()
        self.built(mine, "linux", "aaaaaaaaaaaaaaaa", ["linux-image_6.1_amd64.deb"])
        self.run_cmd(["import", self.theirs()])

        held = sorted(os.listdir(mine))
        self.assertIn("linux-image_6.2_amd64.deb", held)
        self.assertNotIn("linux-image_6.1_amd64.deb", held,
                         "the superseded .deb is still there: apt would "
                         "install whichever version is higher")
        # And with it the stamp that named it.
        self.assertEqual(os.listdir(os.path.join(mine, ".stamps")),
                         ["linux_bbbbbbbbbbbbbbbb"])
        # The index described the repository as it was a moment ago; it is
        # made from the directory, so it is left for the next build to write.
        self.assertFalse(os.path.isfile(os.path.join(mine, "Packages")))

    # A package the tar says nothing about is nobody's business but this
    # machine's.
    def test_what_they_did_not_build_is_left_alone(self):
        mine = self.repository()
        self.built(mine, "busybox", "cccccccccccccccc", ["busybox_1.35_amd64.deb"])
        self.run_cmd(["import", self.theirs()])
        self.assertIn("busybox_1.35_amd64.deb", os.listdir(mine))

    # A .deb no stamp names is a leftover rather than something superseded,
    # and removing it is guesswork rather than following a stamp.
    def test_what_no_stamp_names_waits_for_force(self):
        mine = self.repository()
        with open(os.path.join(mine, "orphan_1.0_amd64.deb"), "wb") as f:
            f.write(b"deb")
        where = self.theirs()

        shown = self.run_cmd(["import", where])
        self.assertIn("orphan_1.0_amd64.deb", os.listdir(mine))
        self.assertIn("--force", shown)

        self.run_cmd(["import", where, "--force"])
        self.assertNotIn("orphan_1.0_amd64.deb", os.listdir(mine))

# What a runner starting from nothing wants: this machine looking like that
# tar, rather than the two of them merged.
class AnImportCanReplaceWhatIsThere(Caches):
    def test(self):
        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where, "chroots"])
        with open(os.path.join(self.paths["chroots"], "mine"), "wb") as f:
            f.write(b"only here")

        self.run_cmd(["import", where, "chroots", "--replace"])
        self.assertEqual(sorted(os.listdir(self.paths["chroots"])), ["blob"])

    def test_extending_is_what_it_does_otherwise(self):
        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where, "chroots"])
        with open(os.path.join(self.paths["chroots"], "mine"), "wb") as f:
            f.write(b"only here")

        self.run_cmd(["import", where, "chroots"])
        self.assertEqual(sorted(os.listdir(self.paths["chroots"])),
                         ["blob", "mine"])

class TheRecordTravelsWithTheCaches(Caches):
    def test(self):
        index = cache_index.Index()
        index.made(cache_index.CHROOT, "bookworm-amd64")
        index.hit(cache_index.CHROOT, "bookworm-amd64")
        theirs = index.entries()[0][2]

        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where, "chroots"])
        with tarfile.open(where) as tar:
            self.assertIn("index.json", tar.getnames())

        # Arriving on a machine that has never seen it: made then, used now.
        index.forget()
        self.run_cmd(["import", where, "chroots"])
        (kind, key, entry), = index.entries()
        self.assertEqual((kind, key), (cache_index.CHROOT, "bookworm-amd64"))
        self.assertEqual(entry["made"], theirs["made"])
        self.assertEqual(entry["uses"], 0)

    # An import told to take one cache says nothing about the others.
    def test_only_the_caches_asked_for(self):
        index = cache_index.Index()
        index.made(cache_index.CHROOT, "bookworm-amd64")
        index.made(cache_index.PACKAGE, "bookworm/amd64/busybox")

        where = os.path.join(self.workdir, "caches.tar")
        self.run_cmd(["export", where])
        index.forget()
        self.run_cmd(["import", where, "chroots"])
        self.assertEqual([kind for kind, _, _ in index.entries()],
                         [cache_index.CHROOT])

# Every image seine builds says what it is, rather than inheriting the
# answer from the image it was built on: podman hands an image its base's
# labels, so the builder -- built on the tooling -- would call itself
# tooling, and the imager's kernel would call itself a root file-system.
class EveryImageSaysWhatItIs(avocado.Test):
    def test(self):
        from seine.bootstrap import HostBootstrap, TargetBootstrap
        from seine.cache import CARRIED_KINDS
        from seine.imager_appliance import ImagerAppliance
        from seine.imager_kernel import ImagerKernel
        from seine.sbuild import BuilderImage
        from seine.transport_bootstrap import TransportBootstrap
        from seine.utils import BUILDER_KIND, IMAGER_KIND, ROOTFS_KIND
        from seine.utils import TOOLING_KIND, TRANSPORT_KIND

        for cls, kind in [(HostBootstrap, TOOLING_KIND),
                          (TargetBootstrap, ROOTFS_KIND),
                          (BuilderImage, BUILDER_KIND),
                          (ImagerKernel, IMAGER_KIND),
                          (ImagerAppliance, IMAGER_KIND),
                          (TransportBootstrap, TRANSPORT_KIND)]:
            self.assertEqual(cls.kind, kind,
                             "%s says it is a %s" % (cls.__name__, cls.kind))

        # Everything but the root file-system: what stands on that one is
        # still current on the machine that receives it, since what decides
        # is what its base was built from and not which bytes it came out as.
        self.assertEqual(sorted(CARRIED_KINDS),
                         sorted([TOOLING_KIND, BUILDER_KIND, IMAGER_KIND,
                                 TRANSPORT_KIND]))
        self.assertNotIn(ROOTFS_KIND, CARRIED_KINDS)

# Which of a storage's images go into a tar, decided by what each says it is.
class OnlyWhatAnotherMachineCanUseIsCarried(avocado.Test):
    def setUp(self):
        from seine.utils import ContainerEngine
        self.asked = ContainerEngine.check_output
        storage = [
            {"Names": ["localhost/bootstrap/debian/trixie/all:latest"],
             "Labels": {"seine.kind": "tooling"}},
            {"Names": ["localhost/builder/debian/trixie:latest"],
             "Labels": {"seine.kind": "builder"}},
            {"Names": ["localhost/bootstrap/debian/trixie/amd64:latest"],
             "Labels": {"seine.kind": "rootfs"}},
            {"Names": ["localhost/imager-kernel/debian/trixie/amd64:latest"],
             "Labels": {"seine.kind": "imager"}},
            {"Names": ["localhost/transport-bootstrap/amd64/base:latest"],
             "Labels": {"seine.kind": "transport"}},
            # A base image nothing here built, and one an older seine did.
            {"Names": ["docker.io/library/debian:trixie"], "Labels": None},
            {"Names": ["localhost/bootstrap/debian/bookworm/all:latest"],
             "Labels": {}},
            # An intermediate layer, which has no name to ask for it by.
            {"Names": ["<none>:<none>"], "Labels": None},
        ]
        ContainerEngine.check_output = \
            lambda cmd: json.dumps(storage).encode()

    def tearDown(self):
        from seine.utils import ContainerEngine
        ContainerEngine.check_output = self.asked

    def test(self):
        carried = cache.images()
        self.assertEqual(sorted(carried), sorted([
            "localhost/bootstrap/debian/trixie/all:latest",
            "localhost/builder/debian/trixie:latest",
            "localhost/imager-kernel/debian/trixie/amd64:latest",
            "localhost/transport-bootstrap/amd64/base:latest",
            "docker.io/library/debian:trixie"]))
        # The one an export leaves behind, and the one an older seine left
        # unlabelled, which is rebuilt on sight anyway.
        self.assertNotIn("localhost/bootstrap/debian/trixie/amd64:latest", carried)
        self.assertNotIn("localhost/bootstrap/debian/bookworm/all:latest", carried)

    def test_with_the_image_rootfs(self):
        carried = cache.images(True)
        self.assertIn("localhost/bootstrap/debian/trixie/amd64:latest", carried)
        self.assertIn("localhost/imager-kernel/debian/trixie/amd64:latest", carried)
        # Still nothing that cannot be named.
        self.assertNotIn("<none>:<none>", carried)
