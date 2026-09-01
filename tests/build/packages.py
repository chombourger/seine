#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import os
import shutil
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

class PackageWithoutExtensions(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://busybox
        """)
        package = build.image.packages[0]
        self.assertEqual(package.kernel, False)
        self.assertEqual(package.kernel_fragments, [])
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

# same_as() is what a caller merging several images' package lists asks
# before deciding two entries naming the same package may share one task.
class TwoPackagesTheSameSettingsAreTheSamePackage(avocado.Test):
    def test(self):
        one = parse("""
                packages:
                    - source: apt://busybox
                      patches: []
        """).image.packages[0]
        two = parse("""
                packages:
                    - source: apt://busybox
                      patches: []
        """).image.packages[0]
        self.assertTrue(one.same_as(two))

    def test_a_different_setting_is_a_different_package(self):
        one = parse("""
                packages:
                    - source: apt://busybox
                      cross: false
        """).image.packages[0]
        two = parse("""
                packages:
                    - source: apt://busybox
                      cross: true
        """).image.packages[0]
        self.assertFalse(one.same_as(two))

    def test_origins_do_not_count(self):
        # Two files can describe the same package and still be two files,
        # which is all '_origins' says -- comparing it would make the same
        # package different for having been merged in a different file.
        one = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        two = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages[0]
        one.spec["_origins"] = {"source": "board-a.yml"}
        two.spec["_origins"] = {"source": "board-b.yml"}
        self.assertTrue(one.same_as(two))

    def test_priority_does_not_count(self):
        # Where a package sorts in one specification's own list, not
        # something the package it describes has two versions of.
        one = parse("""
                packages:
                    - source: apt://busybox
                      priority: 100
        """).image.packages[0]
        two = parse("""
                packages:
                    - source: apt://busybox
                      priority: 900
        """).image.packages[0]
        self.assertTrue(one.same_as(two))

class ASharedBuilderRejectsBeingTaskedTwice(avocado.Test):
    def builder(self):
        from seine.packages import Builder
        from seine.sbuild   import BuilderImage
        spec = {"source": "debian", "release": "trixie", "architecture": "amd64",
               "uri": "http://example.com/debian"}
        return Builder(spec, {}, BuilderImage(spec, {}))

    def test_a_different_list_is_rejected(self):
        # The hazard this exists for: two images of one release, each
        # calling tasks() with only its own packages instead of the
        # union -- the second call would silently leave a module
        # resolving against only its own list.
        one = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages
        two = parse("""
                packages:
                    - source: apt://vim
        """).image.packages
        builder = self.builder()
        builder.tasks(one, object())
        try:
            builder.tasks(two, object())
            self.fail("a second tasks() call with a different list was accepted!")
        except RuntimeError:
            pass

    def test_the_same_list_again_still_answers(self):
        # A caller only reading the graph, not running it, may ask twice.
        packages = parse("""
                packages:
                    - source: apt://busybox
        """).image.packages
        builder = self.builder()
        first = [t.name for t in builder.tasks(packages, object())]
        second = [t.name for t in builder.tasks(packages, object())]
        self.assertEqual(first, second)
