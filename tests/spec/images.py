#!/usr/bin/env python3

import avocado
import glob
import os
import shutil
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import ContainerEngine, HOST_ARCH

EXAMPLES = os.path.join(path_to_sources, "examples")

# What a build must not leave in the directory it was run from. A root
# file-system and the imager's unpacked appliance are both large, and both
# belong in the scratch space, which is reported by 'seine cache info' and
# emptied by 'seine cache clear'. Someone's checkout is not a scratch
# space.
def leftovers():
    return sorted(glob.glob(os.path.join(path_to_sources, "root-*.tar"))
                  + [path for path in glob.glob(os.path.join(path_to_sources, "tmp*"))
                     if os.path.isdir(path)])

# Building an image takes a kernel build with it, so these do not run
# unless asked for. Not a tag alone: a tag says which tests to select,
# and the default run selects everything.
PLAN = os.environ.get("SEINE_TEST_PLAN", "")

# Which fragment builds the 6.18 kernel for a release. bookworm names a
# version because the packaging it needs is in backports; trixie takes
# what the release has. Both describe the same kernel, which is the point
# of them being two files rather than two copies.
KERNELS = {
    "bookworm": "linux-6.18/bookworm.yml",
    "trixie":   "linux-6.18/kernel.yml",
}

# An image built from the examples as they are shipped, for one release
# and one board. Nothing is copied here: the specification is the files
# under examples/, composed on the command line the way a user composes
# them, with a release chosen at the front and the kernel fragment at the
# back. What this test adds is where to write the image, so four of them
# can be built without agreeing on a filename.
#
# The kernel is part of it on purpose. A build that takes the
# distribution's kernel exercises none of what makes these examples
# interesting -- the graft, the flavour coming from the architecture, the
# cross toolchain the profile selects, the component the firmware needs.
class Image(avocado.Test):
    """
    :avocado: disable
    """

    # A kernel, a root file-system and an image, twice over for an
    # emulated architecture. Not a limit that means anything -- it is
    # there so a wedged build eventually reports rather than hangs.
    timeout = 21600

    architecture = None
    image = None
    release = None

    def setUp(self):
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds images; this takes hours")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")

    @property
    def filename(self):
        return os.path.join(self.workdir, "%s-%s.img" % (self.image, self.release))

    def specification(self):
        names = [
            "common/%s.yaml" % self.release,
            "common/%s.yaml" % self.architecture,
            "common/%s.yaml" % self.image,
            "common/conf-accounts.yaml",
            "common/conf-locales.yaml",
            KERNELS[self.release],
        ]
        specs = [os.path.join(EXAMPLES, name) for name in names]
        for spec in specs:
            self.assertTrue(os.path.isfile(spec), "no such specification: %s" % spec)

        # Where to write it, which is the one thing the examples cannot
        # say for a test building four of them.
        where = os.path.join(self.workdir, "filename.yml")
        with open(where, "w") as f:
            f.write("image:\n    filename: %s\n" % self.filename)
        return specs + [where]

    def test(self):
        # ansible-playbook lives beside the python running the tests when
        # they are run from a virtual environment, and the build needs it.
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))

        # --jobs: what this plan is for is building the examples, and a
        # build that runs its own steps together is the one users will
        # run. The four tests may themselves run together, which is what
        # the cache locks are for.
        #
        # --require-hashes: an example that loses its hash, or grows a new
        # source without one, fails here rather than quietly downloading
        # whatever the URL serves that day.
        # To the log as it goes, so a build can be watched while it runs
        # rather than read once it has ended.
        log = os.path.join(self.outputdir, "build.log")
        with open(log, "w") as f:
            build = subprocess.run(
                [sys.executable, "-u", "./seine.py", "build", "-v",
                 "--jobs", "4", "--require-hashes"]
                + self.specification(),
                cwd=path_to_sources, env=environment, stdout=f,
                stderr=subprocess.STDOUT)
        self.assertEqual(build.returncode, 0,
                         "building %s for %s failed, see %s"
                         % (self.image, self.release, log))

        self.assertTrue(os.path.isfile(self.filename),
                        "no image at %s" % self.filename)
        self.assertGreater(os.path.getsize(self.filename), 0, "the image is empty")
        self.assertEqual(leftovers(), [], "the build left these behind")

        # The kernel that came out is the grafted one rather than the
        # distribution's: an image is produced either way, and the
        # difference is only visible in what was built to make it.
        repository = ContainerEngine.packages(self.release)
        debs = glob.glob(os.path.join(
            repository, "linux-image-*unreleased*_%s.deb" % self.architecture))
        self.assertNotEqual(debs, [], "no grafted kernel in %s" % repository)

