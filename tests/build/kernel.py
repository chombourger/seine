#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import atexit
import avocado
import collections
import os
import shutil
import sys
import tempfile
import types

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
                              fragments:
                                  - configs/embedded.fragment
                              flavour: arm64
        """)
        package = build.image.packages[0]
        self.assertEqual(package.kernel, True)
        self.assertEqual(package.kernel_flavour, "arm64")
        self.assertEqual(package.kernel_fragments, ["configs/embedded.fragment"])
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
                              bogus:
                                  - typo.fragment
            """)
            self.fail("parsing succeeded for an unknown 'kernel' setting!")
        except ValueError:
            pass

class KernelConfigsParsed(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  rtc-and-lpss:
                                      - CONFIG_RTC_DRV_CMOS=m
                                      - CONFIG_MFD_INTEL_LPSS=m
        """)
        self.assertEqual(build.image.packages[0].kernel_configs,
                         {"rtc-and-lpss": ["CONFIG_RTC_DRV_CMOS=m",
                                           "CONFIG_MFD_INTEL_LPSS=m"]})

# Kernels that set no 'configs:' at all -- most of them -- get an empty
# dictionary rather than 'None', so 'extend' can check its length without
# a special case for "never set".
class KernelConfigsDefaultToEmpty(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              flavour: amd64
        """)
        self.assertEqual(build.image.packages[0].kernel_configs, {})

class KernelConfigsNotADict(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  - CONFIG_RTC_DRV_CMOS=m
            """)
            self.fail("parsing succeeded for a non-dict 'configs'!")
        except ValueError:
            pass

class KernelConfigsGroupNameNotAString(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  123:
                                      - CONFIG_RTC_DRV_CMOS=m
            """)
            self.fail("parsing succeeded for a non-string group name!")
        except ValueError:
            pass

class KernelConfigsGroupNotAList(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  rtc: CONFIG_RTC_DRV_CMOS=m
            """)
            self.fail("parsing succeeded for a group that is not a list!")
        except ValueError:
            pass

class KernelConfigsGroupRejectsEmpty(avocado.Test):
    def test(self):
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  rtc: []
            """)
            self.fail("parsing succeeded for an empty group!")
        except ValueError:
            pass

class KernelConfigsRejectsALineThatIsNotAnAssignment(avocado.Test):
    def test(self):
        # No 'CONFIG_' prefix, no '=', and a comment that does not match
        # kconfig's own "is not set" wording -- none of them is a line
        # 'configs:' understands.
        for line in ["RTC_DRV_CMOS=m", "CONFIG_RTC_DRV_CMOS",
                     "# CONFIG_RTC_DRV_CMOS disabled"]:
            try:
                parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  rtc:
                                      - %s
                """ % line)
                self.fail("'%s' was accepted as a 'configs' line!" % line)
            except ValueError:
                pass

class KernelConfigsAcceptsKconfigsOwnDisabledSyntax(avocado.Test):
    def test(self):
        # Lets a fragment excerpt -- like the one 'config:' is documented
        # with -- be pasted into a group unchanged.
        build = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              configs:
                                  no-media:
                                      - '# CONFIG_MEDIA_SUPPORT is not set'
        """)
        self.assertEqual(build.image.packages[0].kernel_configs,
                         {"no-media": ["# CONFIG_MEDIA_SUPPORT is not set"]})

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
                         ("6.18+unreleased", ["linux-image-6.18+unreleased-amd64"]))
        # An architecture nothing was restricted on still has all of its.
        self.assertEqual(seine.kernel._kernel_packages(path, "arm64"),
                         ("6.18+unreleased",
                          ["linux-image-6.18+unreleased-arm64",
                           "linux-image-6.18+unreleased-cloud-arm64"]))

class CheckFlavour(UpstreamKernel):
    def control(self, contents):
        path = os.path.join(self.workdir, "debian", "control")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(contents)
        return path

class CheckFlavourPassesWhenTheRightKernelIsBuilt(CheckFlavour):
    def test(self):
        self.control(CONTROL)
        package = types.SimpleNamespace(
            source="linux", kernel_featureset="none", kernel_flavour="amd64",
            kernel_derived_flavours=None)
        seine.kernel._check_flavour(package, self.workdir, "amd64")

