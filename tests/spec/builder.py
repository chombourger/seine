#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import errno
import os
import shutil
import subprocess
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

# These build Builder objects directly, which write into a real cache on
# first use -- one throwaway directory per test process, not $HOME's.
os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
# And no key either: what a build signs with is read from the environment,
# so a developer who signs their own builds would otherwise run a
# different suite than everyone else.
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)

IMAGE = """
                image:
                    filename: packages-test.img
                    partitions:
                        - label: rootfs
                          where: /
"""

def parse(packages):
    build = BuildCmd()
    build.loads(packages + IMAGE)
    build.parse()
    return build

# As parse(), for what only means something once the architecture being
# built for is known -- which kernels a module has to name, above all.
def parse_for(architecture, packages):
    build = BuildCmd()
    build.loads("""
                distribution:
                    release: trixie
                    architecture: %s
    """ % architecture + packages + IMAGE)
    build.parse()
    return build

# The container that clones is given the agent's socket and nothing else,
# so what these check is that a key never leaves the host: the volumes a
# fetch over ssh asks for, and that a fetch not over ssh asks for none.
class SshFetch(avocado.Test):
    def builder(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def package(self, source):
        build = parse("""
                packages:
                    - source: %s
        """ % source)
        return build.image.packages[0]

    def setUp(self):
        self.sock = os.path.join(self.workdir, "agent.sock")
        with open(self.sock, "w") as f:
            pass
        os.environ["SSH_AUTH_SOCK"] = self.sock

    def tearDown(self):
        os.environ.pop("SSH_AUTH_SOCK", None)

class SshFetchForwardsTheAgent(SshFetch):
    def test(self):
        from seine.packages import SSH_AUTH_SOCK
        package = self.package(
            "git://git@example.com/team/busybox.git;protocol=ssh;rev=deadbeef")
        volumes, environment = self.builder()._ssh(package)
        self.assertIn((self.sock, SSH_AUTH_SOCK), volumes)
        self.assertEqual(environment, {"SSH_AUTH_SOCK": SSH_AUTH_SOCK})
        # Whatever else went in, no directory holding keys did.
        for host, _ in volumes:
            self.assertNotIn(os.path.basename(host), ["", ".ssh"])

class SshFetchWithoutAnAgentIsRejected(SshFetch):
    def test(self):
        os.environ.pop("SSH_AUTH_SOCK")
        package = self.package(
            "git://git@example.com/team/busybox.git;protocol=ssh;rev=deadbeef")
        try:
            self.builder()._ssh(package)
            self.fail("fetching over ssh succeeded with no agent running!")
        except ValueError:
            pass

class FetchWithoutSshForwardsNothing(SshFetch):
    def test(self):
        for source in ["apt://busybox",
                       "git://example.com/busybox.git;rev=deadbeef"]:
            volumes, environment = self.builder()._ssh(self.package(source))
            self.assertEqual(volumes, [], "'%s' asked for a volume" % source)
            self.assertEqual(environment, None,
                             "'%s' asked for an environment" % source)

# What sbuild was told to do, without running it: the builder image is
# replaced by one that writes down the arguments it is handed.
class RecordingBuilderImage:
    def __init__(self, workdir):
        self.workdir = workdir
        self.calls = []

    def exec(self, args, architecture=None, volumes=None, workdir=None,
             environment=None, check=True, tty=False):
        self.calls.append(args)
        if args[0] == "dpkg-source":
            open(os.path.join(self.workdir, "linux_1-1.dsc"), "w").close()
        return 0

class CrossBuildProfile(avocado.Test):
    def builder(self, architecture):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture, "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        image = RecordingBuilderImage(self.workdir)
        builder = Builder(distro, {}, image)
        builder.repository = lambda: self.workdir
        return builder, image

    def sbuild(self, architecture, profiles):
        from seine.utils import HOST_ARCH
        build = parse("""
                packages:
                    - source: apt://linux
                      profiles:
%s
        """ % "".join("                          - %s\n" % p for p in profiles))
        package = build.image.packages[0]
        builder, image = self.builder(architecture)
        source = os.path.join(self.workdir, "linux-1")
        os.makedirs(source, exist_ok=True)
        builder.build(package, self.workdir, "linux_1.dsc", "0", architecture,
                      self.workdir)
        # sbuild is run through a shell, so its arguments arrive as one
        # string rather than as a list.
        command = " ".join(image.calls[-1])
        return [a for a in command.split() if a.startswith("--profiles=")]

    def test(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"

        # 'cross' is dpkg's own profile, and the packaging build-depends on
        # a cross compiler under it: naming any profile takes sbuild's own
        # choice over, so seine has to put it back.
        self.assertEqual(self.sbuild(other, ["nocheck"]),
                         ["--profiles=nocheck,cross"])
        # A native build is not a cross build, whatever it names.
        self.assertEqual(self.sbuild(HOST_ARCH, ["nocheck"]),
                         ["--profiles=nocheck"])

class CoresPerBuild(CrossBuildProfile):
    def jobs(self, options, package_options=None):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        from seine.utils import HOST_ARCH
        distro = {"source": "debian", "release": "trixie",
                  "architecture": HOST_ARCH, "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        build = parse("""
                packages:
                    - source: apt://linux
                      options:
%s
        """ % "".join("                          - %s\n" % o
                      for o in (package_options or ["nocheck"])))
        builder = Builder(distro, options, BuilderImage(distro, options))
        return builder.parallel(build.image.packages[0])

    def test(self):
        cores = os.cpu_count() or 1

        # One build at a time gets the machine, as it always did.
        self.assertEqual(self.jobs({"jobs": 1}), cores)
        # Four at a time get a quarter each: raising --jobs divides the
        # machine rather than multiplying it.
        self.assertEqual(self.jobs({"jobs": 4}), max(1, cores // 4))
        # Unless told otherwise, which is the point of the knob.
        self.assertEqual(self.jobs({"jobs": 4, "parallel": 8}), 8)
        # A package whose build is broken in parallel says so itself, and
        # that beats both.
        self.assertEqual(
            self.jobs({"jobs": 1, "parallel": 8}, ["parallel=1"]), 1)

class DeclaredHashes(avocado.Test):
    def builder(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def package(self, source, digest=None):
        text = ("                packages:\n"
                "                    - source: %s\n" % source)
        if digest is not None:
            text += "                      sha256: %s\n" % digest
        return parse(text).image.packages[0]

    def fetched(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test(self):
        import hashlib
        content = b"a source package, more or less"
        digest = hashlib.sha256(content).hexdigest()
        name = "foo_1.0-1.dsc"
        self.fetched(name, content)
        builder = self.builder()

        # What was declared is what arrived.
        package = self.package("https://example.com/%s" % name, digest)
        builder._verify(package, self.workdir, name, package.sha256, "sha256")

        # And when it is not, the build stops rather than building it.
        wrong = "f" * 64
        package = self.package("https://example.com/%s" % name, wrong)
        try:
            builder._verify(package, self.workdir, name, package.sha256, "sha256")
            self.fail("a source that did not match its hash was accepted!")
        except ValueError as e:
            self.assertIn(digest, str(e))
            self.assertIn(wrong, str(e))

class HashesAreCheckedWhereTheyAreWritten(avocado.Test):
    def test(self):
        for digest in ["nope", "abc", "a" * 63, "g" * 64]:
            try:
                parse("""
                packages:
                    - source: https://example.com/foo_1.0-1.dsc
                      sha256: %s
                """ % digest)
                self.fail("'%s' was accepted as a sha256!" % digest)
            except ValueError:
                pass

class NothingVouchesForIt(DeclaredHashes):
    def test(self):
        import hashlib
        import io
        import contextlib

        content = b"whatever the server sent today"
        digest = hashlib.sha256(content).hexdigest()
        name = "foo_1.0-1.dsc"
        self.fetched(name, content)

        package = self.package("https://example.com/%s" % name)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.builder()._verify(package, self.workdir, name,
                                   package.sha256, "sha256")
        said = said.getvalue()

        # What it was, and what to write down so the next build knows.
        self.assertIn("nothing vouches", said)
        self.assertIn(digest, said)
        self.assertIn("sha256: %s" % digest, said)
        # And the file to write it in, which is the one that carried the
        # URI rather than whichever named the package.
        self.assertIn("<string>", said)

class RequireHashes(avocado.Test):
    def spec(self, packages):
        return packages + IMAGE

    def parsed(self, packages, require=True):
        build = BuildCmd()
        build.options["require_hashes"] = require
        build.loads(self.spec(packages))
        return build.parse()

    def test(self):
        # What answers for itself is left alone: an archive signature and
        # a commit hash are stronger than a hash written down beside a URL.
        self.parsed("""
                packages:
                    - source: apt://busybox
                    - source: git://example.com/bsp.git;rev=deadbeef
        """)

        # A hash that was declared is what was asked for.
        self.parsed("""
                packages:
                    - source: https://example.com/foo_1.0-1.dsc
                      sha256: %s
        """ % ("a" * 64))

    def test_unvouched_is_refused(self):
        try:
            self.parsed("""
                packages:
                    - source: https://example.com/foo_1.0-1.dsc
            """)
            self.fail("a source with nothing vouching for it was accepted!")
        except ValueError as e:
            self.assertIn("--require-hashes", str(e))
            self.assertIn("sha256", str(e))
            self.assertIn("foo_1.0-1.dsc", str(e))

    def test_unvouched_upstream_is_refused(self):
        try:
            self.parsed("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
            """)
            self.fail("an upstream with nothing vouching for it was accepted!")
        except ValueError as e:
            self.assertIn("upstream-sha256", str(e))

    def test_every_offender_at_once(self):
        try:
            self.parsed("""
                packages:
                    - source: https://example.com/one_1.0-1.dsc
                    - source: https://example.com/two_1.0-1.dsc
            """)
            self.fail("sources with nothing vouching for them were accepted!")
        except ValueError as e:
            # One run says all of them rather than one per attempt.
            self.assertIn("2 sources", str(e))
            self.assertIn("one_1.0-1.dsc", str(e))
            self.assertIn("two_1.0-1.dsc", str(e))

    def test_off_by_default(self):
        self.parsed("""
                packages:
                    - source: https://example.com/foo_1.0-1.dsc
        """, require=False)

# A builder image that unpacks something when it is asked to fetch, so a
# fetch can be exercised without a network or a container. Preparing the
# source package is part of that step, so it answers for dpkg-source and
# dpkg-parsechangelog as well.
class FakeFetch:
    def __init__(self, fails=False):
        self.fetches = 0
        self.fails = fails

    def exec(self, args, architecture=None, volumes=None, workdir=None,
             environment=None, check=True, tty=False):
        if args[0] == "dpkg-source":
            open(os.path.join(volumes[0][0], "busybox_1.37.0-1.dsc"), "w").close()
            return 0
        self.fetches += 1
        if self.fails:
            raise RuntimeError("the server said no")
        source = os.path.join(volumes[0][0], "busybox-1.37.0", "debian")
        os.makedirs(source, exist_ok=True)
        with open(os.path.join(source, "changelog"), "w") as f:
            f.write("busybox (1:1.37.0-6) unstable; urgency=medium\n")
        return 0

    def output(self, args, architecture=None, volumes=None, workdir=None,
               environment=None):
        return b"1700000000\n"

class FetchedSourcesOutliveTheirStep(avocado.Test):
    def builder(self, image):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        return Builder(distro, {"keep": False}, image)

    def package(self):
        return parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]

    def test(self):
        image = FakeFetch()
        builder = self.builder(image)
        package = self.package()

        workdir, dsc, epoch = builder._fetched(package)
        try:
            # What the fetch left behind belongs to the package, so the
            # step that builds it can be a different step.
            self.assertIn(package.name, builder._sources)
            self.assertEqual(builder._sources[package.name],
                             (workdir, dsc, epoch))
            self.assertTrue(os.path.isfile(os.path.join(workdir, dsc)))
            self.assertEqual(image.fetches, 1)
        finally:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    def test_a_failed_fetch_leaves_nothing_behind(self):
        import tempfile
        from seine import packages as module

        made = []
        original = module.tempfile.mkdtemp

        def watched(*args, **kwargs):
            path = original(*args, **kwargs)
            made.append(path)
            return path

        module.tempfile.mkdtemp = watched
        try:
            builder = self.builder(FakeFetch(fails=True))
            try:
                builder._fetched(self.package())
                self.fail("a fetch that failed was reported as done!")
            except RuntimeError:
                pass
        finally:
            module.tempfile.mkdtemp = original

        # No half-fetched directory, and nothing for a build to find.
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]),
                         "%s was left behind" % made[0])
        self.assertEqual(builder._sources, {})