class PcImageBookworm(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "amd64"
    image = "pc-image"
    release = "bookworm"

class PcImageTrixie(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "amd64"
    image = "pc-image"
    release = "trixie"

# arm64 on an amd64 machine: the kernel is cross-compiled and the imager
# appliance runs under emulation, which is the slowest of the four and the
# one that has the most to say when it breaks.
class Rpi4ImageBookworm(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "arm64"
    image = "rpi4-image"
    release = "bookworm"

class Rpi4ImageTrixie(Image):
    """
    :avocado: tags=full,container
    """
    architecture = "arm64"
    image = "rpi4-image"
    release = "trixie"

# Which board's example this machine can build without emulating anything.
# The round trip below is about what a cache carries, not about an
# appliance running under qemu, so it takes the native one.
NATIVE = {"amd64": "pc-image", "arm64": "rpi4-image"}

# What a developer handed someone else's cache actually gets.
#
# Two spaces of their own, a build in each, and a tar in between: the first
# builds from nothing and exports what it kept, the second imports that and
# builds the same specification. What the second one must not do is the work
# the first one already did -- rebuild the package, unpack a buildd chroot,
# or make the containers again -- and what it must still do is produce an
# image, since a cache that cannot be built from is not a cache.
#
# The specification is the busybox rebuild rather than a kernel: it is a
# real package build, with a patch and a stamp of its own, and it needs
# no kernel compile to prove it.
#
# Both spaces are the test's own, so nothing here touches the caches or the
# storage of whoever is running it.
#
# Once per release, because what unpacks a buildd chroot is the sbuild of
# the release being built and the two do not agree: bookworm's takes the
# first name in its cache directory that could be a chroot tarball, while
# trixie's skips the empty ones first. A cache seine writes has more than
# the tarball in it, so a name that is safe on one is not evidence about the
# other.
class CarriedCache(avocado.Test):
    """
    :avocado: disable
    """
    timeout = 21600

    release = None

    def setUp(self):
        # Before anything that may cancel: tearDown runs either way.
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds images; this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to build an image")
        self.architecture = HOST_ARCH
        if self.architecture not in NATIVE:
            self.cancel("no example image for %s" % self.architecture)

    # podman's storage holds files belonging to uids this user cannot unlink
    # outside a user namespace, so removing a space is podman's job -- and
    # avocado cannot clean up the workdir until it is done.
    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space],
                           check=False)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        # ansible-playbook lives beside the python running the tests when
        # they are run from a virtual environment, and the build needs it.
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_CACHE_DIR"] = os.path.join(path, "cache")
        environment["SEINE_BUILD_DIR"] = os.path.join(path, "build")
        self.spaces.append(path)
        return environment

    # Straight to its file rather than through a pipe, and '-u' so the file
    # is written as the build goes rather than when it ends. A build that
    # has to finish before anything can be read is a build diagnosed after
    # the fact, and the log of the step that went wrong is the first thing
    # worth looking at while it is still going.
    def seine(self, space, args, log):
        where = os.path.join(self.outputdir, "%s.log" % log)
        with open(where, "w") as f:
            run = subprocess.run(
                [sys.executable, "-u", "./seine.py"] + args,
                cwd=path_to_sources, env=space, stdout=f,
                stderr=subprocess.STDOUT)
        with open(where, "r", errors="replace") as f:
            said = f.read()
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))
        return said

    def specification(self, filename):
        names = [
            "common/%s.yaml" % self.release,
            "common/%s.yaml" % self.architecture,
            "common/%s.yaml" % NATIVE[self.architecture],
            "rebuild-busybox/busybox.yaml",
        ]
        specs = [os.path.join(EXAMPLES, name) for name in names]
        for spec in specs:
            self.assertTrue(os.path.isfile(spec), "no such specification: %s" % spec)

        where = os.path.join(self.workdir, "%s.yml" % os.path.basename(filename))
        with open(where, "w") as f:
            f.write("image:\n    filename: %s\n" % filename)
        return specs + [where]

    def images_in(self, space):
        listed = subprocess.check_output(
            ["podman", "--root", os.path.join(space["SEINE_BUILD_DIR"], "storage"),
             "images", "--format", "{{.Repository}} {{.Id}}"]).decode()
        return dict(line.split() for line in listed.splitlines()
                    if len(line.split()) == 2 and "<none>" not in line)

    def test(self):
        first = self.space("first")
        second = self.space("second")
        built = os.path.join(self.workdir, "first.img")

        # One machine, from nothing.
        self.seine(first, ["build", "-v", "--jobs", "4"]
                          + self.specification(built), "build-first")
        self.assertTrue(os.path.isfile(built), "no image at %s" % built)

        carried = os.path.join(self.workdir, "caches.tar")
        self.seine(first, ["cache", "export", carried], "export")
        self.seine(second, ["cache", "import", carried], "import")

        # The other machine, before it builds: the package is already built
        # and every image it needs is there, bar the root file-system, which
        # is deliberately not carried.
        planned = self.seine(second,
                             ["build", "--dry-run"] + self.specification(built),
                             "plan-second")
        self.assertIn("already built, and not built again", planned)
        self.assertIn("busybox", planned.split(
            "already built, and not built again")[1])

        images = self.images_in(second)
        self.assertNotEqual(images, {}, "no images were imported")
        for name in images:
            self.assertEqual(images[name], self.images_in(first)[name],
                             "%s is not the image the first machine had" % name)

        # Named rather than left to the loop above, which would be happy
        # with an empty answer: the tooling every container is made from, the
        # builder packages are built in, the kernel libguestfs boots, and the
        # transport bootstrap ansible connects through. The last two stand on
        # a root file-system this machine made for itself, and are current
        # all the same -- which is the whole point of what an image records
        # about its base.
        for kind in ["bootstrap/", "builder/", "imager-kernel/", "transport-"]:
            self.assertTrue(any(kind in name for name in images),
                            "no %s image was carried, only %s"
                            % (kind.rstrip("/"), sorted(images)))
        # And the one that is left behind on purpose.
        rootfs = "bootstrap/debian/%s/%s" % (self.release, self.architecture)
        self.assertFalse(any(rootfs in name for name in images),
                         "the image's own root file-system was carried")

        # What must not be touched by the second build, since the tar
        # brought it: the chroot it would otherwise spend a full mmdebstrap
        # run on, and the stamp that says the package is built.
        chroots = os.path.join(second["SEINE_CACHE_DIR"], "chroots")
        stamps = glob.glob(os.path.join(second["SEINE_CACHE_DIR"], "packages",
                                        "*", ".stamps", "busybox_*"))
        self.assertEqual(len(stamps), 1, "no busybox stamp in the imported cache")
        before = {path: os.stat(path)
                  for path in glob.glob(os.path.join(chroots, "*", "*", "*.tar.zst"))
                            + stamps}
        self.assertNotEqual(before, {}, "no chroot in the imported cache")

        # The index is derived from the .debs rather than carried, so the
        # tar does not bring one and the build about to run writes it.
        repository = glob.glob(os.path.join(second["SEINE_CACHE_DIR"],
                                            "packages", "*"))
        self.assertNotEqual(repository, [], "no repository was imported")
        self.assertFalse(os.path.isfile(os.path.join(repository[0], "Packages")),
                         "the tar carried a repository index")

        again = os.path.join(self.workdir, "second.img")
        self.seine(second, ["build", "-v", "--jobs", "4"]
                           + self.specification(again), "build-second")
        self.assertTrue(os.path.isfile(again), "no image at %s" % again)
        self.assertEqual(leftovers(), [], "the build left these behind")

        # Written by the build that needed it, from what the directory holds.
        self.assertTrue(os.path.isfile(os.path.join(repository[0], "Packages")),
                        "the build did not write a repository index")

        for path, stat in before.items():
            now = os.stat(path)
            self.assertEqual((now.st_ino, now.st_mtime_ns),
                             (stat.st_ino, stat.st_mtime_ns),
                             "%s was made again" % path)

        # And the images the second build used are the ones it was handed,
        # rather than ones it built for itself.
        for name, id in self.images_in(second).items():
            if name in images:
                self.assertEqual(id, images[name],
                                 "%s was built again" % name)

        # What the second machine did with what it was given, in its own
        # words: the record travelled with the caches, so the things it
        # reused are things it knows were made before it had them.
        listed = self.seine(second, ["cache", "info", "--entries"], "entries")
        self.assertIn("busybox", listed)
        self.assertIn("%s-%s" % (self.release, self.architecture), listed)

    # A cache is not a licence to skip a build that has changed: a
    # specification asking for something else is pending again, imported
    # cache or not.
    def test_a_changed_specification_is_still_built(self):
        first = self.space("first")
        built = os.path.join(self.workdir, "first.img")
        self.seine(first, ["build", "--packages-only", "-v", "--jobs", "4"]
                          + self.specification(built), "build-packages")

        carried = os.path.join(self.workdir, "caches.tar")
        self.seine(first, ["cache", "export", carried], "export")
        second = self.space("second")
        self.seine(second, ["cache", "import", carried], "import")

        # The same specification, plus a build option nobody asked for
        # before, which is part of what a stamp is made of.
        changed = os.path.join(self.workdir, "changed.yml")
        with open(changed, "w") as f:
            f.write("packages:\n"
                    "    - source: apt://busybox\n"
                    "      options: [nostrip]\n")
        planned = self.seine(second,
                             ["build", "--dry-run"]
                             + self.specification(built) + [changed],
                             "plan-changed")
        self.assertIn("package:busybox", planned)
        self.assertNotIn("already built, and not built again", planned)

