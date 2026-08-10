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