# Two packages naming the same source share the one fetch it costs,
# instead of each paying for it -- the case a multi-group build's union
# does not itself collapse, since the two are genuinely different
# packages (different names, possibly different config) that merely
# happen to start from the same bytes.
class TwoPackagesSharingASourceFetchItOnce(avocado.Test):
    def builder(self, image):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        return Builder(distro, {"keep": False}, image)

    def package(self, name):
        return parse("""
                packages:
                    - source: apt://busybox
                      name: %s
        """ % name).image.packages[0]

    def test_the_keys_match(self):
        builder = self.builder(FakeFetch())
        self.assertEqual(builder._fetch_key(self.package("one")),
                         builder._fetch_key(self.package("two")))

    # A kernel tree is full of symlinks; dereferencing one into a plain
    # file is a change dpkg-source refuses to represent as a patch. Caught
    # for real building two kernels this way, see the 'full' test in
    # tests/spec/multiconfig.py.
    def test_symlinks_are_copied_as_symlinks(self):
        builder = self.builder(FakeFetch())
        source = os.path.join(self.workdir, "source")
        os.makedirs(os.path.join(source, "sub"))
        with open(os.path.join(source, "sub", "real.h"), "w") as f:
            f.write("real\n")
        os.symlink("sub/real.h", os.path.join(source, "top-level-link.h"))
        os.symlink("real.h", os.path.join(source, "sub", "nested-link.h"))

        dest = os.path.join(self.workdir, "dest")
        builder._copy_into(source, dest)

        top = os.path.join(dest, "top-level-link.h")
        nested = os.path.join(dest, "sub", "nested-link.h")
        self.assertTrue(os.path.islink(top), "a top-level symlink was dereferenced")
        self.assertEqual(os.readlink(top), "sub/real.h")
        self.assertTrue(os.path.islink(nested),
                        "a symlink inside a copied directory was dereferenced")
        self.assertEqual(os.readlink(nested), "real.h")

    def test_a_different_source_is_a_different_key(self):
        builder = self.builder(FakeFetch())
        one = self.package("one")
        two = parse("""
                packages:
                    - source: apt://vim
        """).image.packages[0]
        self.assertNotEqual(builder._fetch_key(one), builder._fetch_key(two))

    def test(self):
        # As tasks() would: _fetch() once (the graph only ever builds one
        # fetch task per key), then a prepare:<name> task per package.
        image = FakeFetch()
        builder = self.builder(image)
        one, two = self.package("one"), self.package("two")
        key = builder._fetch_key(one)
        builder._shared_wanted[key] = 2

        builder._fetch(one)
        self.assertEqual(image.fetches, 1)

        one_dir, one_dsc, _ = builder._prepare_source(one, key, None)
        two_dir, two_dsc, _ = builder._prepare_source(two, key, None)
        try:
            # Still one fetch for the two of them.
            self.assertEqual(image.fetches, 1)
            # And each has its own, independent copy to mutate -- not
            # the same directory handed out twice.
            self.assertNotEqual(one_dir, two_dir)
            self.assertTrue(os.path.isfile(os.path.join(one_dir, one_dsc)))
            self.assertTrue(os.path.isfile(os.path.join(two_dir, two_dsc)))
            # And nothing left claiming to still be needed.
            self.assertEqual(builder._shared_fetches, {})
            self.assertNotIn(key, builder._shared_wanted)
        finally:
            shutil.rmtree(one_dir, ignore_errors=True)
            shutil.rmtree(two_dir, ignore_errors=True)

    def test_works_for_a_single_consumer_too(self):
        # The degenerate case, and the common one: nothing else asks for
        # this key, but the split still works the same way -- one fetch
        # task, one prepare task, cleaned up once it has taken its copy.
        image = FakeFetch()
        builder = self.builder(image)
        one = self.package("one")
        key = builder._fetch_key(one)
        builder._shared_wanted[key] = 1

        builder._fetch(one)
        workdir, dsc, _ = builder._prepare_source(one, key, None)
        try:
            self.assertEqual(image.fetches, 1)
            self.assertTrue(os.path.isfile(os.path.join(workdir, dsc)))
            self.assertEqual(builder._shared_fetches, {})
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # _taken_shared() sits in _prepare_source()'s finally -- checked here
    # that it actually fires and frees the canonical copy even when
    # _prepared() itself is what fails, not the fetch.
    def test_a_failed_prepare_still_frees_a_fetch_nothing_else_needs(self):
        class FailingPrepare(FakeFetch):
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                if args[0] == "dpkg-source":
                    raise RuntimeError("dpkg-source blew up")
                return super().exec(args, architecture, volumes, workdir,
                                    environment, check)

        builder = self.builder(FailingPrepare())
        one = self.package("one")
        key = builder._fetch_key(one)
        builder._shared_wanted[key] = 1

        builder._fetch(one)
        canonical = builder._shared_fetches[key]
        try:
            builder._prepare_source(one, key, None)
            self.fail("a failing prepare was reported as done!")
        except RuntimeError:
            pass

        # Freed even though nothing built successfully -- nothing else
        # was ever going to ask for it.
        self.assertNotIn(key, builder._shared_fetches)
        self.assertFalse(os.path.isdir(canonical), "%s was left behind" % canonical)