# The two releases whose sbuild reads its cache directory differently. The
# specification is the same one either way: what is being tested is what a
# cache carries, and the release is the thing that changes how it is read.
class CarriedCacheBookworm(CarriedCache):
    """
    :avocado: tags=full,container
    """
    release = "bookworm"

class CarriedCacheTrixie(CarriedCache):
    """
    :avocado: tags=full,container
    """
    release = "trixie"

# Two scope roles against real archives and a real sbuild: one source
# fetched, two builds of it, and a repository per architecture with the
# same version in each.
#
# Cross-architecture on purpose. Native is the case where the two
# collapse into one build, which the unit tests already say; what is only
# testable here is the pair -- the host build native in its own chroot,
# the target build cross in the same one, and the two not writing over
# each other.
#
# busybox rather than a kernel: it is a real build with real build
# dependencies, and it needs no kernel compile to prove it. Everything is
# in spaces of its own, so nothing here touches the caches or the storage
# of whoever is running it.
class ScopedRebuild(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 7200

    # Which architecture is not this machine's, since that is the case
    # worth building: a host build and a target build that are different
    # builds.
    OTHER = {"amd64": "arm64", "arm64": "amd64"}

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds packages; this takes a while")
        if shutil.which("podman") is None:
            self.cancel("podman is needed to rebuild a package")
        if HOST_ARCH not in self.OTHER:
            self.cancel("no other architecture known for %s" % HOST_ARCH)
        self.architecture = self.OTHER[HOST_ARCH]

    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space],
                           check=False)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_CACHE_DIR"] = os.path.join(path, "cache")
        environment["SEINE_BUILD_DIR"] = os.path.join(path, "build")
        self.spaces.append(path)
        return environment

    def seine(self, space, args, log):
        where = os.path.join(self.outputdir, "%s.log" % log)
        with open(where, "w") as f:
            run = subprocess.run(
                [sys.executable, "-u", "./seine.py"] + args,
                cwd=path_to_sources, env=space, stdout=f,
                stderr=subprocess.STDOUT)
        with open(where, "r", errors="replace") as f:
            said = f.read()
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))
        return said

    # The release and architecture fragments as they are shipped, with the
    # rebuild written here: what the example says about busybox is right,
    # and what this test adds is who it is for.
    def specification(self):
        names = ["common/bookworm.yaml", "common/%s.yaml" % self.architecture]
        specs = [os.path.join(EXAMPLES, name) for name in names]
        for spec in specs:
            self.assertTrue(os.path.isfile(spec), "no such specification: %s" % spec)

        where = os.path.join(self.workdir, "scoped.yml")
        with open(where, "w") as f:
            f.write("packages:\n"
                    "    - source: apt://busybox\n"
                    "      scope: [host, target]\n"
                    "      profiles: [nocheck]\n"
                    "image:\n"
                    "    filename: scoped.img\n"
                    "    partitions:\n"
                    "        - label: rootfs\n"
                    "          where: /\n")
        return specs + [where]

    def repository(self, space):
        return os.path.join(space["SEINE_CACHE_DIR"], "packages", "bookworm")

    def debs(self, space, pattern):
        return sorted(os.path.basename(path) for path in
                      glob.glob(os.path.join(self.repository(space), pattern)))

    def test(self):
        space = self.space("scoped")
        # --packages-only: what is under test is the rebuilds, and a root
        # file-system and an image after them prove nothing more about
        # 'scope'.
        said = self.seine(space, ["build", "-v", "--jobs", "2",
                                  "--packages-only"] + self.specification(),
                          "build")

        # One source, fetched once, a build of it per architecture, and
        # one step publishing them.
        for architecture in [HOST_ARCH, self.architecture]:
            self.assertIn("package:busybox:%s" % architecture, said)
        self.assertEqual(
            len([line for line in said.splitlines() if "deploy:busybox" in line]),
            1, "publishing was not one step")
        # One fetch between the two builds, read off the steps rather than
        # off what the fetch printed: with more than one step running, what
        # a step's containers print goes to a log of its own.
        self.assertEqual(
            len([line for line in said.splitlines() if "fetch:busybox" in line]),
            1, "busybox was fetched more than once")

        # One repository holding both architectures, at the same version,
        # since one source package is what each build was handed.
        versions = set()
        for architecture in [HOST_ARCH, self.architecture]:
            debs = self.debs(space, "busybox_*_%s.deb" % architecture)
            self.assertNotEqual(debs, [], "no busybox built for %s" % architecture)
            versions.update(name.split("_")[1] for name in debs)
        self.assertEqual(len(versions), 1,
                         "the two builds are of different versions: %s"
                         % ", ".join(sorted(versions)))

        # And exactly one copy of the architecture-independent binary the
        # source also builds, made by the native build: two would be one
        # filename written twice, with whichever landed last deciding what
        # an image installs.
        self.assertEqual(len(self.debs(space, "*_all.deb")), 1,
                         "the arch-all package was not built exactly once")

        # Its index describes every architecture at once, which is what
        # lets one repository serve them all.
        with open(os.path.join(self.repository(space), "Packages")) as f:
            described = set(line.split()[1] for line in f
                            if line.startswith("Architecture:"))
        self.assertEqual(described,
                         set([HOST_ARCH, self.architecture, "all"]))

        # And a rebuild asks for neither of them again.
        planned = self.seine(space, ["build", "--dry-run", "--packages-only"]
                                    + self.specification(), "plan")
        for architecture in [HOST_ARCH, self.architecture]:
            self.assertIn("busybox:%s" % architecture, planned)
        self.assertIn("already built, and not built again", planned)

