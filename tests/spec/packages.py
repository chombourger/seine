#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd

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

class SupportedSources(avocado.Test):
    def test(self):
        try:
            build = parse("""
                packages:
                    - source: apt://busybox
                    - source: apt://busybox=1:1.37.0-6
                    - source: https://deb.debian.org/debian/pool/b/busybox/busybox_1.37.0-6.dsc
                    - source: git://salsa.debian.org/installer-team/busybox.git;branch=master;rev=deadbeef
            """)
        except ValueError as e:
            self.fail("failed to parse valid package sources: %s" % e)

        packages = build.image.packages
        self.assertEqual(len(packages), 4)
        self.assertEqual(packages[0].name, "busybox")
        self.assertEqual(packages[0].version, None)
        self.assertEqual(packages[1].version, "1:1.37.0-6")
        self.assertEqual(packages[2].scheme, "https")
        self.assertEqual(packages[3].parameters["rev"], "deadbeef")

class PackagesOrderedByPriority(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://last
                      priority: 900
                    - source: apt://first
                      priority: 100
                    - source: apt://default
        """)
        names = [p.name for p in build.image.packages]
        self.assertEqual(names, ["first", "default", "last"])

class PackagesOrderedByAfter(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://application
                      after:
                          - library
                    - source: apt://library
        """)
        self.assertEqual([p.name for p in build.image.packages],
                         ["library", "application"])

class PackagesOrderedByBefore(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://application
                    - source: apt://library
                      before:
                          - application
        """)
        self.assertEqual([p.name for p in build.image.packages],
                         ["library", "application"])

class ConstraintsWinOverPriority(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://application
                      priority: 100
                      after:
                          - library
                    - source: apt://library
                      priority: 900
        """)
        self.assertEqual([p.name for p in build.image.packages],
                         ["library", "application"])

class PriorityDecidesWhenUnconstrained(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://application
                      after:
                          - library
                    - source: apt://library
                    - source: apt://early
                      priority: 100
        """)
        self.assertEqual([p.name for p in build.image.packages],
                         ["early", "library", "application"])

class UnknownPackageReferenced(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://application
                      after:
                          - nosuchpackage
            """)
            self.fail("parsing succeeded for an 'after' naming an unknown package!")
        except ValueError:
            pass

class CircularConstraints(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://one
                      after:
                          - two
                    - source: apt://two
                      after:
                          - one
            """)
            self.fail("parsing succeeded for packages depending on each other!")
        except ValueError:
            pass

class PackageReferencingItself(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://one
                      after:
                          - one
            """)
            self.fail("parsing succeeded for a package listed after itself!")
        except ValueError:
            pass

class KernelExtension(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              config:
                                  - configs/embedded.fragment
                              flavour: arm64
        """)
        package = build.image.packages[0]
        self.assertEqual(package.kernel, True)
        self.assertEqual(package.kernel_flavour, "arm64")
        self.assertEqual(package.kernel_config, ["configs/embedded.fragment"])
        # A flavour means nothing without a featureset, and nearly every
        # kernel wanted is in 'none'.
        self.assertEqual(package.kernel_featureset, "none")

class KernelFeatureset(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              featureset: rt
                              flavour: amd64
        """)
        self.assertEqual(build.image.packages[0].kernel_featureset, "rt")

class PackageWithoutExtensions(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://busybox
        """)
        package = build.image.packages[0]
        self.assertEqual(package.kernel, False)
        self.assertEqual(package.kernel_config, [])
        self.assertEqual(package.kernel_flavour, None)

class UnknownExtension(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      extends:
                          bootloader:
                              config: [x]
            """)
            self.fail("parsing succeeded for an unknown 'extends' build type!")
        except ValueError:
            pass

class UnknownKernelSetting(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  - typo.fragment
            """)
            self.fail("parsing succeeded for an unknown 'kernel' setting!")
        except ValueError:
            pass

class KernelFlavourNotAString(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour:
                                  - arm64
            """)
            self.fail("parsing succeeded for a non-string 'flavour'!")
        except ValueError:
            pass

class LocalRevision(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://busybox
                    - source: apt://linux
                      revision: acme3
        """)
        packages = build.image.packages
        self.assertEqual(packages[0].revision, "mod1")
        self.assertEqual(packages[1].revision, "acme3")

class RevisionNotAString(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      revision: 3
            """)
            self.fail("parsing succeeded for a non-string 'revision'!")
        except ValueError:
            pass