# _upstream_key() is the graft-tree counterpart of _fetch_key() above,
# sharing the same fetch/prepare split already proven there -- what is
# its own is only the key, so that is what this checks.
class UpstreamKeyMatchesTheGraftedTree(avocado.Test):
    def builder(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def package(self, upstream=None):
        extra = ""
        if upstream:
            extra = ("                      extends:\n"
                     "                          kernel:\n"
                     "                              upstream: %s\n" % upstream)
        return parse("""
                packages:
                    - source: apt://linux
%s
        """ % extra).image.packages[0]

    def test_none_without_an_upstream(self):
        self.assertIsNone(self.builder()._upstream_key(self.package()))

    def test_matches_the_same_upstream(self):
        uri = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.tar.xz"
        builder = self.builder()
        one, two = self.package(uri), self.package(uri)
        self.assertIsNotNone(builder._upstream_key(one))
        self.assertEqual(builder._upstream_key(one), builder._upstream_key(two))

    def test_a_different_upstream_is_a_different_key(self):
        builder = self.builder()
        one = self.package(
            "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.tar.xz")
        two = self.package(
            "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.13.tar.xz")
        self.assertNotEqual(builder._upstream_key(one), builder._upstream_key(two))

# _prepare_source() looks for what _fetch_upstream() left under
# kernel.UPSTREAM inside whatever _shared_fetches[upstream_key] names --
# a layout only a real, expensive grafted-kernel build would otherwise
# exercise. Checked directly, with a fake standing in for the tar/git
# clone kernel._upstream_args() would actually run.
class FetchUpstreamPopulatesTheSharedEntry(avocado.Test):
    def test(self):
        from seine.packages import Builder
        from seine.kernel import UPSTREAM

        class FakeUpstream:
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                os.makedirs(os.path.join(volumes[0][0], "linux-6.12"),
                           exist_ok=True)
                return 0

        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, FakeUpstream())
        package = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.12.tar.xz
        """).image.packages[0]

        builder._fetch_upstream(package)

        key = builder._upstream_key(package)
        canonical = builder._shared_fetches[key]
        self.assertTrue(
            os.path.isdir(os.path.join(canonical, UPSTREAM, "linux-6.12")),
            "the fetched tree was not where _prepare_source() looks for it")

class AKernelsTreeIsFetchedWithItsSource(avocado.Test):
    def test(self):
        from seine.packages import Builder, WORKDIR
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}

        fetched = []

        class Image:
            def exec(self, args, architecture=None, volumes=None,
                     workdir=None, environment=None, check=True):
                fetched.append(" ".join(args))
                os.makedirs(os.path.join(volumes[0][0], "linux-6.18.43"),
                            exist_ok=True)
                return 0

        package = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz
        """).image.packages[0]

        builder = Builder(distro, {"keep": False}, Image())
        # What is under test is the fetching; turning what came down into
        # a source package is the next step's business.
        builder._prepared = lambda *arguments: ("linux_6.18.43-1.dsc", 0)
        workdir, dsc, epoch = builder._fetched(package)
        try:
            # The kernel's own tree comes down with the packaging, in the
            # step that waits on the network -- not later, in the one that
            # waits on the machine.
            self.assertEqual(len(fetched), 2)
            self.assertIn("apt-get source linux", fetched[0])
            self.assertIn("linux-6.18.43.tar.xz", fetched[1])
            self.assertTrue(os.path.isdir(os.path.join(workdir, ".upstream")))
        finally:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

class TheRepositoryIsNotRehashedEveryTime(avocado.Test):
    def test(self):
        from seine.packages import Builder

        commands = []

        class Image:
            def exec(self, args, architecture=None, volumes=None,
                     workdir=None, environment=None, check=True):
                commands.append(" ".join(args))
                return 0

        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        Builder(distro, {}, Image()).index()

        command = commands[0]
        # The index is rewritten after every package that builds, so what
        # it costs has to be what arrived rather than what is there: a
        # kernel's debug package is larger than most images, and hashing
        # it once per package is time spent on nothing.
        self.assertIn("apt-ftparchive", command)
        self.assertIn("--db", command)
        self.assertNotIn("dpkg-scanpackages", command)
        # And what apt reads is still what it always read.
        self.assertIn("> Packages", command)
        self.assertIn("Packages.gz", command)

class Publishes(avocado.Test):
    """
    :avocado: disable
    """

    # Publishing a package records it in the cache index, and the index
    # belongs to whoever is running the tests: point it at the test's own
    # directory rather than writing in their cache.
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

class PublishingRecordsWhatWasBuilt(Publishes):
    def test(self):
        from seine.packages import Builder

        commands = []

        class Image:
            def exec(self, args, architecture=None, volumes=None,
                     workdir=None, environment=None, check=True):
                commands.append(" ".join(args))
                return 0

        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        builder = Builder(distro, {}, Image())
        builder.repository = lambda: self.workdir

        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        stamp = os.path.join(self.workdir, "busybox_stamp")
        output = os.path.join(self.workdir, "output")
        os.makedirs(output, exist_ok=True)
        open(os.path.join(output, "busybox_1.37.0-6_amd64.deb"), "w").close()
        builder._built[(package.name, "amd64")] = (stamp, output)
        builder._deploy(package, ["amd64"])

        # The stamp is written and the index rewritten, in that order and
        # only once the .deb has been moved in to be recorded.
        with open(stamp) as f:
            self.assertEqual(f.read().strip(), "busybox_1.37.0-6_amd64.deb")
        self.assertTrue(os.path.isfile(
            os.path.join(self.workdir, "busybox_1.37.0-6_amd64.deb")))
        self.assertEqual(len(commands), 1)
        self.assertIn("apt-ftparchive", commands[0])
        # And nothing is left waiting to be published twice.
        self.assertEqual(builder._built, {})

