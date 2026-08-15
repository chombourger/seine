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

import seine.kernel

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

# The two architecture tables in the module packaging, read back. They
# answer different questions -- the kernel's name for an architecture and
# uname's -- and the way to get them wrong is to add one and forget the
# other, which nothing about a build would notice on the architecture
# that was added.
class ArchitecturesAreMappedBothWays(avocado.Test):
    def tables(self):
        from seine.kernel import KERNEL_ARCHITECTURES, KERNEL_MACHINES
        # The tables themselves, not the packaging rendered from them:
        # this is where an architecture is added, and where one gets
        # added to only one of the two.
        return {"KERNEL_ARCH_": KERNEL_ARCHITECTURES,
                "KERNEL_MACHINE_": KERNEL_MACHINES}

    def test_both_tables_name_the_same_architectures(self):
        tables = self.tables()
        self.assertEqual(sorted(tables["KERNEL_ARCH_"]),
                         sorted(tables["KERNEL_MACHINE_"]),
                         "an architecture is mapped one way and not the "
                         "other, so a module built for it gets an empty "
                         "value where the other table would have answered")

    def test_the_architectures_the_suite_builds_are_mapped(self):
        tables = self.tables()
        for architecture in ["amd64", "arm64"]:
            for prefix in sorted(tables):
                self.assertIn(architecture, tables[prefix],
                              "%s has no %s" % (prefix, architecture))

    def test_the_two_spellings_differ_where_they_should(self):
        tables = self.tables()
        # The whole reason there are two: they agree on amd64, which is
        # how a specification that confuses them looks correct.
        self.assertEqual(tables["KERNEL_ARCH_"]["amd64"],
                         tables["KERNEL_MACHINE_"]["amd64"])
        self.assertNotEqual(tables["KERNEL_ARCH_"]["arm64"],
                            tables["KERNEL_MACHINE_"]["arm64"])

    def test_an_unmapped_architecture_stops_the_build(self):
        from seine.kernel import kernel_architecture
        from seine.module import module_packaging
        templates, _ = module_packaging()
        # Rather than handing the tree an empty ARCH, which a tree that
        # falls back to uname reads as the builder's own. Refused in the
        # packaging, for a build seine did not decide the architecture
        # of, and in seine, for one it did.
        self.assertIn("$(error seine has no kernel architecture",
                      templates["rules"])
        try:
            kernel_architecture("sparc64")
            self.fail("an architecture seine has never heard of was mapped!")
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

        seine.kernel._filter_series(package, self.workdir)
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
            seine.kernel._restrict_flavour_toml(package, path, "amd64", ["flavour", "featureset"])
            self.assertEqual(self.enabled(path),
                             {"flavour": ["amd64"], "featureset": ["none"]})

class RestrictsToAFlavourThatExists(RestrictsFlavoursInToml):
    def test(self):
        package = self.kernel("                              flavour: nosuch")
        try:
            seine.kernel._restrict_flavour_toml(package, self.defines(), "amd64", ["flavour", "featureset"])
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
        from seine.kernel import kernel_rules
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
            import seine.kernel
            seine.kernel.kernel_rules = lambda: rules._replace(
                content=rules.content + b"\n# moved\n")
            after = (builder.stamp(grafted), builder.stamp(ordinary))
        finally:
            seine.kernel.kernel_rules = kernel_rules
            kernel_rules.cache_clear()

        self.assertNotEqual(before[0], after[0],
                            "changing the rules did not ask for a rebuild")
        # An ordinary rebuild never consults them.
        self.assertEqual(before[1], after[1],
                         "a package that is not a graft was rebuilt for nothing")

# What kbuild does with module.lds, cut down to the two places that name
# it: the rule that links a module and the prerequisite that has to exist
# for that rule to fire.
MODFINAL = """# SPDX-License-Identifier: GPL-2.0-only
PHONY := __modfinal

quiet_cmd_ld_ko_o = LD [M]  $@
      cmd_ld_ko_o =\t\t\t\t\t\t\t\\
\t$(LD) -r $(KBUILD_LDFLAGS)\t\t\t\t\t\\
\t\t-T $(objtree)/scripts/module.lds\t\t\t\\
\t\t-o $@ $(filter %.o, $^)

$(modules): %.ko: %.o %.mod.o $(objtree)/scripts/module.lds FORCE
\t+$(call if_changed,ld_ko_o)
"""