class DependentsRebuildWithTheirDependencies(avocado.Test):
    def stamps(self, profiles):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        build = parse("""
                packages:
                    - source: apt://base
                      profiles: [%s]
                    - source: apt://middle
                      after:
                          - base
                    - source: apt://top
                      after:
                          - middle
        """ % profiles)
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        return {p.name: os.path.basename(s).rsplit("_", 1)[1]
                for p, s in builder.stamps(build.image.packages)}

    def test(self):
        before = self.stamps("")
        after = self.stamps("nocheck")
        # The change is to 'base' alone; what is built against it, directly
        # or through another package, has to be rebuilt too.
        for name in ["base", "middle", "top"]:
            self.assertNotEqual(before[name], after[name],
                                "%s was not invalidated" % name)

class PackagesNotAList(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    source: apt://busybox
            """)
            self.fail("parsing succeeded when 'packages' was not a list!")
        except ValueError:
            pass

class SourceMissing(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - profiles: [nocheck]
            """)
            self.fail("parsing succeeded for a package with no 'source'!")
        except ValueError:
            pass

class UnsupportedScheme(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: ftp://example.com/busybox.dsc
            """)
            self.fail("parsing succeeded for an unsupported URI scheme!")
        except ValueError:
            pass

class HttpsSourceIsNotADsc(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: https://example.com/busybox-1.37.0.tar.bz2
            """)
            self.fail("parsing succeeded for an https source that is not a .dsc!")
        except ValueError:
            pass

class GitSourceWithoutRevision(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: git://example.com/busybox.git;branch=master
            """)
            self.fail("parsing succeeded for a git source with no ';rev='!")
        except ValueError:
            pass

class ProfilesNotAList(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://busybox
                      profiles: nocheck
            """)
            self.fail("parsing succeeded for 'profiles' that was not a list!")
        except ValueError:
            pass

class PackagesFromSeveralFilesAreAppended(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                packages:
                    - source: apt://one
        """)
        build.loads("""
                packages:
                    - source: apt://two
        """ + IMAGE)
        build.parse()
        self.assertEqual([p.name for p in build.image.packages], ["one", "two"])

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
             environment=None, check=True):
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
        builder.build(package, source, "0")
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

class DumpHidesInternalAttributes(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://busybox
                      priority: 100
        """)
        dumped = build.dump(build.spec)
        self.assertIn("apt://busybox", dumped)
        self.assertNotIn("_dirname", dumped)
        self.assertNotIn("priority", dumped)

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

class UpstreamKernel(avocado.Test):
    def builder(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "bookworm",
                  "architecture": "amd64", "uri": "http://example.com/debian",
                  "feeds": [{"suite": "bookworm"}]}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def kernel(self, settings, source="apt://linux"):
        build = parse("""
                packages:
                    - source: %s
                      extends:
                          kernel:
%s
        """ % (source, settings))
        return build.image.packages[0]

class UpstreamSourcesParsed(UpstreamKernel):
    def test(self):
        tarball = self.kernel(
            "                              upstream: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.43.tar.xz")
        self.assertEqual(tarball.kernel_upstream.scheme, "https")
        self.assertEqual(tarball.kernel_upstream.name, "linux-6.18.43.tar.xz")

        tree = self.kernel(
            "                              upstream: git://example.com/bsp.git;rev=deadbeef")
        self.assertEqual(tree.kernel_upstream.scheme, "git")
        self.assertEqual(tree.kernel_upstream.name, "bsp")
        self.assertEqual(tree.kernel_upstream.parameters["rev"], "deadbeef")

class UpstreamSourcesRejected(UpstreamKernel):
    def test(self):
        # A tree with no scheme, one named the way a source package is,
        # a git tree that is not pinned, and a plain upstream tarball
        # dressed as a tree: none of them names a kernel tree to build.
        for upstream in ["linux-6.18.43.tar.xz",
                         "apt://linux",
                         "git://example.com/bsp.git",
                         "https://example.com/linux-6.18.43.patch"]:
            try:
                self.kernel("                              upstream: %s" % upstream)
                self.fail("'%s' was accepted as a kernel tree!" % upstream)
            except ValueError:
                pass

class UpstreamNeedsTheDistributionsPackaging(UpstreamKernel):
    def test(self):
        # The graft keeps the distribution's debian/, so there has to be
        # one to keep: a .dsc from somewhere else brought its own.
        try:
            self.kernel(
                "                              upstream: git://example.com/bsp.git;rev=deadbeef",
                source="https://example.com/linux_6.1.0-1.dsc")
            self.fail("grafted onto a source that is not the distribution's!")
        except ValueError:
            pass