class PublishingABuildThatDidNotHappenDoesNothing(Publishes):
    def test(self):
        from seine.packages import Builder

        class Image:
            def exec(self, *args, **kwargs):
                raise AssertionError("nothing to publish, nothing to run")

        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        Builder(distro, {}, Image())._deploy(package)

# sbuild finds its chroot by looking for a file whose name looks like
# '<dist>-<arch>.t<something>' in its cache directory, and keeps the last
# one readdir hands it. So the digest seine records beside a chroot must not
# look like that, or every build is a coin toss between unpacking the chroot
# and unpacking a 17-byte text file.
class TheChrootDigestCannotBeMistakenForAChroot(avocado.Test):
    def test(self):
        import re
        from seine.sbuild import SbuildChroot

        os.environ["SEINE_CACHE_DIR"] = self.workdir
        try:
            chroot = SbuildChroot(
                {"source": "debian", "release": "bookworm"}, {}, "amd64")
            # ChrootInfoUnshare.pm's own rule, as it reads there.
            looks_like_a_chroot = re.compile(
                r"^[^-]+-[^-]+(-[^-]+)?(-sbuild)?\.t.+$")
            self.assertIsNotNone(
                looks_like_a_chroot.match(os.path.basename(chroot.path)),
                "sbuild would not find the chroot at %s" % chroot.path)
            # Every name seine writes in there, the lock included: bookworm's
            # sbuild takes the first match without skipping empty files, so
            # a lock file called '<tarball>.lock' is a chroot as far as it is
            # concerned.
            for path in [chroot.inputs, "%s.lock" % chroot.inputs]:
                self.assertIsNone(
                    looks_like_a_chroot.match(os.path.basename(path)),
                    "sbuild would take %s for a chroot" % path)
            # The two live side by side, which is the point of the name.
            self.assertEqual(os.path.dirname(chroot.inputs),
                             os.path.dirname(chroot.path))
        finally:
            os.environ.pop("SEINE_CACHE_DIR", None)

