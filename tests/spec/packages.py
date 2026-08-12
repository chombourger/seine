#!/usr/bin/env python3

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

# Nothing under here may write into the machine's own cache. These build
# Builder objects directly, and asking one for a stamp or an index makes
# the directory it would live in -- so a plain unit test run leaves
# directories in ~/.cache/seine, and a run at an older commit leaves them
# in a layout the current code no longer uses.
#
# One cache per test process, thrown away with it. Set rather than
# defaulted so it holds however the suite was invoked; the tests that
# build images for real pass their own to the seine they run.
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

class PackageNamedAfterItsSource(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://busybox
                    - source: git://example.com/team/busybox-utils.git;rev=deadbeef
        """)
        names = [(p.name, p.source_name) for p in build.image.packages]
        self.assertEqual(sorted(names), [("busybox", "busybox"),
                                         ("busybox-utils", "busybox-utils")])

class PackageNamesItsSourcePackage(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
        """)
        package = build.image.packages[0]
        self.assertEqual(package.name, "nvidia-open")
        # What it is fetched by is not renamed with it: that is the
        # repository the clone comes from, not the package it makes.
        self.assertEqual(package.source_name, "open-gpu-kernel-modules")

class PackageNameIsReferencedByOrdering(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://application
                      after:
                          - nvidia-open
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
        """)
        self.assertEqual([p.name for p in build.image.packages],
                         ["nvidia-open", "application"])

class PackageNameIsNotASourcePackageName(avocado.Test):
    def test(self):
        for name in ["NVIDIA-open", "nvidia_open", "-nvidia", "n"]:
            try:
                parse("""
                packages:
                    - source: apt://busybox
                      name: %s
                """ % name)
                self.fail("parsing succeeded for the package name '%s'!" % name)
            except ValueError:
                pass

MODULE = """
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
                      version: "580.95.05"
                      extends:
                          module:
%s
"""

class ModuleExtension(avocado.Test):
    def test(self):
        build = parse_for("amd64", """
                packages:
                    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
                      name: nvidia-open
                      version: "580.95.05"
                      extends:
                          module:
                              build: kernel-open
                              modules:
                                  - nvidia
                                  - nvidia-drm
                              make-vars:
                                  SYSSRC: /usr/src/linux
                              amd64-kernels:
                                  - apt://linux-headers-amd64
                                  - linux
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        package = [p for p in build.image.packages if p.name == "nvidia-open"][0]
        self.assertEqual(package.module, True)
        self.assertEqual(package.module_build, "kernel-open")
        self.assertEqual(package.module_modules, ["nvidia", "nvidia-drm"])
        self.assertEqual(package.module_make_vars, {"SYSSRC": "/usr/src/linux"})
        self.assertEqual(package.module_kernels,
                         {"amd64": ["apt://linux-headers-amd64", "linux"]})
        self.assertEqual(package.upstream_version, "580.95.05")

class ModuleDefaults(avocado.Test):
    def test(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
        """)
        package = build.image.packages[0]
        # The tree's root, for packaging that keeps its makefile there.
        self.assertEqual(package.module_build, ".")
        self.assertEqual(package.module_modules, [])
        self.assertEqual(package.module_make_vars, {})

class ModulesAreBuiltNativelyForNow(avocado.Test):
    def test(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
        """)
        # Until a module has kbuild tools it can run, which is a later
        # commit. Not the default anything else gets.
        self.assertEqual(build.image.packages[0].cross, False)

# A module is built against its kernel's headers, which depend on the
# linux-kbuild of the same ABI -- and 'pkg.linux.notools' is the one
# profile that does not build one. For a kernel grafted here no archive
# can supply it either, so this is refused when the specification is read
# rather than by the build that would fail on it.
class AKernelWithModulesKeepsItsKbuild(avocado.Test):
    KERNEL_AND_MODULE = """
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [linux]
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
                      profiles: [%s]
    """

    def tasks(self, profiles):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        build = parse_for("amd64", self.KERNEL_AND_MODULE % profiles)
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        builder._kernels_keep_their_kbuild(build.image.packages)

    def test_notools_is_refused(self):
        from seine.packages import MIN_TOOLS
        try:
            self.tasks("pkg.linux.notools")
            self.fail("a kernel with no kbuild was accepted!")
        except ValueError as e:
            # The way out is named, since the profile that works is not
            # one anybody guesses.
            self.assertIn(MIN_TOOLS, str(e))

    def test_mintools_is_not(self):
        self.tasks("pkg.linux.mintools")

    def test_asking_for_every_tool_is_not(self):
        self.tasks("nodoc")

# A kernel records its ABI while its source is prepared, which only a
# build that rebuilds it does. The second build of a specification reuses
# the kernel, so the ABI has to come from what the first one left behind.
class AnAbiSurvivesTheBuildThatMadeIt(avocado.Test):
    def test_it_is_read_back_off_the_stamp(self):
        from seine.packages import Builder, STAMPS
        from seine.sbuild import BuilderImage
        build = parse_for("amd64", """
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [linux]
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        packages = build.image.packages
        kernel = [p for p in packages if p.kernel][0]
        module = [p for p in packages if p.module][0]

        # Nothing recorded, as on the second build of a specification.
        self.assertEqual(builder.abinames, {})
        stamp = builder.stamp(kernel, "amd64")
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        with open(stamp, "w") as f:
            f.write("linux-headers-6.18+unreleased-amd64_1_amd64.deb\n")
            f.write("linux-headers-6.18+unreleased-common_1_all.deb\n")
            f.write("linux-image-6.18+unreleased-amd64_1_amd64.deb\n")

        kernels = builder.resolved_kernels(module, "amd64", packages)
        self.assertEqual([k.release for k in kernels],
                         ["6.18+unreleased-amd64"])

# A kernel built here is published under the distribution's own names, so
# a metapackage naming that flavour resolves to the graft once the image
# is composed. Resolving both builds the modules for a kernel the image
# does not carry, and writes one metapackage stanza twice.
class AGraftSupersedesTheMetapackageItReplaces(avocado.Test):
    def resolved(self, kernels):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        build = parse_for("amd64", """
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [%s]
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """ % kernels)
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        builder.abinames["linux"] = "6.18+unreleased"
        builder.metapackages[("amd64", "apt://linux-headers-amd64")] = \
            "linux-headers-6.12.101+deb13-amd64"
        module = [p for p in build.image.packages if p.module][0]
        return builder.resolved_kernels(module, "amd64", build.image.packages)

    def test_the_graft_is_what_is_left(self):
        kernels = self.resolved("linux, apt://linux-headers-amd64")
        self.assertEqual([k.release for k in kernels],
                         ["6.18+unreleased-amd64"])

    def test_an_abi_written_out_is_kept(self):
        # It names one kernel rather than a flavour, so it is not the one
        # the graft takes over, and it has no name to collide on.
        kernels = self.resolved(
            "linux, apt://linux-headers-6.12.101+deb13-amd64")
        self.assertEqual(sorted(k.release for k in kernels),
                         ["6.12.101+deb13-amd64", "6.18+unreleased-amd64"])

    def test_a_metapackage_alone_is_untouched(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
        """)
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        builder.metapackages[("amd64", "apt://linux-headers-amd64")] = \
            "linux-headers-6.12.101+deb13-amd64"
        kernels = builder.resolved_kernels(build.image.packages[0], "amd64",
                                           build.image.packages)
        self.assertEqual([k.release for k in kernels],
                         ["6.12.101+deb13-amd64"])

class UnknownModuleSetting(avocado.Test):
    def test(self):
        try:
            parse_for("amd64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
                              kernels: [linux]
            """)
            self.fail("parsing succeeded for an unknown 'module' setting!")
        except ValueError as e:
            # The message says how kernels are named, since naming them
            # flat is the mistake somebody makes once.
            self.assertIn("<architecture>-kernels", str(e))

class ModuleNamesAKernelImage(avocado.Test):
    def test(self):
        try:
            parse_for("amd64", MODULE % """
                              amd64-kernels: [apt://linux-image-amd64]
            """)
            self.fail("parsing succeeded for a kernel image package!")
        except ValueError as e:
            self.assertIn("linux-headers-amd64", str(e))

class ModuleNamesAnUnsupportedScheme(avocado.Test):
    def test(self):
        try:
            parse_for("amd64", MODULE % """
                              amd64-kernels: [git://example.com/linux.git;rev=1]
            """)
            self.fail("parsing succeeded for a kernel named by a git tree!")
        except ValueError:
            pass

class ModuleKernelsAreAList(avocado.Test):
    def test(self):
        try:
            parse_for("amd64", MODULE % """
                              amd64-kernels: apt://linux-headers-amd64
            """)
            self.fail("parsing succeeded for a kernel list that is not one!")
        except ValueError:
            pass

class ModuleWithoutAVersion(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
                distribution:
                    release: trixie
                    architecture: amd64
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      extends:
                          module:
                              amd64-kernels: [apt://linux-headers-amd64]
        """ + IMAGE)
        try:
            build.parse()
            self.fail("parsing succeeded for a module with no version!")
        except ValueError:
            pass

class ModuleWithoutKernelsForItsArchitecture(avocado.Test):
    def test(self):
        try:
            parse_for("arm64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
            """)
            self.fail("parsing succeeded for a module built for no kernels!")
        except ValueError as e:
            message = str(e)
            self.assertIn("nvidia-open", message)
            self.assertIn("arm64", message)
            # What it does name, so a misspelt architecture is read off
            # the message rather than hunted for.
            self.assertIn("amd64", message)

class EveryModuleMissingKernelsIsNamed(avocado.Test):
    def test(self):
        try:
            parse_for("arm64", """
                packages:
                    - source: git://example.com/one.git;rev=deadbeef
                      name: driver-one
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [apt://linux-headers-amd64]
                    - source: git://example.com/two.git;rev=deadbeef
                      name: driver-two
                      version: "2.0"
                      extends:
                          module:
                              amd64-kernels: [apt://linux-headers-amd64]
            """)
            self.fail("parsing succeeded for two modules built for no kernels!")
        except ValueError as e:
            # One message, not one build each finding the next.
            self.assertIn("driver-one", str(e))
            self.assertIn("driver-two", str(e))

class ModuleNamesAKernelNothingBuilds(avocado.Test):
    def test(self):
        try:
            parse_for("amd64", MODULE % """
                              amd64-kernels: [linux]
            """)
            self.fail("parsing succeeded for a kernel nothing builds!")
        except ValueError as e:
            # It says both ways out: build that kernel, or name the
            # distribution's headers package.
            self.assertIn("apt://linux-headers-amd64", str(e))

class ModuleKernelsAreCheckedPerArchitecture(avocado.Test):
    def test(self):
        try:
            build = parse_for("arm64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
                              arm64-kernels: [apt://linux-headers-arm64]
            """)
        except ValueError as e:
            self.fail("a module naming this architecture's kernels was "
                      "refused: %s" % e)
        self.assertEqual(sorted(build.image.packages[0].module_kernels),
                         ["amd64", "arm64"])

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
                for p, a, s in builder.stamps(build.image.packages)}

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

# What a kernel reference turns into, which decides the name of every
# binary package a module build produces.
class ResolvedKernels(avocado.Test):
    def builder(self, architecture="amd64"):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture,
                  "uri": "http://example.com/debian"}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def test_an_explicit_headers_package_needs_nothing_asked(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)
        kernels = self.builder().resolved_kernels(
            build.image.packages[0], "amd64")
        self.assertEqual([k.release for k in kernels],
                         ["6.12.101+deb13-amd64"])
        self.assertEqual([k.headers for k in kernels],
                         ["linux-headers-6.12.101+deb13-amd64"])

    def test_a_featureset_is_part_of_the_release(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-rt-amd64
        """)
        kernels = self.builder().resolved_kernels(
            build.image.packages[0], "amd64")
        # 'rt-amd64' is a flavour within a featureset and not two things
        # to be told apart: what is stripped is the prefix, nothing else.
        self.assertEqual([k.release for k in kernels],
                         ["6.12.101+deb13-rt-amd64"])

    def test_a_grafted_kernel_is_named_for_the_abi_it_gave_itself(self):
        build = parse_for("amd64", """
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [linux]
                    - source: apt://linux
                      extends:
                          kernel:
                              upstream: https://kernel.org/linux-6.18.43.tar.xz
                              flavour: amd64
        """)
        packages = build.image.packages
        module = [p for p in packages if p.name == "driver"][0]
        builder = self.builder()
        # What extend_kernel() records once the kernel's control file has
        # been regenerated. Nothing predicts it: an UNRELEASED changelog
        # is what turns a version into '6.18+unreleased'.
        builder.abinames["linux"] = "6.18+unreleased"
        kernels = builder.resolved_kernels(module, "amd64", packages)
        self.assertEqual([k.release for k in kernels],
                         ["6.18+unreleased-amd64"])
        self.assertEqual([k.headers for k in kernels],
                         ["linux-headers-6.18+unreleased-amd64"])

    def test_a_module_is_built_after_the_kernels_it_names(self):
        build = parse_for("amd64", """
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [linux]
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        # Written by nobody: naming a kernel is what says the module is
        # built on it, and that is what carries the kernel's digest into
        # the module's stamp.
        self.assertEqual([p.name for p in build.image.packages],
                         ["linux", "driver"])

    def test_an_unprepared_kernel_says_so(self):
        build = parse_for("amd64", """
                packages:
                    - source: git://example.com/driver.git;rev=deadbeef
                      name: driver
                      version: "1.0"
                      extends:
                          module:
                              amd64-kernels: [linux]
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        packages = build.image.packages
        module = [p for p in packages if p.name == "driver"][0]
        try:
            self.builder().resolved_kernels(module, "amd64", packages)
            self.fail("a kernel with no ABI yet resolved to something!")
        except ValueError:
            pass

# The packaging seine writes for a tree that carries none.
class GeneratedPackaging(avocado.Test):
    def packaging(self, architecture="amd64", spec=None, resolved=None):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        build = parse_for(architecture, spec or MODULE % """
                              build: kernel-open
                              modules: [nvidia, nvidia-drm]
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
                              arm64-kernels:
                                  - apt://linux-headers-arm64
        """)
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture,
                  "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        builder.packages = build.image.packages
        for key, value in (resolved or {}).items():
            builder.metapackages[key] = value
        package = [p for p in build.image.packages if p.module][0]
        source = os.path.join(self.workdir, package.name)
        os.makedirs(source, exist_ok=True)
        builder.extend_module(package, source, 1700000000)
        written = {}
        for name in ["changelog", "control", "rules", "source/format"]:
            with open(os.path.join(source, "debian", name), "r") as f:
                written[name] = f.read()
        return written

    def test_every_architecture_is_described(self):
        control = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["control"]
        # One .dsc is published however many architectures are built
        # from it, so the control file has to describe all of them --
        # two builds writing one filename with two contents otherwise.
        self.assertIn("Package: nvidia-open-modules-6.12.101+deb13-amd64", control)
        self.assertIn("Package: nvidia-open-modules-6.12.101+deb13-arm64", control)
        self.assertIn("Architecture: amd64", control)
        self.assertIn("Architecture: arm64", control)

    def test_headers_are_qualified_by_architecture(self):
        control = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["control"]
        # So an amd64 build installs amd64's headers and no others.
        self.assertIn("linux-headers-6.12.101+deb13-amd64 [amd64],", control)
        self.assertIn("linux-headers-6.12.101+deb13-arm64 [arm64],", control)

    def test_a_metapackage_pins_what_it_points_at(self):
        control = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["control"]
        # Exactly, or apt is free to pair a metapackage from this build
        # with modules from an earlier one -- which is a machine running
        # a driver nobody built for the kernel it has.
        self.assertIn(
            "nvidia-open-modules-6.12.101+deb13-arm64 (= ${binary:Version})",
            control)

    def test_a_flavour_gets_a_metapackage(self):
        control = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["control"]
        # Named for the flavour, which outlives the ABI under it, so a
        # playbook installs modules without naming a kernel version.
        self.assertIn("Package: nvidia-open-modules-arm64", control)
        # The pinned kernel gets none: an ABI cannot be split back into
        # an ABI and a flavour, and a specification that named an exact
        # kernel can name the exact package.
        self.assertNotIn("Package: nvidia-open-modules-amd64\n", control)

    def test_the_rules_name_each_architectures_kernels(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        self.assertIn("KERNELS_amd64 = 6.12.101+deb13-amd64", rules)
        self.assertIn("KERNELS_arm64 = 6.12.101+deb13-arm64", rules)
        self.assertIn("KERNELS = $(KERNELS_$(DEB_HOST_ARCH))", rules)

    def test_the_build_names_kbuild_output(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # Exported for the tree's benefit: one that does no more than
        # 'make -C $(KDIR) M=$(PWD)' would otherwise have kbuild put
        # objtree in the -common package, which carries neither .config
        # nor auto.conf.
        self.assertIn("KBUILD_OUTPUT=$$KERNEL_OBJ", rules)

    def test_the_modules_may_be_compressed_when_installed(self):
        written = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})
        # A kernel installs its modules compressed if it was built that
        # way, so the compressor has to be there -- and what it was built
        # with is the kernel's business rather than this package's.
        self.assertIn("xz-utils", written["control"])
        self.assertIn("zstd", written["control"])
        # And what came out is looked for under either name.
        self.assertIn("-name $$m.ko.\\*", written["rules"])

    def test_the_build_is_given_the_jobs_it_was_asked_for(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # dh_auto_build would have worked this out, and these rules do
        # not go through it: without saying so a module tree compiles one
        # file at a time however many processors it was given.
        self.assertIn("parallel=%", rules)
        self.assertIn("$(MAKE) $(JOBS)", rules)

    def test_the_tree_is_built_through_its_own_makefile(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # Driving kbuild directly skips whatever the tree does first,
        # and packaging that computes what kbuild needs produces a build
        # that succeeds and ships nothing.
        self.assertIn("-C $(BUILD) $(TARGET)", rules)
        self.assertIn("TARGET = modules", rules)

    def test_the_architecture_is_not_asked_of_the_machine(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # A tree left to ask uname compiles for the builder when it is
        # cross-compiling, and is right only by luck when it is not.
        self.assertIn("KERNEL_ARCH_amd64   = x86_64", rules)
        self.assertIn("KERNEL_ARCH_arm64   = arm64", rules)
        self.assertIn("$(KERNEL_ARCH_$(DEB_HOST_ARCH))", rules)
        # And uname's spelling beside the kernel's, which are the same
        # word on amd64 and different ones on arm64 -- so a value that
        # wants the second and is given the first is right until the day
        # it is built for something else.
        self.assertIn("KERNEL_MACHINE_amd64   = x86_64", rules)
        self.assertIn("KERNEL_MACHINE_arm64   = aarch64", rules)
        self.assertIn("$(KERNEL_MACHINE_$(DEB_HOST_ARCH))", rules)

    def test_the_distributions_build_flags_are_kept_out(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # They are for userspace: a module compiled with them is
        # compiled against the wrong world.
        self.assertIn("CFLAGS= CXXFLAGS= CPPFLAGS= LDFLAGS= dh $@", rules)

    def test_both_halves_of_the_headers_are_named(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # A tree that tests the kernel by compiling against it needs the
        # generic sources, which Debian keeps in the -common package.
        # Read out of the flavour package's makefile rather than guessed.
        self.assertIn("KERNEL_OBJ=$(HEADERS)-$(1)", rules)
        self.assertIn("KERNEL_SRC=`sed -n", rules)

    def test_a_value_naming_a_variable_reaches_the_shell(self):
        rules = self.packaging(spec=MODULE % """
                              make-vars:
                                  SYSSRC: $KERNEL_SRC
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)["rules"]
        # Doubled, because make reads it first: '$KERNEL_SRC' is the
        # variable K and the word ERNEL_SRC to make, and the shell that
        # was meant to expand it never sees a dollar.
        self.assertIn('SYSSRC="$$KERNEL_SRC"', rules)

    def test_each_kernel_is_named_to_the_environment(self):
        rules = self.packaging(spec=MODULE % """
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)["rules"]
        # The loop variable has to reach the macro that sets up the
        # environment, or every kernel is described as the empty one.
        self.assertIn("$(call kernel_env,$$k)", rules)

    def test_modules_may_ask_for_what_they_need_installed(self):
        control = self.packaging(spec=MODULE % """
                              runtime-depends:
                                  - firmware-nvidia-gsp
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)["control"]
        # Beside the kernel they were built for, which seine adds itself.
        self.assertIn("linux-image-6.12.101+deb13-amd64, firmware-nvidia-gsp",
                      control)

    def test_a_tree_may_ask_for_what_it_needs_to_build(self):
        control = self.packaging(spec=MODULE % """
                              build-depends:
                                  - python3
                                  - libelf-dev
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)["control"]
        self.assertIn("python3,", control)
        self.assertIn("libelf-dev", control)

    def test_the_source_package_is_native(self):
        written = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})
        # There is no upstream tarball to be a delta against: what is
        # built is a tree.
        self.assertEqual(written["source/format"], "3.0 (native)\n")
        self.assertIn("nvidia-open (580.95.05) unstable", written["changelog"])


class MakeVarsCannotRunCommands(avocado.Test):
    def test(self):
        for value in ["$(id)", "`id`", "x; id", "x && id"]:
            try:
                parse_for("amd64", MODULE % ("""
                              make-vars:
                                  SYSSRC: "%s"
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
                """ % value))
                self.fail("a make variable running '%s' was accepted!" % value)
            except ValueError:
                pass

class MakeVarsNameTheKernelBeingBuiltFor(avocado.Test):
    def test(self):
        build = parse_for("amd64", MODULE % """
                              make-vars:
                                  SYSSRC: $KERNEL_SRC
                                  SYSOUT: $KERNEL_OBJ
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)
        # The variables the rules set per kernel are what a value is for:
        # the kernel differs on every turn of the loop.
        self.assertEqual(build.image.packages[0].module_make_vars,
                         {"SYSSRC": "$KERNEL_SRC", "SYSOUT": "$KERNEL_OBJ"})

class PackagingDecidesWhetherToRebuild(avocado.Test):
    def test(self):
        from seine.packages import module_packaging
        # The packaging is data, and the digest reads it: editing the
        # rules has to be enough to ask for a rebuild, or the modules go
        # on being the ones the old rules produced.
        _, content = module_packaging()
        self.assertGreater(len(content), 0)
        self.assertIn(b"KERNEL_ARCH", content)

# A metapackage names whichever kernel is current, and until somebody
# has asked apt which that is, nothing can be named after it.
class UnresolvedMetapackagesAreRefused(avocado.Test):
    def test(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "arm64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        build = parse_for("arm64", MODULE % """
                              arm64-kernels: [apt://linux-headers-arm64]
        """)
        try:
            builder.resolved_kernels(build.image.packages[0], "arm64",
                                     build.image.packages)
            self.fail("a metapackage nobody resolved was named anyway!")
        except ValueError as e:
            # Carrying on would strip 'linux-headers-arm64' to 'arm64'
            # and name the modules after a kernel that does not exist.
            self.assertIn("resolved", str(e))

class MetapackagesAreRecognised(avocado.Test):
    def test(self):
        from seine.packages import is_kernel_metapackage, is_built_kernel
        # An ABI starts with the kernel's version, which is what tells
        # one kernel from whichever kernel is current.
        for reference in ["apt://linux-headers-amd64",
                          "apt://linux-headers-rt-arm64"]:
            self.assertEqual(is_kernel_metapackage(reference), True, reference)
        for reference in ["apt://linux-headers-6.12.101+deb13-amd64",
                          "apt://linux-headers-6.18+unreleased-rt-arm64"]:
            self.assertEqual(is_kernel_metapackage(reference), False, reference)
        self.assertEqual(is_built_kernel("linux"), True)
        self.assertEqual(is_built_kernel("apt://linux-headers-amd64"), False)

class ResolvedMetapackages(avocado.Test):
    def test_the_headers_package_is_read_off_what_apt_said(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        # What 'apt-cache depends' prints, bracketing what is not in the
        # index it was asked about, and naming the -common half beside
        # the one wanted.
        output = (
            "linux-headers-arm64:arm64\n"
            "  Depends: <linux-headers-6.12.101+deb13-common:arm64>\n"
            "    linux-headers-6.12.101+deb13-common\n"
            "  Depends: linux-headers-6.12.101+deb13-arm64:arm64\n")
        self.assertEqual(
            builder._resolved_headers("apt://linux-headers-arm64", "arm64",
                                      output),
            "linux-headers-6.12.101+deb13-arm64")

    def test_nothing_resolved_says_so(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        try:
            builder._resolved_headers("apt://linux-headers-riscv64",
                                      "riscv64", "N: Unable to locate package")
            self.fail("a metapackage that resolved to nothing was accepted!")
        except ValueError:
            pass

    # Resolving happens while the graph is being made, so the step that
    # builds the host bootstrap has not run -- and the builder image doing
    # the asking is built FROM it. A warm cache supplies one either way,
    # which is why the order only matters on a machine whose images were
    # cleared, where the builder build stops at 'FROM ... did not resolve'.
    def test_the_host_bootstrap_is_made_before_the_builder_image(self):
        from seine.packages import Builder

        order = []

        class HostBootstrap:
            name = "bootstrap/debian/trixie/all"

            def create(self):
                order.append("bootstrap")

        class BuilderImage:
            def create(self, hostBootstrap):
                order.append("builder")

            def output(self, args, **kwargs):
                order.append("asked")
                return ("linux-headers-amd64\n"
                        "  Depends: linux-headers-6.12.101+deb13-amd64\n")

        build = parse_for("amd64", MODULE % """
                              amd64-kernels:
                                  - apt://linux-headers-amd64
        """)
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage())
        builder.resolve_kernels(build.image.packages, HostBootstrap())
        self.assertEqual(order, ["bootstrap", "builder", "asked"],
                         "the builder image was made before the bootstrap it "
                         "is built FROM")

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
             environment=None, check=True):
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
                     environment=None, check=True):
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
                     environment=None, check=True):
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
                     environment=None, check=True):
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
                     environment=None, check=True):
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
                     environment=None, check=True):
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

# Building a package a second time replaces what the first build left,
# symlinks included: sbuild writes its log under a dated name and points
# an undated symlink at it, so a repository already holding one is the
# ordinary case rather than the exception.
#
# Across filesystems, which is the case that goes wrong and the ordinary
# one: the scratch space a build writes in and the repository it is
# published to are separate directories, and SEINE_BUILD_DIR exists to
# put them on separate drives. A rename replaces whatever is at the
# destination, so within one filesystem this passes either way; across
# two, shutil falls back to recreating the symlink, and that fails on one
# already there.
class ASecondBuildReplacesTheFirst(Publishes):
    def test(self):
        from seine.packages import Builder

        class Image:
            def exec(self, args, architecture=None, volumes=None, workdir=None,
                     environment=None, check=True):
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
                     environment=None, check=True):
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
                     environment=None, check=True):
                commands.append(" ".join(args))
                return 0

        self.builder(Image()).index()
        self.assertEqual(len(commands), 1)
        self.assertIn("apt-ftparchive sources .", commands[0])
        self.assertIn("Sources.gz", commands[0])

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
