#!/usr/bin/env python3

import avocado
import os
import shutil
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..")
sys.path.append(path_to_sources)

import seine.oci_bundle as oci_bundle
from seine.utils import ContainerEngine

class ABundleIsImportedBeforeBuilding(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "build")

        self.real_run = ContainerEngine.run
        self.loaded = []
        ContainerEngine.run = lambda cmd, check=False, loaded=self.loaded: \
            loaded.append(cmd)

        oci_bundle._attempted = False
        self.bundle_dir = os.path.join(self.workdir, "oci")
        os.makedirs(self.bundle_dir, exist_ok=True)
        self.real_bundle_dir = oci_bundle.BUNDLE_DIR
        oci_bundle.BUNDLE_DIR = self.bundle_dir

    def tearDown(self):
        ContainerEngine.run = self.real_run
        oci_bundle.BUNDLE_DIR = self.real_bundle_dir
        oci_bundle._attempted = False
        os.environ.clear()
        os.environ.update(self.environment)

    def release_dir(self, release="bookworm"):
        path = os.path.join(self.bundle_dir, release)
        os.makedirs(path, exist_ok=True)
        return path

    def test_a_bundled_chroot_is_copied_into_the_cache(self):
        release = self.release_dir()
        chroots = os.path.join(release, "chroots", "bookworm", "amd64")
        os.makedirs(chroots)
        with open(os.path.join(chroots, "bookworm-amd64.tar.zst"), "wb") as f:
            f.write(b"x" * 1024)

        oci_bundle.import_bundled()

        copied = os.path.join(ContainerEngine.cache("chroots"), "bookworm",
                              "amd64", "bookworm-amd64.tar.zst")
        with open(copied, "rb") as f:
            self.assertEqual(f.read(), b"x" * 1024)

    def test_a_bundled_images_archive_is_loaded(self):
        release = self.release_dir()
        images = os.path.join(release, "images.tar.gz")
        open(images, "wb").close()

        oci_bundle.import_bundled()

        self.assertEqual(self.loaded, [["load", "-i", images]])

    def test_importing_is_attempted_only_once(self):
        release = self.release_dir()
        open(os.path.join(release, "images.tar.gz"), "wb").close()

        oci_bundle.import_bundled()
        oci_bundle.import_bundled()

        self.assertEqual(len(self.loaded), 1)

    def test_a_release_with_neither_file_does_nothing(self):
        self.release_dir("bookworm")

        oci_bundle.import_bundled()

        self.assertEqual(self.loaded, [])

    def test_nothing_happens_without_a_bundle_directory(self):
        shutil.rmtree(self.bundle_dir)
        oci_bundle.import_bundled()