class ScopeSaysWhoARebuildIsFor(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://busybox
                      scope: host
                    - source: apt://coreutils
                      scope: [host, target]
                    - source: apt://bash
        """)
        scopes = {p.name: p.scope for p in build.image.packages}
        # A single role is a list of one, so nothing downstream has to ask
        # which of the two spellings it was given.
        self.assertEqual(scopes["busybox"], ["host"])
        self.assertEqual(scopes["coreutils"], ["host", "target"])
        # A rebuild is for the image unless it says otherwise.
        self.assertEqual(scopes["bash"], ["target"])

    def test_an_unknown_scope_is_rejected(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      scope: builder
            """)
            self.fail("parsing succeeded for an unknown 'scope'!")
        except ValueError as e:
            self.assertIn("scope", str(e))

    # A flavour is a name within an architecture, so one cannot be right
    # for two of them.
    def test_a_kernel_cannot_be_built_for_both(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      scope: [host, target]
                      extends:
                          kernel:
                              flavour: arm64
            """)
            self.fail("parsing succeeded for a kernel built for both!")
        except ValueError as e:
            self.assertIn("scope", str(e))
            self.assertIn("flavour", str(e))

class ScopeDecidesWhichArchitecturesAreBuilt(avocado.Test):
    def builder(self, architecture):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture, "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        return Builder(distro, {}, None)

    def packages(self):
        return {p.name: p for p in parse("""
                packages:
                    - source: apt://target-only
                    - source: apt://host-only
                      scope: host
                    - source: apt://both-of-them
                      scope: [host, target]
        """).image.packages}

    def test(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        builder = self.builder(other)
        packages = self.packages()

        self.assertEqual(builder.architectures(packages["target-only"]), [other])
        self.assertEqual(builder.architectures(packages["host-only"]), [HOST_ARCH])
        self.assertEqual(builder.architectures(packages["both-of-them"]),
                         sorted([HOST_ARCH, other]))

    def test_a_host_package_is_never_cross_built(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        builder = self.builder(other)
        package = self.packages()["both-of-them"]

        # The machine running the compiler is the machine the host build
        # is for, so there is nothing to cross to.
        self.assertEqual(builder.cross(package, HOST_ARCH), False)
        self.assertEqual(builder.cross(package, other), True)

    def test_each_architecture_has_a_repository_and_a_stamp_of_its_own(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        builder = self.builder(other)
        package = self.packages()["both-of-them"]

        stamps = {a: s for p, a, s in builder.stamps([package])}
        self.assertEqual(sorted(stamps), sorted([HOST_ARCH, other]))
        self.assertNotEqual(os.path.basename(stamps[HOST_ARCH]),
                            os.path.basename(stamps[other]))
        self.assertIn(HOST_ARCH, stamps[HOST_ARCH])
        self.assertIn(other, stamps[other])

# A package built for the host is linked against what its dependencies
# installed, so those have to be built for the host too. Saying it twice
# is bookkeeping the specification should not have to do.
class ScopePropagatesToDependencies(avocado.Test):
    def scopes(self, spec):
        return {p.name: p.scope for p in parse(spec).image.packages}

    def test(self):
        scopes = self.scopes("""
                packages:
                    - source: apt://tool
                      scope: host
                      after:
                          - library
                    - source: apt://library
        """)
        # The library keeps the image it was already going to be in, and
        # gains the role of what is built on it.
        self.assertEqual(scopes["library"], ["host", "target"])
        self.assertEqual(scopes["tool"], ["host"])

    # One pass carries a role the length of a chain.
    def test_it_reaches_all_the_way_down(self):
        scopes = self.scopes("""
                packages:
                    - source: apt://tool
                      scope: host
                      after:
                          - middle
                    - source: apt://middle
                      after:
                          - bottom
                    - source: apt://bottom
        """)
        for name in ["middle", "bottom"]:
            self.assertIn("host", scopes[name], "%s was left behind" % name)

    # An explicit scope is an answer, not a default to widen.
    def test_an_explicit_scope_is_not_widened(self):
        try:
            self.scopes("""
                packages:
                    - source: apt://tool
                      scope: host
                      after:
                          - library
                    - source: apt://library
                      scope: target
            """)
            self.fail("a host package was built against a target-only one!")
        except ValueError as e:
            # Both entries are named: which of the two is wrong is not
            # seine's to decide.
            self.assertIn("apt://library", str(e))
            self.assertIn("apt://tool", str(e))

    # Nothing to carry when the two already agree.
    def test_matching_scopes_are_left_alone(self):
        scopes = self.scopes("""
                packages:
                    - source: apt://tool
                      scope: host
                      after:
                          - library
                    - source: apt://library
                      scope: [host, target]
        """)
        self.assertEqual(scopes["library"], ["host", "target"])

# An 'Architecture: all' binary is one package under one filename, and one
# repository holds every architecture -- so exactly one of a package's
# builds may produce it.
class ArchitectureAllIsBuiltOnce(avocado.Test):
    def builder(self, architecture):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture, "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        return Builder(distro, {}, None)

    def package(self, cross=""):
        return parse("""
                packages:
                    - source: apt://busybox
                      scope: [host, target]
%s
        """ % cross).image.packages[0]

    def test(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        builder = self.builder(other)
        package = self.package()

        # The cross build cannot make them -- sbuild hands it '-B', and an
        # arch-indep binary is commonly made by running what was just
        # built -- so the native build is the one that does.
        self.assertEqual(builder.indep_architecture(package), HOST_ARCH)

    # 'cross: false' leaves two native builds, and exactly one of them may
    # still have the job.
    def test_two_native_builds_still_nominate_one(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        builder = self.builder(other)
        package = self.package("                      cross: false\n")

        self.assertEqual(builder.cross(package, other), False)
        self.assertEqual(builder.indep_architecture(package), HOST_ARCH)

    # Every build a cross build -- a specification building for one board
    # on somebody's laptop -- and the cross build is asked for them
    # anyway. The alternative is publishing the .debs without the arch-all
    # packages beside them, and an image installing one would take the
    # distribution's copy of a package it asked to have rebuilt.
    def test_a_cross_build_is_asked_when_it_is_the_only_one(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        builder = self.builder(other)
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        self.assertEqual(builder.cross(package, other), True)
        self.assertEqual(builder.indep_architecture(package), other)

    # And it is told so: sbuild would otherwise decide for itself, and
    # what it decides for a cross build is to leave them out.
    def test_the_only_build_is_told_to_make_them(self):
        from seine.packages import Builder
        from seine.utils import HOST_ARCH

        class Image:
            def __init__(self):
                self.calls = []

            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                self.calls.append(" ".join(args))
                return 0

        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        distro = {"source": "debian", "release": "trixie",
                  "architecture": other, "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        image = Image()
        builder = Builder(distro, {}, image)
        builder.repository = lambda: self.workdir
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]

        builder.build(package, self.workdir, "busybox_1.dsc", "0", other,
                      self.workdir)
        self.assertIn("--arch-all", image.calls[-1].split())
        self.assertNotIn("--no-arch-all", image.calls[-1].split())

    # And it is said to sbuild either way rather than left to its default,
    # which builds them whenever the build is a native one.
    def test_sbuild_is_told_which_build_has_the_job(self):
        from seine.packages import Builder
        from seine.utils import HOST_ARCH

        class Image:
            def __init__(self):
                self.calls = []

            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                self.calls.append(" ".join(args))
                return 0

        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        distro = {"source": "debian", "release": "trixie",
                  "architecture": other, "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        image = Image()
        builder = Builder(distro, {}, image)
        builder.repository = lambda: self.workdir
        package = self.package()

        asked = {}
        for architecture in [HOST_ARCH, other]:
            builder.build(package, self.workdir, "busybox_1.dsc", "0",
                          architecture, self.workdir)
            asked[architecture] = [a for a in image.calls[-1].split()
                                   if a.endswith("arch-all")]
        self.assertEqual(asked[HOST_ARCH], ["--arch-all"])
        self.assertEqual(asked[other], ["--no-arch-all"])

# What a package's build is allowed to install, said in apt's own language
# and put in front of that build alone.
class AptPreferencesArePutInFrontOfOneBuild(avocado.Test):
    PINNED = """Package: linux-libc-dev
Pin: release n=bookworm
Pin-Priority: 1001
"""

    def package(self, preferences=True):
        spec = """
                packages:
                    - source: apt://busybox
"""
        if preferences:
            spec += "                      apt-preferences: |\n"
            spec += "".join("                          %s\n" % line
                            for line in self.PINNED.strip().split("\n"))
        return parse(spec).image.packages[0]

    def test(self):
        # Verbatim: what can be said in that file is apt's to define.
        self.assertEqual(self.package().preferences_for("bookworm").strip(),
                         self.PINNED.strip())
        # One text with no release named is for every release.
        self.assertEqual(self.package().preferences_for("trixie").strip(),
                         self.PINNED.strip())
        self.assertEqual(
            self.package(preferences=False).preferences_for("bookworm"), None)

    def test_it_shall_be_a_string_or_a_mapping(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences:
                          - Package: linux-libc-dev
            """)
            self.fail("a list was accepted as 'apt-preferences'!")
        except ValueError as e:
            self.assertIn("apt-preferences", str(e))

    # What a pin names is a suite, so the same package commonly needs a
    # different one per release -- or one for a single release and none
    # for the others.
    def test_it_may_be_keyed_by_release(self):
        package = parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences:
                          bookworm: |
                              Package: linux-libc-dev
                              Pin: release n=bookworm
                              Pin-Priority: 1001
                          trixie: |
                              Package: linux-libc-dev
                              Pin: release n=trixie
                              Pin-Priority: 1001
        """).image.packages[0]
        self.assertIn("n=bookworm", package.preferences_for("bookworm"))
        self.assertIn("n=trixie", package.preferences_for("trixie"))
        # A release it does not name gets none.
        self.assertEqual(package.preferences_for("sid"), None)

    def test_a_release_may_be_pinned_on_its_own(self):
        package = parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences:
                          bookworm: |
                              Package: linux-libc-dev
                              Pin: release n=bookworm
                              Pin-Priority: 1001
        """).image.packages[0]
        self.assertIsNotNone(package.preferences_for("bookworm"))
        self.assertEqual(package.preferences_for("trixie"), None)

    def test_an_empty_one_in_a_mapping_is_rejected(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences:
                          bookworm: "  "
            """)
            self.fail("an empty per-release 'apt-preferences' was accepted!")
        except ValueError as e:
            self.assertIn("bookworm", str(e))

    def test_an_empty_one_is_rejected(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences: "   "
            """)
            self.fail("an empty 'apt-preferences' was accepted!")
        except ValueError as e:
            self.assertIn("apt-preferences", str(e))

    # It reaches the chroot the package is built in, and says exactly what
    # the specification wrote.
    def test_it_reaches_the_chroot(self):
        from seine.packages import Builder, PACKAGE_PREFERENCES

        class Image:
            def __init__(self):
                self.calls = []

            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                self.calls.append(args)
                return 0

        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        image = Image()
        builder = Builder(distro, {}, image)
        builder.repository = lambda: self.workdir

        builder.build(self.package(), self.workdir, "busybox_1.dsc", "0",
                      "amd64", self.workdir)
        command = " ".join(image.calls[-1])
        self.assertIn(PACKAGE_PREFERENCES, command)
        self.assertIn("Pin: release n=bookworm", command)

        # And a package that asked for none is given none, rather than an
        # empty file apt would still read.
        builder.build(self.package(preferences=False), self.workdir,
                      "busybox_1.dsc", "0", "amd64", self.workdir)
        self.assertNotIn(PACKAGE_PREFERENCES, " ".join(image.calls[-1]))

    # A changed pin is a changed build, so it has to be a rebuild.
    def test_it_decides_whether_a_rebuild_is_needed(self):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        builder = Builder(distro, {}, None)

        digests = set()
        for package in [self.package(), self.package(preferences=False)]:
            _, _, stamp = builder.stamps([package])[0]
            digests.add(os.path.basename(stamp))
        self.assertEqual(len(digests), 2,
                         "pinning a package's build did not ask for a rebuild")

    # Another release's pin has no bearing on what comes out here, so it
    # is not a reason to build again.
    def test_another_releases_pin_is_not_a_rebuild(self):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        builder = Builder(distro, {}, None)

        digests = set()
        for trixie in ["Pin-Priority: 1001", "Pin-Priority: 990"]:
            package = parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences:
                          bookworm: |
                              Package: linux-libc-dev
                              Pin: release n=bookworm
                              Pin-Priority: 1001
                          trixie: |
                              Package: linux-libc-dev
                              Pin: release n=trixie
                              %s
            """ % trixie).image.packages[0]
            _, _, stamp = builder.stamps([package])[0]
            digests.add(os.path.basename(stamp))
        self.assertEqual(len(digests), 1,
                         "changing trixie's pin rebuilt a bookworm package")

# sbuild expands percent escapes in the commands it is handed before any
# shell sees them, so a command meaning a literal percent has to double
# it. Found by the preferences file arriving as the expansion of '%s',
# with apt refusing to read what landed there.
class PercentSurvivesSbuild(avocado.Test):
    def test(self):
        from seine.packages import sbuild_command
        self.assertEqual(sbuild_command("printf '%s' x"), "printf '%%s' x")
        # Whatever the specification wrote, too: a percent in the text is
        # as much sbuild's to eat as one in the command around it.
        self.assertEqual(sbuild_command("Pin: version 6.1%"),
                         "Pin: version 6.1%%")

    # What actually reaches sbuild, rather than the helper on its own.
    def test_the_command_sbuild_is_given(self):
        from seine.packages import Builder

        class Image:
            def __init__(self):
                self.calls = []

            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                self.calls.append(args)
                return 0

        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        image = Image()
        builder = Builder(distro, {}, image)
        builder.repository = lambda: self.workdir

        package = parse("""
                packages:
                    - source: apt://busybox
                      apt-preferences: |
                          Package: linux-libc-dev
                          Pin: origin ""
                          Pin-Priority: -1
        """).image.packages[0]
        builder.build(package, self.workdir, "busybox_1.dsc", "0", "amd64",
                      self.workdir)
        # The command travels inside a shell quoting of its own, so what
        # is checked is the percents themselves: every one of them is
        # doubled, since a single one is sbuild's to expand.
        import re
        command = " ".join(image.calls[-1])
        self.assertIn("%%s", command)
        self.assertEqual(re.sub("%%", "", command).count("%"), 0,
                         "an unescaped percent would be eaten by sbuild")

# A build that fails leaves what it wrote where someone can read it: the
# build log sbuild writes beside its output is the only account of why it
# failed, and it is written into that directory rather than the
# repository.
class AFailedBuildKeepsItsLog(avocado.Test):
    def test(self):
        from seine import packages as module
        from seine.packages import Builder
        from seine.utils import ContainerEngine

        class Image:
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                # Only the build itself fails, and only after writing what
                # sbuild would have written -- which is what has to
                # survive.
                output = [host for host, container in volumes or []
                          if container == "/output"]
                if len(output) == 0:
                    return 0
                open(os.path.join(output[0], "busybox.build"), "w").close()
                raise RuntimeError("the build died")

        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        builder = Builder(distro, {}, Image())
        builder.repository = lambda: self.workdir
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        builder._sources[package.name] = (self.workdir, "busybox_1.dsc", 0)
        builder._holding[package.name] = 1

        # Unpacking a buildd chroot is not what is under test, and doing
        # it for real would write into the machine's own cache.
        class Chroot:
            def __init__(self, *arguments):
                pass

            def create(self, image):
                return self

        # What was already in the scratch space is somebody else's, and on
        # a real machine there is usually something.
        before = set(os.listdir(ContainerEngine.scratch()))
        original = module.SbuildChroot
        module.SbuildChroot = Chroot
        try:
            builder._rebuild(package, "amd64", os.path.join(self.workdir, "stamp"))
            self.fail("a failed build was reported as done!")
        except RuntimeError:
            pass
        finally:
            module.SbuildChroot = original

        kept = [name for name in os.listdir(ContainerEngine.scratch())
                if name.startswith("built-") and name not in before]
        self.assertNotEqual(kept, [], "the failed build's log was thrown away")
        for name in kept:
            where = os.path.join(ContainerEngine.scratch(), name)
            self.assertIn("busybox.build", os.listdir(where))
            shutil.rmtree(where, ignore_errors=True)

        # And nothing was published: a stamp for a build that failed would
        # skip it next time.
        self.assertEqual(builder._built, {})

# A second build replaces the first, undated symlink included -- the
# ordinary case, not the exception. Checked across filesystems, since
# SEINE_BUILD_DIR puts the scratch space and the repository on separate
# drives, where a plain rename can't replace what shutil falls back to
# recreating instead.
class ASecondBuildReplacesTheFirst(Publishes):
    def test(self):
        from seine.packages import Builder

        class Image:
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                return 0

        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        builder = Builder(distro, {}, Image())
        repository = os.path.join(self.workdir, "repository")
        os.makedirs(os.path.join(repository, ".stamps"), exist_ok=True)
        builder.repository = lambda: repository

        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]

        for dated in ["busybox_1-2026-01-01.build", "busybox_1-2026-06-01.build"]:
            output = tempfile.mkdtemp(dir=self.workdir)
            open(os.path.join(output, "busybox_1_amd64.deb"), "w").close()
            open(os.path.join(output, dated), "w").close()
            os.symlink(dated, os.path.join(output, "busybox_1.build"))
            builder._built[(package.name, "amd64")] = (
                os.path.join(repository, ".stamps", "busybox_amd64_%s" % dated),
                output)

            renamed = os.rename

            def elsewhere(source, destination):
                raise OSError(errno.EXDEV, "Invalid cross-device link")

            os.rename = elsewhere
            try:
                builder._deploy(package, ["amd64"])
            finally:
                os.rename = renamed

        # The second build's log is what the undated name points at, and
        # the first one is gone with the stamp that named it.
        where = os.path.join(repository, "busybox_1.build")
        self.assertEqual(os.readlink(where), "busybox_1-2026-06-01.build")
        self.assertFalse(os.path.isfile(
            os.path.join(repository, "busybox_1-2026-01-01.build")),
            "the superseded build log was left behind")

# The source package a rebuild was made from is published with the .debs,
# once per package however many architectures were built from it.
class SourcePackagesReachTheRepository(avocado.Test):
    DSC = """Format: 3.0 (quilt)