# A count-only check would miss this: one kernel is left, as asked, and
# it is the wrong one -- 'cloud-arm64', as if 'amd64' had been
# restricted onto this arm64 fixture by mistake.
ONE_WRONG_ARM64_KERNEL = """Source: linux

Package: linux-headers-6.18+unreleased-common
Architecture: all

Package: linux-image-6.18+unreleased-cloud-arm64
Architecture: arm64
"""

class CheckFlavourCatchesTheWrongFlavourAtTheRightCount(CheckFlavour):
    def test(self):
        self.control(ONE_WRONG_ARM64_KERNEL)
        package = types.SimpleNamespace(
            source="linux", kernel_featureset="none", kernel_flavour="arm64",
            kernel_derived_flavours=None)
        try:
            seine.kernel._check_flavour(package, self.workdir, "arm64")
            self.fail("the wrong flavour passed because the count matched!")
        except ValueError:
            pass

# Two kernels, matching 'derived-flavours' asking for two -- but under
# names it never gave, as if a third file's edit had landed instead of
# one of these two's.
TWO_WRONG_DERIVED_KERNELS = """Source: linux

Package: linux-headers-6.18+unreleased-common
Architecture: all

Package: linux-image-6.18+unreleased-rpi4
Architecture: arm64

Package: linux-image-6.18+unreleased-rpi6
Architecture: arm64
"""

class CheckFlavourCatchesAWrongDerivedName(CheckFlavour):
    def test(self):
        self.control(TWO_WRONG_DERIVED_KERNELS)
        package = types.SimpleNamespace(
            source="linux", kernel_featureset="none",
            kernel_derived_flavours={"arm64": {"rpi4": [], "rpi5": []}},
            kernel_derived_flavours_built={"rpi4", "rpi5"})
        try:
            seine.kernel._check_flavour(package, self.workdir, "arm64")
            self.fail("'rpi5' was never built, and the count alone hid it!")
        except ValueError:
            pass

TWO_RIGHT_DERIVED_KERNELS = """Source: linux

Package: linux-headers-6.18+unreleased-common
Architecture: all

Package: linux-image-6.18+unreleased-rpi4
Architecture: arm64

Package: linux-image-6.18+unreleased-rpi5
Architecture: arm64
"""

class CheckFlavourPassesWhenEveryDerivedNameIsThere(CheckFlavour):
    def test(self):
        self.control(TWO_RIGHT_DERIVED_KERNELS)
        package = types.SimpleNamespace(
            source="linux", kernel_featureset="none",
            kernel_derived_flavours={"arm64": {"rpi4": [], "rpi5": []}},
            kernel_derived_flavours_built={"rpi4", "rpi5"})
        seine.kernel._check_flavour(package, self.workdir, "arm64")

class CheckFlavourPassesWhenARenamedKernelIsBuilt(CheckFlavour):
    def test(self):
        # A single-entry 'derived-flavours' is what a plain rename
        # looks like now: one base, one name.
        self.control(ONE_WRONG_ARM64_KERNEL.replace("cloud-arm64", "rpi4"))
        package = types.SimpleNamespace(
            source="linux", kernel_featureset="none",
            kernel_derived_flavours={"arm64": {"rpi4": []}},
            kernel_derived_flavours_built={"rpi4"})
        seine.kernel._check_flavour(package, self.workdir, "arm64")

class CheckFlavourCatchesARenameThatDidNotTakeEffect(CheckFlavour):
    def test(self):
        # 'derived-flavours' asked for 'rpi4'; this control still has
        # Debian's own 'arm64', as if '_add_derived_flavours' had never
        # run.
        self.control(ONE_WRONG_ARM64_KERNEL.replace("cloud-arm64", "arm64"))
        package = types.SimpleNamespace(
            source="linux", kernel_featureset="none",
            kernel_derived_flavours={"arm64": {"rpi4": []}},
            kernel_derived_flavours_built={"rpi4"})
        try:
            seine.kernel._check_flavour(package, self.workdir, "arm64")
            self.fail("a rename that never happened passed the check!")
        except ValueError:
            pass

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