# Signing, against a real archive, a real sbuild and a real apt.
#
# A key made for the test in a GNUPGHOME of its own, so nothing here
# touches the keyring of whoever is running it and the key goes with the
# test. gpg runs on this machine either way -- that is the point of
# signing on the host -- so the only thing the containers ever see is a
# signature and a public key.
#
# What matters is the last step: apt reading the repository with
# verification on, no 'trusted=yes' anywhere, and installing out of it.
# Everything before that is only evidence for why it worked.
class SignedRebuild(avocado.Test):
    """
    :avocado: tags=full,container
    """
    timeout = 3600

    def setUp(self):
        self.spaces = []
        if PLAN != "full":
            self.cancel("SEINE_TEST_PLAN=full builds packages; this takes a while")
        for tool in ["podman", "gpg"]:
            if shutil.which(tool) is None:
                self.cancel("%s is needed to build a signed repository" % tool)

        self.gnupg = os.path.join(self.workdir, "gnupg")
        os.makedirs(self.gnupg, mode=0o700, exist_ok=True)
        made = subprocess.run(
            ["gpg", "--batch", "--quiet", "--passphrase", "",
             "--quick-generate-key", "Seine Image Test <image@example.invalid>",
             "default", "default", "never"],
            capture_output=True, env=dict(os.environ, GNUPGHOME=self.gnupg))
        if made.returncode != 0:
            self.cancel("could not make a key to sign with: %s"
                        % made.stderr.decode(errors="replace"))

    def tearDown(self):
        for space in self.spaces:
            subprocess.run(["podman", "unshare", "rm", "-rf", space], check=False)

    def space(self, name):
        path = os.path.join(self.workdir, name)
        environment = dict(os.environ)
        environment["PATH"] = "%s:%s" % (os.path.dirname(sys.executable),
                                         environment.get("PATH", ""))
        environment["SEINE_CACHE_DIR"] = os.path.join(path, "cache")
        environment["SEINE_BUILD_DIR"] = os.path.join(path, "build")
        environment["GNUPGHOME"] = self.gnupg
        self.spaces.append(path)
        return environment

    def seine(self, space, args, log):
        where = os.path.join(self.outputdir, "%s.log" % log)
        with open(where, "w") as f:
            run = subprocess.run([sys.executable, "-u", "./seine.py"] + args,
                                 cwd=path_to_sources, env=space, stdout=f,
                                 stderr=subprocess.STDOUT)
        self.assertEqual(run.returncode, 0,
                         "'%s' failed, see %s" % (" ".join(args), where))

    def specification(self):
        names = ["common/bookworm.yaml", "common/%s.yaml" % HOST_ARCH]
        specs = [os.path.join(EXAMPLES, name) for name in names]
        for spec in specs:
            self.assertTrue(os.path.isfile(spec), "no such specification: %s" % spec)

        where = os.path.join(self.workdir, "signed.yml")
        with open(where, "w") as f:
            f.write("packages:\n"
                    "    - source: apt://busybox\n"
                    "      profiles: [nocheck]\n"
                    "image:\n"
                    "    filename: signed.img\n"
                    "    partitions:\n"
                    "        - label: rootfs\n"
                    "          where: /\n")
        return specs + [where]

    def gpg(self, *arguments):
        return subprocess.run(["gpg"] + list(arguments), capture_output=True,
                              env=dict(os.environ, GNUPGHOME=self.gnupg))

    def test(self):
        space = self.space("signed")
        self.seine(space, ["build", "--packages-only", "-v",
                           "--sign-key", "image@example.invalid"]
                          + self.specification(), "build")

        repository = os.path.join(space["SEINE_CACHE_DIR"], "packages", "bookworm")
        held = os.listdir(repository)
        for name in ["InRelease", "Release", "Release.gpg"]:
            self.assertIn(name, held, "the repository was not signed")
        keyring = [name for name in held
                   if name.endswith(".gpg") and name != "Release.gpg"]
        self.assertEqual(len(keyring), 1,
                         "expected one key in the repository, found %s" % keyring)

        # Every signature is this key's, checked with gpg rather than
        # taken on trust from the file having a signature in it.
        signed = [os.path.join(repository, "InRelease")]
        signed += glob.glob(os.path.join(repository, "*.dsc"))
        signed += glob.glob(os.path.join(repository, "*.changes"))
        self.assertEqual(len(signed), 3, "not everything was signed: %s" % signed)
        for path in signed:
            self.assertEqual(self.gpg("--verify", path).returncode, 0,
                             "%s is not signed by the key that built it"
                             % os.path.basename(path))

        # And the thing all of it is for: apt reading the repository with
        # verification on, and installing out of it. No 'trusted=yes'
        # anywhere -- the key in the repository is what says it is good.
        script = (
            "rm -f /etc/apt/sources.list.d/*.list "
            "/etc/apt/sources.list.d/*.sources && "
            "install -D -m 0644 /packages/%s /etc/apt/keyrings/%s && "
            "echo 'deb [signed-by=/etc/apt/keyrings/%s] file:/packages ./' "
            "> /etc/apt/sources.list.d/seine.list && "
            "apt-get update -qq && "
            "apt-get install -y --no-install-recommends busybox-syslogd && "
            "dpkg-query -W -f '\\${Version}' busybox-syslogd"
            % (keyring[0], keyring[0], keyring[0]))
        installed = subprocess.run(
            ["podman", "--root", os.path.join(space["SEINE_BUILD_DIR"], "storage"),
             "run", "--rm", "-v", "%s:/packages:ro" % repository,
             "builder/debian/bookworm", "sh", "-c", script],
            capture_output=True)
        self.assertEqual(installed.returncode, 0,
                         "apt would not install from the signed repository:\n%s"
                         % installed.stderr.decode(errors="replace"))
        self.assertIn("+mod1", installed.stdout.decode(errors="replace"),
                      "what was installed is not the rebuilt package")

    # An unsigned build is the one everyone else runs, and it stays the
    # one that works: the repository is trusted for having been made here.
    def test_without_a_key_the_repository_is_unsigned(self):
        space = self.space("unsigned")
        self.seine(space, ["build", "--packages-only"] + self.specification(),
                   "build-unsigned")

        repository = os.path.join(space["SEINE_CACHE_DIR"], "packages", "bookworm")
        held = os.listdir(repository)
        self.assertIn("Packages", held)
        for name in ["InRelease", "Release", "Release.gpg"]:
            self.assertNotIn(name, held,
                             "%s was written for a build nothing signed" % name)
        self.assertEqual([n for n in held if n.endswith(".gpg")], [])