class KeepPatchesDefaultsToTheBuildSystem(UpstreamKernel):
    def test(self):
        package = self.kernel("                              flavour: amd64")
        self.assertEqual(package.kernel_keep_patches, None)

        # An empty list is an answer, not an omission.
        package = self.kernel("                              keep-patches: []")
        self.assertEqual(package.kernel_keep_patches, [])

# A patch as its '+++' lines describe it, which is all the default
# needs to tell the packaging from Debian's own kernel changes.
def patch_touching(*files):
    return "".join("--- a/%s\n+++ b/%s\n" % (f, f) for f in files)

PATCHES = {
    # Packaging: the one carrying ARCH and KERNELRELEASE into the build.
    "debian/kernelvariables.patch":       patch_touching("Makefile"),
    "debian/kbuild-module-lds.patch":     patch_touching("scripts/Makefile.modfinal"),
    # Debian's kernel policy, not its packaging.
    "debian/yama-disable-by-default.patch": patch_touching("security/yama/yama_lsm.c"),
    # Both, which counts as policy: taking it takes the C change too.
    "debian/version.patch":               patch_touching("Makefile", "lib/dump_stack.c"),
    # Always dropped: it describes an orig tarball we do not use.
    "debian/dfsg/remove-firmware.patch":  patch_touching("Makefile"),
    # A backport touching only a makefile is still a backport.
    "bugfix/all/kbuild-btf-fix.patch":    patch_touching("Makefile"),
}

class SeriesCutDownToWhatIsKept(UpstreamKernel):
    def series(self, package, patches=None):
        patches = PATCHES if patches is None else patches
        root = os.path.join(self.workdir, "debian", "patches")
        for name, body in patches.items():
            path = os.path.join(root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(body)
        with open(os.path.join(root, "series"), "w") as f:
            f.write("# a comment\n\n%s\n" % "\n".join(patches))

        self.builder()._filter_series(package, self.workdir)
        with open(os.path.join(root, "series"), "r") as f:
            return [line.strip() for line in f if len(line.strip()) > 0]

    def test(self):
        package = self.kernel("                              flavour: amd64")
        self.assertEqual(sorted(self.series(package)),
                         ["debian/kbuild-module-lds.patch",
                          "debian/kernelvariables.patch"])

class KeepPatchesOverridesWhatIsPackaging(SeriesCutDownToWhatIsKept):
    def test(self):
        # Saying so takes what the default would not have kept.
        package = self.kernel(
            "                              keep-patches: [ 'debian/yama*' ]")
        self.assertEqual(self.series(package),
                         ["debian/yama-disable-by-default.patch"])

class DropPatchesSubtractsFromWhatIsKept(SeriesCutDownToWhatIsKept):
    def test(self):
        package = self.kernel(
            "                              drop-patches: [ 'debian/kbuild-*' ]")
        self.assertEqual(self.series(package), ["debian/kernelvariables.patch"])

class BuildFilesAddToWhatCountsAsPackaging(SeriesCutDownToWhatIsKept):
    # A packaging that builds through a file Debian's does not touch:
    # Kconfig is not C source, but it is not a makefile either.
    PATCHES = {
        "debian/kconfig-defaults.patch": patch_touching("arch/arm64/Kconfig"),
        "debian/kernelvariables.patch":  patch_touching("Makefile"),
    }

    def test(self):
        package = self.kernel("                              flavour: amd64")
        self.assertEqual(self.series(package, self.PATCHES),
                         ["debian/kernelvariables.patch"])

        # Added to the shipped patterns rather than replacing them: the
        # makefile patch is still packaging.
        package = self.kernel(
            "                              build-files: [ '(^|/)Kconfig[^/]*$' ]")
        self.assertEqual(sorted(self.series(package, self.PATCHES)),
                         ["debian/kconfig-defaults.patch",
                          "debian/kernelvariables.patch"])

class BuildFilesAreCheckedWhereTheyAreWritten(UpstreamKernel):
    def test(self):
        try:
            self.kernel("                              build-files: [ '(unclosed' ]")
            self.fail("accepted a pattern that is not a regular expression!")
        except ValueError:
            pass

class BuildFilesCountInTheDigest(UpstreamKernel):
    def test(self):
        builder = self.builder()
        plain = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        extended = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz\n"
            "                              build-files: [ '(^|/)Kconfig[^/]*$' ]")
        # Which patches are kept changes what is built, so it has to
        # change what says whether it needs building.
        self.assertNotEqual(builder.stamp(plain), builder.stamp(extended),
                            "extending 'build-files' did not ask for a rebuild")