class DerivedFlavoursNotADict(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              derived-flavours:\n"
                "                                  - pc")
            self.fail("parsing succeeded for a non-dict 'derived-flavours'!")
        except ValueError:
            pass

# Both are accepted together rather than one rejecting the other: an
# architecture file's 'defaults' commonly gives every kernel a plain
# 'flavour' of its own, for a module built against it, which a package
# also carrying 'derived-flavours' inherits whether it means to use it
# or not.
class DerivedFlavoursIsAcceptedBesideFlavour(UpstreamKernel):
    def test(self):
        package = self.kernel(
            "                              flavour: amd64\n"
            "                              derived-flavours:\n"
            "                                  amd64:\n"
            "                                      pc: []")
        self.assertEqual(package.kernel_flavour, "amd64")
        self.assertEqual(package.kernel_derived_flavours, {"amd64": {"pc": []}})

class DerivedFlavoursTakePrecedenceOverFlavour(UpstreamKernel):
    def test(self):
        import tomllib
        path = os.path.join(self.workdir, "debian", "config", "amd64",
                            "defines.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(DEFINES_TOML)

        package = types.SimpleNamespace(
            source="linux", kernel_flavour="amd64",
            kernel_derived_flavours={"amd64": {"pc": []}})

        # What 'extend()' itself decides between the two -- restricting
        # to plain 'amd64' would leave nothing for '_add_derived_flavours'
        # to find, since the block it looks for is the one this call
        # renames rather than one 'flavour' alone would keep.
        if package.kernel_derived_flavours:
            seine.kernel._add_derived_flavours(package, self.workdir, "amd64")
        else:
            seine.kernel._restrict_flavour(package, self.workdir, "amd64")

        with open(path, "rb") as f:
            defines = tomllib.load(f)
        flavours = {e["name"]: e for e in defines["flavour"]}
        self.assertEqual(flavours["amd64"].get("enable"), False)
        self.assertNotEqual(flavours["pc"].get("enable"), False)

class DerivedFlavoursBaseNameRejectsASingleQuote(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              derived-flavours:\n"
                "                                  \"amd6'4\":\n"
                "                                      pc: []")
            self.fail("a base flavour name with a single quote was accepted!")
        except ValueError:
            pass

class DerivedFlavoursInnerNotADict(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              derived-flavours:\n"
                "                                  amd64:\n"
                "                                      - pc")
            self.fail("a non-dict of derived names was accepted!")
        except ValueError:
            pass

class DerivedFlavoursNameRejectsASingleQuote(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              derived-flavours:\n"
                "                                  amd64:\n"
                "                                      \"p'c\": []")
            self.fail("a derived flavour name with a single quote was accepted!")
        except ValueError:
            pass

class DerivedFlavoursFragmentsNotAList(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              derived-flavours:\n"
                "                                  amd64:\n"
                "                                      pc: configs/pc.fragment")
            self.fail("a non-list of fragments was accepted!")
        except ValueError:
            pass

class DerivedFlavoursFragmentsMayBeEmpty(UpstreamKernel):
    def test(self):
        package = self.kernel(
            "                              derived-flavours:\n"
            "                                  amd64:\n"
            "                                      pc:")
        self.assertEqual(package.kernel_derived_flavours, {"amd64": {"pc": []}})

class DerivedFlavoursBaseNeedsAtLeastOneName(UpstreamKernel):
    def test(self):
        try:
            self.kernel(
                "                              derived-flavours:\n"
                "                                  amd64: {}")
            self.fail("a base naming no derived flavour was accepted!")
        except ValueError:
            pass

class DerivedFlavoursWorksWithoutAGraft(UpstreamKernel):
    def test(self):
        package = self.kernel(
            "                              derived-flavours:\n"
            "                                  amd64:\n"
            "                                      pc: []")
        self.assertEqual(package.kernel_derived_flavours, {"amd64": {"pc": []}})
        self.assertEqual(package.kernel_upstream, None)

# A fragment for '_add_derived_flavours' to read the content of -- unlike
# the parse-level tests above, these are applied to a real defines.toml.
class DerivedFlavourFragment(UpstreamKernel):
    def fragment(self, name, content):
        path = os.path.join(self.workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def defines(self, contents=DEFINES_TOML):
        path = os.path.join(self.workdir, "debian", "config", "amd64",
                            "defines.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(contents)
        return path

class DerivedFlavoursAddsABlockPerName(DerivedFlavourFragment):
    def test(self):
        import tomllib
        pc = self.fragment("pc.fragment", "CONFIG_PC=y\n")
        nas = self.fragment("nas.fragment", "CONFIG_NAS=y\n")
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"amd64": {"pc": [pc], "nas": [nas]}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        with open(path, "rb") as f:
            defines = tomllib.load(f)
        flavours = {e["name"]: e for e in defines["flavour"]}
        self.assertEqual(flavours["amd64"].get("enable"), False,
                         "the base 'amd64' flavour was not disabled")
        self.assertEqual(flavours["pc"]["build"]["config"], ["amd64/pc.config"])
        self.assertEqual(flavours["nas"]["build"]["config"], ["amd64/nas.config"])
        # What each derived flavour's own fragment carried, concatenated
        # into the file '[flavour.build] config' points at.
        with open(os.path.join(self.workdir, "debian", "config", "amd64",
                               "pc.config")) as f:
            self.assertIn("CONFIG_PC=y", f.read())

class DerivedFlavoursAppendToAnExistingBuildTable(DerivedFlavourFragment):
    def test(self):
        import tomllib
        edge = self.fragment("edge.fragment", "CONFIG_EDGE=y\n")
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"cloud-amd64": {"cloud-edge": [edge]}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        with open(path, "rb") as f:
            defines = tomllib.load(f)
        flavours = {e["name"]: e for e in defines["flavour"]}
        # 'cloud-amd64' already had a '[flavour.build] config', so the
        # derived one's own fragment is appended to it rather than
        # opening a second table -- which duplicate-key TOML would
        # refuse to parse at all.
        self.assertEqual(flavours["cloud-edge"]["build"]["config"],
                         ["config.cloud", "amd64/cloud-edge.config"])

class DerivedFlavoursFromTwoBasesAtOnce(DerivedFlavourFragment):
    def test(self):
        import tomllib
        pc = self.fragment("pc.fragment", "CONFIG_PC=y\n")
        hyper = self.fragment("hyper.fragment", "CONFIG_HYPER=y\n")
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"amd64": {"pc": [pc]},
                                     "cloud-amd64": {"hyper": [hyper]}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        with open(path, "rb") as f:
            defines = tomllib.load(f)
        flavours = {e["name"]: e for e in defines["flavour"]}
        self.assertEqual(flavours["amd64"].get("enable"), False)
        self.assertEqual(flavours["cloud-amd64"].get("enable"), False)
        self.assertEqual(flavours["pc"]["build"]["config"], ["amd64/pc.config"])
        self.assertEqual(flavours["hyper"]["build"]["config"],
                         ["config.cloud", "amd64/hyper.config"])

# One dictionary naming bases from two architectures at once, the way
# examples/slim-flavours.yml does -- 'arm64' means nothing to an amd64
# defines.toml, and has to be silently passed over rather than reported
# as a flavour that does not exist.
class DerivedFlavoursIgnoreAnotherArchitecturesEntries(DerivedFlavourFragment):
    def test(self):
        import tomllib
        pc = self.fragment("pc.fragment", "CONFIG_PC=y\n")
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"amd64": {"pc": [pc]},
                                     "arm64": {"rpi4": []}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        self.assertEqual(package.kernel_derived_flavours_built, {"pc"})
        with open(path, "rb") as f:
            defines = tomllib.load(f)
        flavours = {e["name"]: e for e in defines["flavour"]}
        self.assertEqual(flavours["amd64"].get("enable"), False)
        self.assertEqual(flavours["pc"]["build"]["config"], ["amd64/pc.config"])
        self.assertNotIn("rpi4", flavours)

# 'pc-lite' derives from 'pc', which is not an original Debian flavour
# at all -- only something this same setting is itself deriving from
# 'amd64' -- so materializing it has to happen in the right order.
class DerivedFlavoursChainFromAnotherDerivedFlavour(DerivedFlavourFragment):
    def test(self):
        import tomllib
        pc = self.fragment("pc.fragment", "CONFIG_PC=y\n")
        lite = self.fragment("lite.fragment", "CONFIG_LITE=y\n")
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"amd64": {"pc": [pc]},
                                     "pc": {"pc-lite": [lite]}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        with open(path, "rb") as f:
            defines = tomllib.load(f)
        flavours = {e["name"]: e for e in defines["flavour"]}
        self.assertEqual(flavours["amd64"].get("enable"), False,
                         "the original base was not disabled")
        # 'pc' ships too: naming it as someone else's base does not take
        # it away, only 'amd64' -- what it was itself derived from --
        # loses to it.
        self.assertNotEqual(flavours["pc"].get("enable"), False,
                            "an intermediate derived flavour was disabled")
        self.assertEqual(flavours["pc"]["build"]["config"], ["amd64/pc.config"])
        # 'pc-lite' carries both fragments, in the order they were
        # derived: 'pc's own config array, inherited by the copy, with
        # 'pc-lite's own appended after it.
        self.assertEqual(flavours["pc-lite"]["build"]["config"],
                         ["amd64/pc.config", "amd64/pc-lite.config"])

# The same chain, with the entries written in the opposite order --
# 'pc-lite' before the 'amd64' base it needs -- to prove sequencing is
# genuinely topological rather than an accident of dict order. Python
# dicts preserve insertion order, so a loop that only followed that
# order would materialize 'pc-lite' before 'pc' existed to copy.
class DerivedFlavoursSequencingDoesNotDependOnDictOrder(DerivedFlavourFragment):
    def test(self):
        import tomllib
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"pc": {"pc-lite": []},
                                     "amd64": {"pc": []}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        with open(path, "rb") as f:
            defines = tomllib.load(f)
        names = {e["name"] for e in defines["flavour"] if e.get("enable", True)}
        self.assertEqual(names, {"pc", "pc-lite"})

# Neither end of 'a -> b -> a' resolves to anything real, so the cycle
# materializes nothing; '_check_flavour' catches it the same way it
# would a base naming no architecture at all.
class DerivedFlavoursCatchACycle(DerivedFlavourFragment):
    def test(self):
        import tomllib
        package = types.SimpleNamespace(
            source="linux",
            kernel_derived_flavours={"a": {"b": []}, "b": {"a": []}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        self.assertEqual(package.kernel_derived_flavours_built, set())
        with open(path, "rb") as f:
            defines = tomllib.load(f)
        # Nothing was touched: 'amd64' and 'cloud-amd64' are exactly as
        # DEFINES_TOML wrote them, still enabled.
        self.assertEqual(
            [e.get("enable", True) for e in defines["flavour"]], [True, True])

# A base naming an architecture this call is not for -- or a typo, which
# looks the same from here -- materializes nothing rather than failing:
# only '_check_flavour', comparing what this left built against
# debian/control, can tell a genuine mismatch from a board file composed
# without the one that derives the base it names.
class DerivedFlavoursNeedsThatFlavourToExist(DerivedFlavourFragment):
    def test(self):
        import tomllib
        package = types.SimpleNamespace(
            source="linux", kernel_derived_flavours={"nosuch": {"pc": []}})
        path = self.defines()

        seine.kernel._add_derived_flavours(package, self.workdir, "amd64")

        self.assertEqual(package.kernel_derived_flavours_built, set())
        with open(path, "rb") as f:
            defines = tomllib.load(f)
        self.assertEqual(
            [e.get("enable", True) for e in defines["flavour"]], [True, True])

class DerivedFlavoursNeedTheTomlFormat(avocado.Test):
    def test(self):
        package = types.SimpleNamespace(
            source="linux", kernel_derived_flavours={"amd64": {"pc": []}})
        try:
            seine.kernel._add_derived_flavours(package, self.workdir, "amd64")
            self.fail("'derived-flavours' ran with no defines.toml to edit!")
        except ValueError:
            pass

class DerivedFlavoursCountInTheDigest(UpstreamKernel):
    def test(self):
        builder = self.builder()
        stamps = []
        # The revision held fixed: what has to move the digest is the
        # derived flavour's name, not a side effect of some other
        # setting moving with it.
        for entries in ["amd64:\n                                      pc: []",
                        "amd64:\n                                      pc: []\n"
                        "                                  cloud-amd64:\n"
                        "                                      hyper: []"]:
            build = parse("""
                packages:
                    - source: apt://linux
                      revision: fixed1
                      extends:
                          kernel:
                              derived-flavours:
                                  %s
        """ % entries)
            stamps.append(builder.stamp(build.image.packages[0]))
        self.assertNotEqual(stamps[0], stamps[1],
                            "adding a derived flavour did not ask for a rebuild")

class DerivedFlavoursCountTheBaseInTheDigest(UpstreamKernel):
    def test(self):
        # 'pc' derived from 'amd64' and 'pc' derived from 'cloud-amd64'
        # are different kernels -- the name alone is not enough to tell
        # a rebuild is needed.
        builder = self.builder()
        stamps = []
        for base in ["amd64", "cloud-amd64"]:
            build = parse("""
                packages:
                    - source: apt://linux
                      revision: fixed1
                      extends:
                          kernel:
                              derived-flavours:
                                  %s:
                                      pc: []
        """ % base)
            stamps.append(builder.stamp(build.image.packages[0]))
        self.assertNotEqual(stamps[0], stamps[1],
                            "changing what 'pc' derives from did not ask for a rebuild")

class KernelConfigsCountInTheDigest(UpstreamKernel):
    def test(self):
        builder = self.builder()
        plain = self.kernel("                              flavour: amd64")
        extended = self.kernel(
            "                              flavour: amd64\n"
            "                              configs:\n"
            "                                  magic-sysrq:\n"
            "                                      - CONFIG_MAGIC_SYSRQ=n")
        self.assertNotEqual(builder.stamp(plain), builder.stamp(extended),
                            "adding a 'configs' group did not ask for a rebuild")

class KernelConfigsGroupContentCountsInTheDigest(UpstreamKernel):
    def test(self):
        # Same group name, different line -- the group changed what it
        # asks for, not what it is called, and the digest has to tell.
        builder = self.builder()
        stamps = []
        for line in ["CONFIG_MAGIC_SYSRQ=n", "CONFIG_MAGIC_SYSRQ=y"]:
            package = self.kernel(
                "                              flavour: amd64\n"
                "                              configs:\n"
                "                                  magic-sysrq:\n"
                "                                      - %s" % line)
            stamps.append(builder.stamp(package))
        self.assertNotEqual(stamps[0], stamps[1],
                            "changing a 'configs' line did not ask for a rebuild")

class KernelConfigsGroupOrderCountsInTheDigest(UpstreamKernel):
    def test(self):
        # Unlike a patch list, this is not a set: two groups touching
        # the same symbol settle it by which is written last.
        builder = self.builder()
        stamps = []
        for entries in ["first:\n                                      - CONFIG_X=y\n"
                        "                                  second:\n"
                        "                                      - CONFIG_X=n",
                        "second:\n                                      - CONFIG_X=n\n"
                        "                                  first:\n"
                        "                                      - CONFIG_X=y"]:
            package = self.kernel(
                "                              flavour: amd64\n"
                "                              configs:\n"
                "                                  %s" % entries)
            stamps.append(builder.stamp(package))
        self.assertNotEqual(stamps[0], stamps[1],
                            "reordering 'configs' groups did not ask for a rebuild")

class DerivedFlavoursDefaultTheirOwnRevision(avocado.Test):
    def revision(self, base, names):
        entries = "\n".join("                                      %s: []" % n
                            for n in names)
        build = parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              derived-flavours:
                                  %s:
%s
        """ % (base, entries))
        return build.image.packages[0].revision

    def test(self):
        # Two files rebuilding 'linux' under derived flavours of their
        # own -- one per architecture, so each is its own build -- would
        # otherwise both default to 'mod1' and publish the same
        # '<source>_<version>+mod1.dsc' from two different debian/ trees.
        self.assertEqual(self.revision("arm64", ["rpi4", "rpi5"]), "rpi4.rpi5")
        self.assertNotEqual(self.revision("arm64", ["rpi4", "rpi5"]),
                            self.revision("amd64", ["pc", "nas"]))

class DerivedFlavoursRevisionIsStillOverridable(avocado.Test):
    def test(self):
        build = parse("""
                packages:
                    - source: apt://linux
                      revision: acme3
                      extends:
                          kernel:
                              derived-flavours:
                                  arm64:
                                      rpi4: []
                                      rpi5: []
        """)
        self.assertEqual(build.image.packages[0].revision, "acme3")

class DerivedFlavoursRevisionRejectsAHyphen(avocado.Test):
    def test(self):
        # One name with a hyphen is enough on its own -- the joined
        # result of several would be too, but there is no need for two
        # names to prove a '-' cannot survive into a Debian revision.
        try:
            parse("""
                packages:
                    - source: apt://linux
                      extends:
                          kernel:
                              derived-flavours:
                                  arm64:
                                      cloud-edge: []
                                      rpi5: []
            """)
            self.fail("a derived flavour name with a hyphen defaulted a revision anyway!")
        except ValueError:
            pass

        build = parse("""
                packages:
                    - source: apt://linux
                      revision: edge1
                      extends:
                          kernel:
                              derived-flavours:
                                  arm64:
                                      cloud-edge: []
                                      rpi5: []
        """)
        self.assertEqual(build.image.packages[0].revision, "edge1")

# '_write_configs' is a function of its own, the same reason
# '_add_derived_flavours' is: pointed at a fixture directly rather than
# only reachable through the whole of 'extend', which would also need a
# builder image to regenerate debian/control.
class KernelConfigsWrittenAsFragment(avocado.Test):
    def config(self, content=""):
        path = os.path.join(self.workdir, "config")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_a_group_is_appended_with_its_name_as_header(self):
        package = types.SimpleNamespace(kernel_configs={
            "rtc-and-lpss": ["CONFIG_RTC_DRV_CMOS=m",
                             "CONFIG_MFD_INTEL_LPSS=m"]})
        path = self.config("CONFIG_ALREADY_THERE=y\n")
        seine.kernel._write_configs(package, path)
        with open(path, "r") as f:
            written = f.read()
        self.assertEqual(written,
                         "CONFIG_ALREADY_THERE=y\n"
                         "\n# rtc-and-lpss, added by seine\n"
                         "CONFIG_RTC_DRV_CMOS=m\n"
                         "CONFIG_MFD_INTEL_LPSS=m\n")

    def test_an_assignment_to_n_becomes_a_kconfig_comment(self):
        package = types.SimpleNamespace(
            kernel_configs={"rtc": ["CONFIG_RTC_DRV_CMOS=m",
                                    "CONFIG_RTC_DRV_RX6110=n"]})
        path = self.config()
        seine.kernel._write_configs(package, path)
        with open(path, "r") as f:
            written = f.read()
        self.assertIn("CONFIG_RTC_DRV_CMOS=m\n", written)
        self.assertIn("# CONFIG_RTC_DRV_RX6110 is not set\n", written)
        self.assertNotIn("CONFIG_RTC_DRV_RX6110=n", written)

    def test_kconfigs_own_disabled_syntax_passes_through_unchanged(self):
        package = types.SimpleNamespace(
            kernel_configs={"no-media": ["# CONFIG_MEDIA_SUPPORT is not set"]})
        path = self.config()
        seine.kernel._write_configs(package, path)
        with open(path, "r") as f:
            self.assertIn("# CONFIG_MEDIA_SUPPORT is not set\n", f.read())

    def test_groups_are_written_in_the_order_they_were_named(self):
        # A later group is meant to win over an earlier one touching the
        # same symbol, which only holds if the file carries them in that
        # order -- kconfig itself has no other way to tell which of two
        # settings for one symbol is meant to stick.
        package = types.SimpleNamespace(kernel_configs=collections.OrderedDict(
            [("first", ["CONFIG_X=y"]), ("second", ["CONFIG_X=n"])]))
        path = self.config()
        seine.kernel._write_configs(package, path)
        with open(path, "r") as f:
            written = f.read()
        self.assertLess(written.index("first"), written.index("second"))

    def test_no_groups_leaves_the_file_untouched(self):
        package = types.SimpleNamespace(kernel_configs={})
        path = self.config("CONFIG_ALREADY_THERE=y\n")
        seine.kernel._write_configs(package, path)
        with open(path, "r") as f:
            self.assertEqual(f.read(), "CONFIG_ALREADY_THERE=y\n")