# Debian installs module.lds under arch/<arch>/ and patches kbuild to
# look there too; a tree that moved that rule makes the patch
# inapplicable, and the graft drops it. These check the replacement.
class TheGraftWritesTheModuleLdsPatch(UpstreamKernel):
    def sourcedir(self, series=None):
        import os
        sourcedir = os.path.join(self.workdir, "linux-6.18")
        os.makedirs(os.path.join(sourcedir, "scripts"), exist_ok=True)
        with open(os.path.join(sourcedir, "scripts", "Makefile.modfinal"),
                  "w") as f:
            f.write(MODFINAL)
        patches = os.path.join(sourcedir, "debian", "patches")
        os.makedirs(patches, exist_ok=True)
        if series is not None:
            for name, content in series:
                os.makedirs(os.path.join(patches, os.path.dirname(name)),
                            exist_ok=True)
                with open(os.path.join(patches, name), "w") as f:
                    f.write(content)
            with open(os.path.join(patches, "series"), "w") as f:
                f.writelines("%s\n" % name for name, _ in series)
        return sourcedir

    def written(self, sourcedir):
        import os
        from seine.kernel import MODULE_LDS_PATCH
        path = os.path.join(sourcedir, "debian", "patches", MODULE_LDS_PATCH)
        if os.path.isfile(path) == False:
            return None
        with open(path, "r") as f:
            return f.read()

    def test_the_patch_applies_and_names_both_places(self):
        import os
        import subprocess
        from seine.kernel import MODULE_LDS_PATCH
        sourcedir = self.sourcedir()
        package = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        seine.kernel.module_lds_patch(package, sourcedir)

        patch = self.written(sourcedir)
        self.assertNotEqual(patch, None, "no patch was written")
        # quilt reads the series, so a patch not in it is not applied.
        with open(os.path.join(sourcedir, "debian", "patches", "series")) as f:
            self.assertIn(MODULE_LDS_PATCH, f.read().split())

        subprocess.run(["patch", "-p1", "--batch"], cwd=sourcedir,
                       input=patch.encode(), check=True,
                       stdout=subprocess.DEVNULL)
        with open(os.path.join(sourcedir, "scripts", "Makefile.modfinal")) as f:
            patched = f.read()
        self.assertNotIn("$(objtree)/scripts/module.lds -o", patched)
        self.assertIn("arch/$(SRCARCH)/module.lds", patched,
                      "the patched rule does not look where Debian installs it")
        # Both the rule and the prerequisite, or make still has no rule to
        # make the .ko.
        self.assertEqual(patched.count("$(ARCH_MODULE_LDS)"), 2)

    def test_the_same_tree_writes_the_same_bytes(self):
        package = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        written = []
        for _ in range(2):
            sourcedir = self.sourcedir()
            seine.kernel.module_lds_patch(package, sourcedir)
            written.append(self.written(sourcedir))
        # A timestamp in the header would make every build a new source
        # package, and every source package a rebuilt kernel.
        self.assertEqual(written[0], written[1])

    def test_a_patch_already_in_the_series_wins(self):
        package = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        sourcedir = self.sourcedir(series=[
            ("debian/kbuild-look-for-module.lds-under-arch-directory-too.patch",
             "--- a/scripts/Makefile.modfinal\n"
             "+++ b/scripts/Makefile.modfinal\n")])
        seine.kernel.module_lds_patch(package, sourcedir)
        # Debian's own, kept because it applied -- or one the specification
        # rebased. Either answers for the file, and two patches changing
        # the same rule is one that does not apply.
        self.assertEqual(self.written(sourcedir), None,
                         "a second patch was written for a file already patched")

    def test_a_tree_that_does_not_ask_for_it_is_left_alone(self):
        import os
        package = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        sourcedir = self.sourcedir()
        with open(os.path.join(sourcedir, "scripts", "Makefile.modfinal"),
                  "w") as f:
            f.write("# nothing here names module.lds\n")
        seine.kernel.module_lds_patch(package, sourcedir)
        self.assertEqual(self.written(sourcedir), None)