Source: busybox
Version: 1:1.35.0-4+mod1
Files:
 0123456789abcdef0123456789abcdef 1024 busybox_1.35.0.orig.tar.bz2
 fedcba9876543210fedcba9876543210 2048 busybox_1.35.0-4+mod1.debian.tar.xz
Checksums-Sha256:
 aaaa 1024 busybox_1.35.0.orig.tar.bz2
 bbbb 2048 busybox_1.35.0-4+mod1.debian.tar.xz
"""

    def builder(self, image=None):
        from seine.packages import Builder

        class Quiet:
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                return 0

        distro = {"source": "debian", "release": "trixie",
                  "architecture": "arm64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "trixie"}]}
        builder = Builder(distro, {}, image or Quiet())
        self.repository = os.path.join(self.workdir, "repository")
        os.makedirs(os.path.join(self.repository, ".stamps"), exist_ok=True)
        builder.repository = lambda: self.repository
        return builder

    def source(self):
        where = os.path.join(self.workdir, "source")
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, "busybox_1.35.0-4+mod1.dsc"), "w") as f:
            f.write(self.DSC)
        for name in ["busybox_1.35.0.orig.tar.bz2",
                     "busybox_1.35.0-4+mod1.debian.tar.xz",
                     # What the source was fetched as, superseded by what
                     # was just built and no part of it.
                     "busybox_1.35.0-4.debian.tar.xz"]:
            open(os.path.join(where, name), "w").close()
        return where, "busybox_1.35.0-4+mod1.dsc"

    # What the .dsc names, and not what happens to lie beside it.
    def test_the_files_are_the_ones_the_dsc_names(self):
        builder = self.builder()
        where, dsc = self.source()
        self.assertEqual(sorted(builder.source_files(where, dsc)),
                         ["busybox_1.35.0-4+mod1.debian.tar.xz",
                          "busybox_1.35.0-4+mod1.dsc",
                          "busybox_1.35.0.orig.tar.bz2"])

    def test_they_are_published_with_the_debs(self):
        builder = self.builder()
        where, dsc = self.source()
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        builder._stage_source(package, where, dsc)

        output = tempfile.mkdtemp(dir=self.workdir)
        open(os.path.join(output, "busybox_1_arm64.deb"), "w").close()
        stamp = os.path.join(self.repository, ".stamps", "busybox_arm64_abcd")
        builder._built[(package.name, "arm64")] = (stamp, output)
        builder._deploy(package, ["arm64"])

        held = sorted(os.listdir(self.repository))
        for name in ["busybox_1.35.0-4+mod1.dsc",
                     "busybox_1.35.0.orig.tar.bz2",
                     "busybox_1.35.0-4+mod1.debian.tar.xz"]:
            self.assertIn(name, held)
        # The tarball it was fetched as is not part of what was built.
        self.assertNotIn("busybox_1.35.0-4.debian.tar.xz", held)

        # And the stamp names them, or nothing would ever retire them.
        with open(stamp) as f:
            recorded = f.read().split()
        self.assertIn("busybox_1.35.0-4+mod1.dsc", recorded)
        self.assertIn("busybox_1_arm64.deb", recorded)

    # One publication for a package built for two architectures: a source
    # package is not built for one, and both builds were handed this one.
    def test_published_once_for_two_architectures(self):
        from seine.utils import HOST_ARCH
        moved = []

        builder = self.builder()
        where, dsc = self.source()
        package = parse("""
                packages:
                    - source: apt://busybox
                      scope: [host, target]
        """).image.packages[0]
        builder._stage_source(package, where, dsc)

        architectures = builder.architectures(package)
        self.assertEqual(len(architectures), 2)
        stamps = {}
        for architecture in architectures:
            output = tempfile.mkdtemp(dir=self.workdir)
            open(os.path.join(output, "busybox_1_%s.deb" % architecture),
                 "w").close()
            stamps[architecture] = os.path.join(
                self.repository, ".stamps", "busybox_%s_abcd" % architecture)
            builder._built[(package.name, architecture)] = (
                stamps[architecture], output)
        builder._deploy(package, architectures)

        # One copy of each file, and every build's stamp names them, so
        # they go when the last of those builds does rather than when the
        # first is forgotten.
        held = sorted(os.listdir(self.repository))
        self.assertEqual(held.count("busybox_1.35.0-4+mod1.dsc"), 1)
        for architecture in architectures:
            with open(stamps[architecture]) as f:
                self.assertIn("busybox_1.35.0-4+mod1.dsc", f.read())

    # The index describes them, or a repository holding source packages is
    # not holding them in any way anything can find.
    def test_the_index_describes_them(self):
        commands = []

        class Image:
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True, tty=False):
                commands.append(" ".join(args))
                return 0

        self.builder(Image()).index()
        self.assertEqual(len(commands), 1)
        self.assertIn("apt-ftparchive sources .", commands[0])
        self.assertIn("Sources.gz", commands[0])

    # apt-ftparchive keeps what it has read, so that indexing after every
    # build does not re-hash every .deb again. For a file replaced under
    # its own name it goes on describing the one before, and what comes
    # out is an index advertising the size and hash of a package that is
    # not there -- which apt fetches and refuses as a hash mismatch.
    def test_the_index_is_kept_when_nothing_was_replaced(self):
        from seine.packages import INDEX_CACHE
        builder = self.builder()
        cache = os.path.join(self.repository, INDEX_CACHE)
        open(cache, "w").close()
        builder.index()
        self.assertTrue(os.path.isfile(cache),
                        "the cache was thrown away for nothing")

    def test_a_replaced_package_is_indexed_from_the_files(self):
        from seine.packages import INDEX_CACHE
        builder = self.builder()
        cache = os.path.join(self.repository, INDEX_CACHE)
        open(cache, "w").close()
        builder.index(cached=False)
        self.assertFalse(os.path.isfile(cache),
                         "an index made from what it remembers describes the "
                         "package that was replaced")

    def test_publishing_over_a_package_says_so(self):
        from seine.packages import INDEX_CACHE
        builder = self.builder()
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        cache = os.path.join(self.repository, INDEX_CACHE)

        # Built once, then built again at the same version -- which is
        # every rebuild caused by something the digest counts and the
        # version does not: a patch, a profile, the packaging itself.
        for contents in ["first", "second and longer"]:
            open(cache, "w").close()
            output = tempfile.mkdtemp(dir=self.workdir)
            with open(os.path.join(output, "busybox_1_arm64.deb"), "w") as f:
                f.write(contents)
            stamp = os.path.join(self.repository, ".stamps",
                                 "busybox_arm64_%s" % contents[:5])
            builder._built[(package.name, "arm64")] = (stamp, output)
            builder._deploy(package, ["arm64"])
            published = os.path.join(self.repository, "busybox_1_arm64.deb")
            with open(published) as f:
                self.assertEqual(f.read(), contents)

        # The second one took the place of the first, so what
        # apt-ftparchive remembered about that name is wrong.
        self.assertFalse(os.path.isfile(cache),
                         "a package published over another left the index "
                         "describing the one before it")

# A key seine never sees signs what a build produced. gpg runs on the
# machine seine was started on and talks to the agent there, so these
# check what seine asks of it and what it does with the answer, with a
# key made for the test and thrown away with it.
class Signing(avocado.Test):
    def setUp(self):
        # Set before anything can cancel: tearDown runs after a cancelled
        # setUp, and a home directory it cannot ask about is one no agent
        # was ever started for.
        self.gnupg = None
        if shutil.which("gpg") is None:
            self.cancel("gpg is needed to sign anything")
        self.gnupg = os.path.join(self.workdir, "gnupg")
        os.makedirs(self.gnupg, mode=0o700, exist_ok=True)
        os.environ["GNUPGHOME"] = self.gnupg
        made = subprocess.run(
            ["gpg", "--batch", "--quiet", "--passphrase", "",
             "--quick-generate-key", "Seine Spec <spec@example.invalid>",
             "default", "default", "never"],
            capture_output=True)
        if made.returncode != 0:
            self.cancel("could not make a key to sign with: %s"
                        % made.stderr.decode(errors="replace"))

    # gpg starts an agent for a home directory the first time it is used,
    # and the agent outlives the test: avocado then removes the workdir
    # under it and the daemon goes on running against a directory that is
    # not there. A machine that runs this suite regularly gathered a
    # hundred of them, the oldest eighteen days old.
    def tearDown(self):
        if self.gnupg is not None:
            subprocess.run(["gpgconf", "--homedir", self.gnupg, "--kill", "all"],
                           capture_output=True)
        os.environ.pop("GNUPGHOME", None)

    def signer(self):
        from seine.signing import Signer
        return Signer("spec@example.invalid")

    # Whatever the key was named as, gpg's own answer for it: a name
    # matching two keys would otherwise sign with neither on purpose.
    def test_the_key_is_resolved(self):
        signer = self.signer()
        self.assertEqual(len(signer.fingerprint()), 40)
        # Named for the key, since it is kept beside other people's.
        self.assertEqual(signer.keyring(), "%s.gpg" % signer.fingerprint()[-8:])

    def test_a_key_that_is_not_there_says_so(self):
        from seine.signing import Signer
        try:
            Signer("nobody@example.invalid").fingerprint()
            self.fail("a key that does not exist was accepted!")
        except ValueError as e:
            self.assertIn("nobody@example.invalid", str(e))

    # A .dsc and a .changes carry the signature inside them, so what is
    # published is signed rather than accompanied by a signature.
    def test_a_file_is_signed_in_place(self):
        where = os.path.join(self.workdir, "busybox_1.dsc")
        with open(where, "w") as f:
            f.write("Format: 3.0 (quilt)\nSource: busybox\n")
        self.signer().clearsign(where)

        with open(where) as f:
            signed = f.read()
        self.assertIn("BEGIN PGP SIGNED MESSAGE", signed)
        self.assertIn("Source: busybox", signed)
        self.assertEqual(subprocess.run(["gpg", "--verify", where],
                                        capture_output=True).returncode, 0)

    # Both, because apt looks for both.
    def test_the_release_is_signed_both_ways(self):
        release = os.path.join(self.workdir, "Release")
        with open(release, "w") as f:
            f.write("Suite: seine\n")
        self.signer().sign_release(release)

        for name in ["InRelease", "Release.gpg"]:
            where = os.path.join(self.workdir, name)
            self.assertTrue(os.path.isfile(where), "no %s was written" % name)
        self.assertEqual(subprocess.run(
            ["gpg", "--verify", os.path.join(self.workdir, "InRelease")],
            capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(
            ["gpg", "--verify", os.path.join(self.workdir, "Release.gpg"), release],
            capture_output=True).returncode, 0)

    # What apt is told, which is to verify rather than to trust whatever
    # is there.
    def test_apt_is_pointed_at_the_key(self):
        from seine.packages import apt_configuration, KEYRINGS
        said = apt_configuration("/packages", keyring="ABCD1234.gpg")
        self.assertIn("signed-by=%s/ABCD1234.gpg" % KEYRINGS, said)
        self.assertNotIn("trusted=yes", said)
        # And the key is put there before anything reads from it.
        self.assertIn("install -D", said)
        self.assertLess(said.index("install -D"), said.index("signed-by"))

    # Unsigned, it is trusted because it was made here a moment ago.
    def test_without_a_key_nothing_changes(self):
        from seine.packages import apt_configuration
        said = apt_configuration("/packages")
        self.assertIn("trusted=yes", said)
        self.assertNotIn("signed-by", said)

    # A repository is signed when there is a signature, not when there is
    # a key lying in it: a cache carried here brings the public key and
    # leaves the private one behind.
    def test_a_key_alone_is_not_a_signed_repository(self):
        from seine import packages as module
        where = os.path.join(self.workdir, "repository")
        os.makedirs(where, exist_ok=True)
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        original = module.repository
        module.repository = lambda d: where
        try:
            open(os.path.join(where, "ABCD1234.gpg"), "w").close()
            self.assertEqual(module.keyring(distro), None)
            open(os.path.join(where, "InRelease"), "w").close()
            self.assertEqual(module.keyring(distro), "ABCD1234.gpg")
        finally:
            module.repository = original

    # Signing changes the .dsc and the .changes, so who signed is part of
    # what says whether a package needs building again.
    def test_the_key_decides_whether_a_rebuild_is_needed(self):
        from seine.packages import Builder
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages

        digests = set()
        for key in [None, "spec@example.invalid"]:
            builder = Builder(distro, {"sign_key": key}, None)
            digests.add(os.path.basename(builder.stamps(package)[0][2]))
        self.assertEqual(len(digests), 2,
                         "signing a build did not ask for it to be rebuilt")

# The specification content behind a build, redacted and with every file
# path made portable -- what a person or the AI chat reads to tell what a
# cached build actually has in it.
FLUSH_IMAGE = ("image:\n"
              "    filename: digest-excerpt-test.img\n"
              "    partitions:\n"
              "        - label: rootfs\n"
              "          where: /\n")

class DigestExcerpt(avocado.Test):
    def builder(self, redact_patterns=None):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        return Builder(distro, {}, BuilderImage(distro, {}), redact_patterns)

    def kernel(self, settings, extra=""):
        return parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
%s
%s
        """ % (settings, extra)).image.packages[0]

    # A path names the file that declared it ('origin_of()') as the
    # anchor to be relative to -- which only exists for a spec loaded
    # from a real file, not from 'parse()' 's in-memory string. Written
    # to 'self.workdir' and loaded from there so a test can exercise
    # that anchor for real, flush left (no leading indent, unlike the
    # rest of this file's triple-quoted strings) since it is written to
    # disk rather than parsed inline.
    def load(self, content):
        path = os.path.join(self.workdir, "spec.yml")
        with open(path, "w") as f:
            f.write(content + FLUSH_IMAGE)
        build = BuildCmd()
        build.load_all([path])
        build.parse()
        return build.image.packages[0]