class KeepPatchesMatchingNothingIsRejected(SeriesCutDownToWhatIsKept):
    def test(self):
        package = self.kernel(
            "                              keep-patches: [ 'debian/renamed/*' ]")
        try:
            self.series(package, {"debian/kernelvariables.patch": patch_touching("Makefile")})
            self.fail("a 'keep-patches' matching no patch was accepted!")
        except ValueError:
            pass

# What Debian's debian/config/<arch>/defines.toml looks like where it
# matters here: flavours and featuresets as arrays of tables, each with
# tables of its own nested under it, and no 'enable' until we add one.
DEFINES_TOML = """[[flavour]]
name = 'amd64'
[flavour.defs]
is_default = true

[[flavour]]
name = 'cloud-amd64'
[flavour.build]
config = ['config.cloud']

[[featureset]]
name = 'none'

[[featureset]]
name = 'rt'
[[featureset.flavour]]
name = 'amd64'

[build]
enable_signed = true
"""

class RestrictsFlavoursInToml(UpstreamKernel):
    def defines(self):
        path = os.path.join(self.workdir, "defines.toml")
        with open(path, "w") as f:
            f.write(DEFINES_TOML)
        return path

    def enabled(self, path):
        import tomllib
        with open(path, "rb") as f:
            defines = tomllib.load(f)
        return {kind: [e["name"] for e in defines[kind] if e.get("enable", True)]
                for kind in ["flavour", "featureset"]}

    def test(self):
        package = self.kernel("                              flavour: amd64")
        path = self.defines()

        # Twice: a second pass has to overwrite the 'enable' it wrote the
        # first time rather than add another, which is a duplicate key and
        # so a file that no longer parses at all.
        for _ in range(2):
            self.builder()._restrict_flavour_toml(package, path, "amd64", ["flavour", "featureset"])
            self.assertEqual(self.enabled(path),
                             {"flavour": ["amd64"], "featureset": ["none"]})

class RestrictsToAFlavourThatExists(RestrictsFlavoursInToml):
    def test(self):
        package = self.kernel("                              flavour: nosuch")
        try:
            self.builder()._restrict_flavour_toml(package, self.defines(), "amd64", ["flavour", "featureset"])
            self.fail("restricted the kernel to a flavour it does not have!")
        except ValueError:
            pass

class WhatWasFetchedCountsInTheDigest(UpstreamKernel):
    def test(self):
        builder = self.builder()
        stamps = []
        for digest in ["a" * 64, "b" * 64]:
            package = self.kernel(
                "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz\n"
                "                              upstream-sha256: %s" % digest)
            stamps.append(builder.stamp(package))
        # A tarball that changed under the same URL is a different kernel,
        # which the URL alone could not say.
        self.assertNotEqual(stamps[0], stamps[1],
                            "a different upstream hash did not ask for a rebuild")

class UpstreamKernelIsRebuiltWhenItMoves(UpstreamKernel):
    def test(self):
        builder = self.builder()
        stamps = []
        for upstream in ["linux-6.18.43.tar.xz", "linux-6.18.44.tar.xz"]:
            package = self.kernel(
                "                              upstream: https://cdn.kernel.org/pub/linux/kernel/v6.x/%s"
                % upstream)
            stamps.append(builder.stamp(package))
        self.assertNotEqual(stamps[0], stamps[1],
                            "moving to another kernel tree did not ask for a rebuild")

class PatchListsCountAsSetsInTheDigest(UpstreamKernel):
    def test(self):
        builder = self.builder()
        stamps = []
        for order in [["debian/gitignore.patch", "debian/version.patch"],
                      ["debian/version.patch", "debian/gitignore.patch"]]:
            package = self.kernel(
                "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz\n"
                "                              drop-patches:\n"
                + "".join("                                  - %s\n" % p for p in order)
                + "                              keep-patches:\n"
                + "".join("                                  - %s\n" % p for p in order))
            stamps.append(builder.stamp(package))
        # Writing the same patches in another order is the same kernel,
        # and rebuilding it would be work for nothing.
        self.assertEqual(stamps[0], stamps[1],
                         "reordering a patch list asked for a rebuild")