# What the graft does to a tree is code, and code is in no digest -- so
# the version stands in for it.
class GraftedKernelsAreRebuiltWhenTheGraftChanges(UpstreamKernel):
    def test(self):
        import seine.packages
        builder = self.builder()
        grafted = self.kernel(
            "                              upstream: https://cdn.kernel.org/linux-6.18.43.tar.xz")
        ordinary = self.kernel("                              flavour: amd64")
        before = (builder.stamp(grafted), builder.stamp(ordinary))

        version = seine.kernel.GRAFT_VERSION
        try:
            seine.kernel.GRAFT_VERSION = version + 1
            after = (builder.stamp(grafted), builder.stamp(ordinary))
        finally:
            seine.kernel.GRAFT_VERSION = version

        self.assertNotEqual(before[0], after[0],
                            "bumping the graft version did not ask for a rebuild")
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
        self.assertEqual(seine.kernel._abiname([("linux-headers-6.18+unreleased-common", [])]),
                         "6.18+unreleased")

        # One kernel where the flavours were restricted, and the debug
        # package and the metapackage beside it counted as neither.
        self.assertEqual(seine.kernel._kernel_packages(path, "amd64"),
                         ["linux-image-6.18+unreleased-amd64"])
        # An architecture nothing was restricted on still has all of its.
        self.assertEqual(seine.kernel._kernel_packages(path, "arm64"),
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
            seine.kernel._restrict_flavour_toml(package, path, "amd64",
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
            seine.kernel._toml_set(path, "build", "enable_signed", "false")
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

class AbiSuffixOnlyMeansSomethingForAGraft(UpstreamKernel):
    def test(self):
        try:
            self.kernel("                              abi-suffix: '+acme1'\n"
                        "                              flavour: amd64")
            self.fail("'abi-suffix' was accepted without a graft!")
        except ValueError:
            pass

class AbiSuffixNotAString(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              upstream: git://example.com/bsp.git;rev=deadbeef\n"
                "                              abi-suffix:\n"
                "                                  - acme1")
            self.fail("parsing succeeded for a non-string 'abi-suffix'!")
        except ValueError:
            pass

class AbiSuffixRejectsASingleQuote(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              upstream: git://example.com/bsp.git;rev=deadbeef\n"
                "                              abi-suffix: \"+ac'me1\"")
            self.fail("'abi-suffix' with a single quote in it was accepted!")
        except ValueError:
            pass

# The '[[debianrelease]]' UNRELEASED sits in, with another beside it that
# 'abi-suffix' has no business touching.
DEBIANRELEASE_DEFINES_TOML = """[[debianrelease]]
name_regex = 'UNRELEASED'
abi_version_full = false
abi_suffix = '+unreleased'

[[debianrelease]]
name_regex = 'unstable'
abi_suffix = '+deb13'
"""

class AbiSuffixRewritesTheUnreleasedEntry(UpstreamKernel):
    def defines(self, contents=DEBIANRELEASE_DEFINES_TOML):
        path = os.path.join(self.workdir, "debian", "config", "defines.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(contents)
        return path

    def test(self):
        import tomllib
        package = self.kernel(
            "                              upstream: git://example.com/bsp.git;rev=deadbeef\n"
            "                              abi-suffix: '+acme1'")
        path = self.defines()

        # Twice, so the second pass overwrites the value it wrote rather
        # than inserting a second 'abi_suffix', which is a duplicate key
        # and a file that no longer parses.
        for _ in range(2):
            seine.kernel._set_abi_suffix(package, self.workdir)
            with open(path, "rb") as f:
                defines = tomllib.load(f)
            releases = {r["name_regex"]: r["abi_suffix"]
                       for r in defines["debianrelease"]}
            self.assertEqual(releases, {"UNRELEASED": "+acme1", "unstable": "+deb13"})

class AbiSuffixNeedsAnUnreleasedEntry(AbiSuffixRewritesTheUnreleasedEntry):
    def test(self):
        package = self.kernel(
            "                              upstream: git://example.com/bsp.git;rev=deadbeef\n"
            "                              abi-suffix: '+acme1'")
        self.defines("[[debianrelease]]\nname_regex = 'unstable'\n"
                     "abi_suffix = '+deb13'\n")
        try:
            seine.kernel._set_abi_suffix(package, self.workdir)
            self.fail("'abi-suffix' was rewritten with no UNRELEASED entry to find!")
        except ValueError:
            pass
