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

import seine.module

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

class ModulesCrossCompileLikeAnythingElse(avocado.Test):
    def builder(self, architecture):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture,
                  "uri": "http://example.com/debian"}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def test_a_module_says_nothing_about_crossing(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels: [apt://linux-headers-amd64]
        """)
        # No opinion of its own, so it gets seine's: cross-compile for
        # an architecture that is not the builder's.
        self.assertEqual(build.image.packages[0].cross, None)

    def test_another_architecture_is_crossed_to(self):
        from seine.packages import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        build = parse_for(other, MODULE % ("""
                              %s-kernels: [apt://linux-headers-%s]
        """ % (other, other)))
        self.assertEqual(
            self.builder(other).cross(build.image.packages[0], other), True)

    def test_the_builders_own_architecture_is_not(self):
        from seine.packages import HOST_ARCH
        build = parse_for(HOST_ARCH, MODULE % ("""
                              %s-kernels: [apt://linux-headers-%s]
        """ % (HOST_ARCH, HOST_ARCH)))
        # Nothing to cross to, so nothing is built to cross with.
        builder = self.builder(HOST_ARCH)
        self.assertEqual(builder.cross(build.image.packages[0], HOST_ARCH),
                         False)
        self.assertEqual(seine.module.cross_headers(builder, build.image.packages), [])

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
        seine.module.check_kbuild(build.image.packages)

    def test_notools_is_refused(self):
        from seine.kernel import MIN_TOOLS
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

        kernels = seine.module.resolved_kernels(builder, module, "amd64", packages)
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
        return seine.module.resolved_kernels(builder, module, "amd64", build.image.packages)

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
        kernels = seine.module.resolved_kernels(builder, build.image.packages[0],
                                                "amd64",
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
        kernels = seine.module.resolved_kernels(
            self.builder(), build.image.packages[0], "amd64")
        self.assertEqual([k.release for k in kernels],
                         ["6.12.101+deb13-amd64"])
        self.assertEqual([k.headers for k in kernels],
                         ["linux-headers-6.12.101+deb13-amd64"])

    def test_a_featureset_is_part_of_the_release(self):
        build = parse_for("amd64", MODULE % """
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-rt-amd64
        """)
        kernels = seine.module.resolved_kernels(
            self.builder(), build.image.packages[0], "amd64")
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
        kernels = seine.module.resolved_kernels(builder, module, "amd64", packages)
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
            seine.module.resolved_kernels(self.builder(), module, "amd64", packages)
            self.fail("a kernel with no ABI yet resolved to something!")
        except ValueError:
            pass

# The packaging seine writes for a tree that carries none.
class GeneratedPackaging(avocado.Test):
    def packaging(self, architecture="amd64", spec=None, resolved=None,
                  carrying=None):
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
        # Packaging the tree came with, as the fetch would leave it.
        for name, content in (carrying or {}).items():
            path = os.path.join(source, "debian", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        seine.module.extend(builder, package, source, 1700000000)
        self.source = source
        written = {}
        for name in ["changelog", "control", "rules", "source/format"]:
            with open(os.path.join(source, "debian", name), "r") as f:
                written[name] = f.read()
        return written

    def test_packaging_the_tree_came_with_is_replaced(self):
        # A tree that packages itself usually packages the module for
        # dkms. seine's packaging is what is built instead, with none of
        # the tree's left behind beside it.
        written = self.packaging(
            resolved={("arm64", "apt://linux-headers-arm64"):
                      "linux-headers-6.12.101+deb13-arm64"},
            carrying={"control": "Source: bcachefs-tools\n",
                      "rules": "#!/usr/bin/make -f\n",
                      "bcachefs-kernel-dkms.dkms": "PACKAGE_NAME=bcachefs\n"})
        self.assertIn("Source: nvidia-open", written["control"])
        self.assertNotIn("bcachefs", written["control"])
        self.assertFalse(os.path.exists(os.path.join(
            self.source, "debian", "bcachefs-kernel-dkms.dkms")))

    def test_one_kernel_under_two_architectures_is_refused(self):
        # A kernel built here is built for one architecture, so naming
        # it under both is the same kernel twice. dh would say so only
        # after that kernel had been built.
        with self.assertRaises(ValueError) as refused:
            self.packaging(spec=MODULE % """
                              modules: [nvidia]
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
                              arm64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
            """)
        self.assertIn("6.12.101+deb13-amd64", str(refused.exception))
        self.assertIn("arm64-kernels", str(refused.exception))

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
        self.assertIn("linux-headers-6.12.101+deb13-amd64 [amd64]", control)
        self.assertIn("linux-headers-6.12.101+deb13-arm64 [arm64]", control)

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
        self.assertIn("KERNEL_ARCH_amd64 = x86_64", rules)
        self.assertIn("KERNEL_ARCH_arm64 = arm64", rules)
        self.assertIn("$(KERNEL_ARCH_$(DEB_HOST_ARCH))", rules)
        # And uname's spelling beside the kernel's, which are the same
        # word on amd64 and different ones on arm64 -- so a value that
        # wants the second and is given the first is right until the day
        # it is built for something else.
        self.assertIn("KERNEL_MACHINE_amd64 = x86_64", rules)
        self.assertIn("KERNEL_MACHINE_arm64 = aarch64", rules)
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

    def test_the_control_file_is_shaped_like_one(self):
        control = self.packaging(spec=MODULE % """
                              build-depends:
                                  - python3
                              runtime-depends:
                                  - firmware-nvidia-gsp
                              amd64-kernels:
                                  - apt://linux-headers-6.12.101+deb13-amd64
        """)["control"]
        # A template that swallows a newline joins two fields into one,
        # which dpkg reads as a field nobody has heard of rather than as
        # the two that were meant. Every line is a field or a
        # continuation of one.
        for line in control.split("\n"):
            if line == "" or line.startswith(" "):
                continue
            self.assertRegex(line, r"^[A-Z][A-Za-z-]*: ",
                             "'%s' is not a field" % line)
        self.assertIn("\nRules-Requires-Root: no\n", control)

    def test_either_headers_are_installed_by_profile(self):
        control = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["control"]
        # One source package, built on machines of either architecture:
        # which headers it needs is not a property of the package but of
        # the build, so dpkg's own 'cross' profile decides rather than
        # this file naming one of them.
        self.assertIn("linux-headers-6.12.101+deb13-arm64 [arm64] <!cross>",
                      control)
        self.assertIn(
            "linux-headers-6.12.101+deb13-arm64-cross:native [arm64] <cross>",
            control)

    def test_the_whole_toolchain_is_named_when_crossing(self):
        rules = self.packaging(resolved={
            ("arm64", "apt://linux-headers-arm64"):
                "linux-headers-6.12.101+deb13-arm64"})["rules"]
        # Not only CC: a tree's own build stage reaches for these by
        # bare name and gets the builder's otherwise, which is a module
        # that compiles and a package that is wrong.
        for tool in ["CC", "CXX", "LD", "AR", "AS", "NM",
                     "OBJCOPY", "OBJDUMP", "RANLIB", "STRIP"]:
            self.assertIn("export %-7s = $(DEB_HOST_GNU_TYPE)-" % tool, rules)
        self.assertIn("ifneq ($(DEB_BUILD_ARCH),$(DEB_HOST_ARCH))", rules)

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
        from seine.module import module_packaging
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
            seine.module.resolved_kernels(builder, build.image.packages[0], "arm64",
                                     build.image.packages)
            self.fail("a metapackage nobody resolved was named anyway!")
        except ValueError as e:
            # Carrying on would strip 'linux-headers-arm64' to 'arm64'
            # and name the modules after a kernel that does not exist.
            self.assertIn("resolved", str(e))

# One kernel, one cross headers package, however many modules are built
# against it: it is a property of the kernel rather than of the module,
# and building it per module would build the same thing repeatedly.
class CrossHeadersAreBuiltOncePerKernel(avocado.Test):
    def builder(self, architecture="arm64"):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": architecture,
                  "uri": "http://example.com/debian"}
        return Builder(distro, {}, BuilderImage(distro, {}))

    def modules(self, architecture, kernels, count=2, cross=True):
        listed = "\n".join("                                  - %s" % kernel
                           for kernel in kernels)
        entries = "\n".join("""
                    - source: git://example.com/driver%d.git;rev=deadbeef
                      name: driver%d
                      version: "1.0"
                      cross: %s
                      extends:
                          module:
                              %s-kernels:
%s""" % (index, index, "true" if cross else "false", architecture, listed)
                            for index in range(count))
        return parse_for(architecture, "\n                packages:" + entries)

    def test_two_modules_on_one_kernel_need_one_package(self):
        build = self.modules("arm64", ["apt://linux-headers-6.12.101+deb13-arm64"])
        headers = seine.module.cross_headers(self.builder(), build.image.packages)
        self.assertEqual([p.name for p in headers],
                         ["linux-headers-6.12.101+deb13-arm64-cross"])

    def test_two_kernels_need_one_each(self):
        build = self.modules("arm64", ["apt://linux-headers-6.12.101+deb13-arm64",
                                       "apt://linux-headers-6.12.101+deb13-rt-arm64"])
        headers = seine.module.cross_headers(self.builder(), build.image.packages)
        self.assertEqual(sorted(p.name for p in headers),
                         ["linux-headers-6.12.101+deb13-arm64-cross",
                          "linux-headers-6.12.101+deb13-rt-arm64-cross"])

    def test_they_are_built_for_the_machine_doing_the_building(self):
        build = self.modules("arm64", ["apt://linux-headers-6.12.101+deb13-arm64"])
        headers = seine.module.cross_headers(self.builder(), build.image.packages)
        # 'host', so the tools in it run where the compiler runs rather
        # than where the modules will.
        self.assertEqual(headers[0].scope, ["host"])
        self.assertEqual(headers[0].cross_kernel.release,
                         "6.12.101+deb13-arm64")

    def test_a_build_that_is_not_crossing_needs_none(self):
        build = self.modules("arm64", ["apt://linux-headers-6.12.101+deb13-arm64"],
                             cross=False)
        # Built on the architecture it is built for -- emulated or
        # otherwise -- the tools it needs are the ones it has.
        self.assertEqual(seine.module.cross_headers(self.builder(), build.image.packages), [])

# The packaging seine writes for a cross headers package: a kernel's
# headers as its own build left them, with kbuild tools rebuilt for the
# machine that will use them.
class GeneratedCrossPackaging(avocado.Test):
    def packaging(self, release="6.18+unreleased-arm64", target="arm64",
                  debs=("linux-headers-6.18+unreleased-arm64_1_arm64.deb",
                        "linux-headers-6.18+unreleased-common_1_all.deb")):
        from seine.packages import Builder
        from seine.module import Kernel
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": target, "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        kernel = Kernel("linux", "linux-headers-%s" % release, release, None)
        package = seine.module._cross_package(kernel, 1)

        source = os.path.join(self.workdir, "linux")
        staged = os.path.join(self.workdir, "debs")
        for path in [source, staged]:
            os.makedirs(path, exist_ok=True)
        for name in debs:
            with open(os.path.join(staged, name), "w") as f:
                f.write("not really a deb")
        seine.module.extend_cross_headers(builder, package, source, 1700000000, staged)

        written = {}
        for name in ["changelog", "control", "rules"]:
            with open(os.path.join(source, "debian", name)) as f:
                written[name] = f.read()
        written["staged"] = sorted(os.listdir(
            os.path.join(source, "debian", "headers")))
        return written

    def test_it_is_built_for_the_machine_that_will_use_it(self):
        written = self.packaging()
        # Architecture: the builder's, since what is new about it is
        # tools that have to run here. ARCH: the kernel's, since what it
        # carries is headers for another machine entirely.
        self.assertIn("Architecture: amd64", written["control"])
        self.assertIn("ARCH     = arm64", written["rules"])

    def test_the_headers_travel_with_the_source(self):
        written = self.packaging()
        # A source package cannot reach outside itself, and what these
        # carry -- .config and Module.symvers -- cannot be made again
        # without building that kernel.
        self.assertEqual(len(written["staged"]), 2)
        self.assertIn("DEBS     = debian/headers", written["rules"])

    def test_the_tools_are_checked_for_being_runnable_here(self):
        written = self.packaging()
        # A build that quietly made them for the target would produce a
        # package whose whole purpose is undone.
        self.assertIn("fixdep cannot be run here", written["rules"])
        self.assertIn("Module.symvers", written["rules"])

    def test_a_kernel_with_no_headers_is_refused(self):
        try:
            self.packaging(debs=())
            self.fail("packaging was written for a kernel with no headers!")
        except ValueError:
            pass

    def test_the_version_is_one_a_native_package_may_have(self):
        changelog = self.packaging()["changelog"]
        version = changelog.split("(")[1].split(")")[0]
        # dpkg reads what follows the last '-' as a Debian revision, and
        # refuses one on a native package -- which a kernel's release is
        # full of.
        self.assertNotIn("-", version)
        self.assertIn("cross", version)

    def test_the_version_moves_with_the_kernel(self):
        first = self.packaging()["changelog"]
        second = self.packaging(release="6.18+unreleased2-arm64")["changelog"]
        # Or a rebuilt kernel would leave headers behind describing the
        # one before it.
        self.assertNotEqual(first.split("\n")[0], second.split("\n")[0])

# A cross headers package is fetched and stamped like anything else, but
# it was made up rather than asked for, so what those come to is worth
# reading back.
class CrossHeadersAreFetchedAndStamped(avocado.Test):
    def parts(self, release="6.18+unreleased-arm64"):
        from seine.packages import Builder
        from seine.module import Kernel
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "arm64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        kernel = Kernel("linux", "linux-headers-%s" % release, release, None)
        return builder, seine.module._cross_package(kernel, 1)

    def test_the_source_is_asked_of_apt_rather_than_named(self):
        builder, package = self.parts()
        command = " ".join(seine.module._fetch_cross_args(builder, package, "arm64")[2].split())
        # Which source a headers package came from is written in the
        # headers package. Guessing 'linux' is right for Debian's kernel
        # and wrong for anybody else's.
        self.assertIn("apt-get source $source=$version", command)
        self.assertIn("^Source:", command)
        self.assertIn("apt-get download linux-headers-6.18+unreleased-arm64:arm64",
                      command)
        # The architecture of the kernel, which is not the architecture
        # this package is built for.
        self.assertIn("dpkg --add-architecture arm64", command)

    def test_the_repository_this_build_fills_is_asked_too(self):
        builder, package = self.parts()
        command = " ".join(seine.module._fetch_cross_args(builder, package, "arm64")[2].split())
        # A kernel built here is in that repository and in no archive.
        # Both indexes: the headers are downloaded, the source they name
        # is fetched.
        self.assertIn("deb [trusted=yes] file:/packages ./", command)
        self.assertIn("deb-src [trusted=yes] file:/packages ./", command)
        # Before apt is asked anything.
        self.assertLess(command.index("sources.list.d"),
                        command.index("apt-get update"))

    def test_a_kernel_that_moved_is_a_different_package(self):
        first, package = self.parts()
        second, other = self.parts(release="6.18+unreleased2-arm64")
        # A grafted kernel rebuilt has a new ABI, and headers left
        # describing the one before it would have modules built against
        # a kernel that is not there.
        self.assertNotEqual(
            os.path.basename(first.stamp(package, "amd64")).rsplit("_", 1)[1],
            os.path.basename(second.stamp(other, "amd64")).rsplit("_", 1)[1])

    def test_it_is_stamped_for_the_machine_that_builds_it(self):
        builder, package = self.parts()
        from seine.packages import HOST_ARCH
        self.assertIn("_%s_" % HOST_ARCH,
                      os.path.basename(builder.stamp(package, HOST_ARCH)))

class MetapackagesAreRecognised(avocado.Test):
    def test(self):
        from seine.module import is_kernel_metapackage, is_built_kernel
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
            seine.module._resolved_headers("apt://linux-headers-arm64", "arm64",
                                      output),
            "linux-headers-6.12.101+deb13-arm64")

    def test_nothing_resolved_says_so(self):
        from seine.packages import Builder
        from seine.sbuild import BuilderImage
        distro = {"source": "debian", "release": "trixie",
                  "architecture": "amd64", "uri": "http://example.com/debian"}
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        try:
            seine.module._resolved_headers("apt://linux-headers-riscv64",
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
        seine.module.resolve_kernels(builder, build.image.packages, HostBootstrap())
        self.assertEqual(order, ["bootstrap", "builder", "asked"],
                         "the builder image was made before the bootstrap it "
                         "is built FROM")