class GraftedKernelsAreRebuiltWhenTheRulesChange(UpstreamKernel):
    def test(self):
        from seine.packages import kernel_rules
        builder = self.builder()
        grafted = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        ordinary = self.kernel("                              flavour: amd64")
        before = (builder.stamp(grafted), builder.stamp(ordinary))

        # The rules decide which of the distribution's patches a grafted
        # kernel is built with, so they are part of what it is built from.
        rules = kernel_rules()
        kernel_rules.cache_clear()
        try:
            import seine.packages
            seine.packages.kernel_rules = lambda: rules._replace(
                content=rules.content + b"\n# moved\n")
            after = (builder.stamp(grafted), builder.stamp(ordinary))
        finally:
            seine.packages.kernel_rules = kernel_rules
            kernel_rules.cache_clear()

        self.assertNotEqual(before[0], after[0],
                            "changing the rules did not ask for a rebuild")
        # An ordinary rebuild never consults them.
        self.assertEqual(before[1], after[1],
                         "a package that is not a graft was rebuilt for nothing")

# What Debian's generated debian/control holds for a rebuild whose
# changelog says UNRELEASED, which a local rebuild's has to: the ABI name
# is '6.18+unreleased' rather than the '6.1.0-53' a released kernel gets.
CONTROL = """Source: linux

Package: linux-headers-6.18+unreleased-common
Architecture: all

Package: linux-image-6.18+unreleased-amd64
Architecture: amd64

Package: linux-image-6.18+unreleased-amd64-dbg
Architecture: amd64

Package: linux-image-amd64
Architecture: amd64

Package: linux-image-6.18+unreleased-arm64
Architecture: arm64

Package: linux-image-6.18+unreleased-cloud-arm64
Architecture: arm64
"""

class KernelsAreCountedByTheirAbiName(UpstreamKernel):
    def control(self):
        path = os.path.join(self.workdir, "control")
        with open(path, "w") as f:
            f.write(CONTROL)
        return path

    def test(self):
        builder = self.builder()
        path = self.control()
        self.assertEqual(builder._abiname([("linux-headers-6.18+unreleased-common", [])]),
                         "6.18+unreleased")

        # One kernel where the flavours were restricted, and the debug
        # package and the metapackage beside it counted as neither.
        self.assertEqual(builder._kernel_packages(path, "amd64"),
                         ["linux-image-6.18+unreleased-amd64"])
        # An architecture nothing was restricted on still has all of its.
        self.assertEqual(builder._kernel_packages(path, "arm64"),
                         ["linux-image-6.18+unreleased-arm64",
                          "linux-image-6.18+unreleased-cloud-arm64"])

# The root debian/config/defines.toml, where featuresets are declared for
# every architecture at once and one of them already carries an 'enable'.
ROOT_DEFINES_TOML = """[[featureset]]
name = 'none'

[[featureset]]
name = 'rt'
enable = true
  [featureset.description]
  parts = ['rt']

[build]
compiler = 'gcc-12'
"""

class RestrictsFeaturesetsWhereTheyAreShared(UpstreamKernel):
    def test(self):
        import tomllib
        path = os.path.join(self.workdir, "defines.toml")
        with open(path, "w") as f:
            f.write(ROOT_DEFINES_TOML)

        package = self.kernel("                              flavour: amd64")
        # Twice, because this file already says 'enable' for rt: the
        # second pass has to overwrite it rather than add a second one,
        # which is a duplicate key and a file that no longer parses.
        for _ in range(2):
            self.builder()._restrict_flavour_toml(package, path, "amd64",
                                                  ["featureset"])
            with open(path, "rb") as f:
                defines = tomllib.load(f)
            self.assertEqual(
                [(e["name"], e.get("enable", True)) for e in defines["featureset"]],
                [("none", True), ("rt", False)])

class SignedCodeIsDisabledForAGraftedKernel(UpstreamKernel):
    def test(self):
        import tomllib
        path = os.path.join(self.workdir, "defines.toml")
        with open(path, "w") as f:
            f.write("[[flavour]]\nname = 'amd64'\n\n"
                    "[build]\nenable_signed = true\nenable_vdso = true\n")

        # Twice, to be sure the second pass settles the key rather than
        # adding a second one, and that what else the block said survives:
        # kernel_file and kernel_stem live there too, and losing them
        # would leave the packaging not knowing what it builds.
        for _ in range(2):
            self.builder()._toml_set(path, "build", "enable_signed", "false")
            with open(path, "rb") as f:
                defines = tomllib.load(f)
            self.assertEqual(defines["build"],
                             {"enable_signed": False, "enable_vdso": True})

class SignedCodeIsLeftAloneWithoutAGraft(UpstreamKernel):
    def test(self):
        # A rebuild of the distribution's own kernel still has the
        # lockdown patches, so the check that wants them passes and there
        # is nothing here to turn off.
        package = self.kernel("                              flavour: amd64")
        self.assertEqual(package.kernel_upstream, None)