class APlainPackageExcerptsSourceAndRevisionOnly(DigestExcerpt):
    def test(self):
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        self.assertEqual(self.builder().digest_excerpt(package),
                         {"source": "apt://busybox", "revision": "mod1"})

class PatchesAreExcerptedAsPortablePaths(DigestExcerpt):
    def test(self):
        os.makedirs(os.path.join(self.workdir, "patches"), exist_ok=True)
        open(os.path.join(self.workdir, "patches", "0001-fix.patch"), "w").close()
        package = self.load(
            "packages:\n"
            "    - source: apt://busybox\n"
            "      patches:\n"
            "          - patches/0001-fix.patch\n")
        excerpt = self.builder().digest_excerpt(package)
        # Exactly as written, not the absolute path 'patch_files()'
        # resolved it to.
        self.assertEqual(excerpt["patches"], ["patches/0001-fix.patch"])

class KernelFragmentsAreExcerptedAsPortablePaths(DigestExcerpt):
    def test(self):
        os.makedirs(os.path.join(self.workdir, "configs"), exist_ok=True)
        open(os.path.join(self.workdir, "configs", "embedded.fragment"), "w").close()
        package = self.load(
            "packages:\n"
            "    - source: apt://linux\n"
            "      extends:\n"
            "          kernel:\n"
            "              flavour: amd64\n"
            "              fragments:\n"
            "                  - configs/embedded.fragment\n")
        excerpt = self.builder().digest_excerpt(package)
        self.assertEqual(excerpt["extends"]["kernel"]["fragments"],
                         ["configs/embedded.fragment"])

class KernelConfigsAreExcerptedAsWritten(DigestExcerpt):
    def test(self):
        package = self.kernel(
            "                              flavour: amd64",
            "                              configs:\n"
            "                                  magic-sysrq:\n"
            "                                      - CONFIG_MAGIC_SYSRQ=n")
        excerpt = self.builder().digest_excerpt(package)
        self.assertEqual(excerpt["extends"]["kernel"]["configs"],
                         {"magic-sysrq": ["CONFIG_MAGIC_SYSRQ=n"]})

class AnUnresolvableOriginLeavesThePathAsIs(DigestExcerpt):
    def test(self):
        # 'kernel_upstream_sha256'/etc. are set programmatically in some
        # tests below without an '_origins' entry at all -- the excerpt
        # has to degrade to the path as given rather than raise.
        import types
        package = types.SimpleNamespace(
            source="apt://linux", revision="mod1", profiles=[], options=[],
            sha256=None, patches=["a.patch"],
            kernel=False, module=False)
        package.origin_of = lambda setting: None
        package.patch_files = lambda: ["a.patch"]
        excerpt = self.builder().digest_excerpt(package)
        self.assertEqual(excerpt["patches"], ["a.patch"])

class TheExcerptIsRedacted(DigestExcerpt):
    def test(self):
        package = parse("""
                redact:
                    - 'AKIA[0-9A-Z]{16}'
                packages:
                    - source: apt://busybox
                      revision: AKIAABCDEFGHIJKLMNOP
        """).image.packages[0]
        from seine.utils import redactions
        builder = self.builder(redactions(
            {"redact": ["AKIA[0-9A-Z]{16}"]}))
        excerpt = builder.digest_excerpt(package)
        self.assertNotIn("AKIA", excerpt["revision"])
        self.assertIn("<redacted:", excerpt["revision"])

# The excerpt file lives beside its stamp in name only -- in a directory
# of its own, never the stamps directory itself, which '_previous()' and
# 'cache.py' 's own lookup both scan by name prefix.
class ExcerptLivesInItsOwnDirectory(DigestExcerpt):
    def test(self):
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        builder = self.builder()
        stamp = builder.stamps([package])[0][2]
        builder._record_excerpt(stamp, package)

        excerpt = builder._excerpt_path(stamp)
        self.assertTrue(os.path.isfile(excerpt))
        self.assertNotEqual(os.path.dirname(excerpt),
                            os.path.dirname(stamp))
        # '_previous()' matches every file in the stamps directory by
        # name prefix -- proof the excerpt, sitting elsewhere, is never
        # mistaken for a stamp and its YAML mis-read as a file list.
        self.assertEqual(builder._previous(package, "amd64"), {})

class ForgettingAStampForgetsItsExcerptToo(DigestExcerpt):
    def test(self):
        package = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        builder = self.builder()
        stamp = builder.stamps([package])[0][2]
        open(stamp, "w").close()
        builder._record_excerpt(stamp, package)
        excerpt = builder._excerpt_path(stamp)
        self.assertTrue(os.path.isfile(excerpt))

        builder._forget(package, "amd64", produced=set())
        self.assertFalse(os.path.isfile(stamp))
        self.assertFalse(os.path.isfile(excerpt),
                         "the excerpt outlived the stamp it belongs to")
