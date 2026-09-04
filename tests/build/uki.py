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
from seine.utils import ContainerEngine

os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
os.environ["SEINE_BUILD_DIR"] = tempfile.mkdtemp(prefix="seine-tests-build-")
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)
atexit.register(shutil.rmtree, os.environ["SEINE_BUILD_DIR"],
                ignore_errors=True)

# None of the specs below set 'distribution: release:', so this is
# where utils.distribution()'s own fallback release deploys to.
DEPLOYED_INITRD = os.path.join(
    ContainerEngine.deploy_root(), "bookworm", "minimal.img")
os.makedirs(os.path.dirname(DEPLOYED_INITRD), exist_ok=True)
open(DEPLOYED_INITRD, "w").close()

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

UKI = """
                packages:
                    - name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              tool: ukify
                              linux-image: linux-image-amd64
                              initrd: minimal.img
%s
"""

class UkiExtension(avocado.Test):
    def test(self):
        build = parse(UKI % "")
        package = build.image.packages[0]
        self.assertEqual(package.uki, True)
        self.assertEqual(package.uki_tool, "ukify")
        self.assertEqual(package.uki_linux_image, "linux-image-amd64")
        self.assertEqual(package.uki_initrd, "minimal.img")
        self.assertEqual(package.uki_cmdline, "")
        self.assertIsNone(package.source)
        self.assertEqual(package.upstream_version, "1")

class UkiTakesACmdline(avocado.Test):
    def test(self):
        build = parse(UKI % """
                              cmdline: "console=ttyS0 ro"
        """)
        package = build.image.packages[0]
        self.assertEqual(package.uki_cmdline, "console=ttyS0 ro")

class MissingTool(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                packages:
                    - name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              linux-image: linux-image-amd64
                              initrd: minimal.img
            """)
        self.assertIn("'extends: uki: tool'", str(refused.exception))

class UnknownTool(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse((UKI % "").replace("tool: ukify", "tool: grub"))
        self.assertIn("'extends: uki: tool'", str(refused.exception))

class MissingLinuxImage(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                packages:
                    - name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              tool: ukify
                              initrd: minimal.img
            """)
        self.assertIn("'extends: uki: linux-image'", str(refused.exception))

class MissingInitrd(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                packages:
                    - name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              tool: ukify
                              linux-image: linux-image-amd64
            """)
        self.assertIn("'extends: uki: initrd'", str(refused.exception))

class ForbiddenCmdlineCharacter(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(UKI % """
                              cmdline: "ro; rm -rf /"
            """)
        self.assertIn("'extends: uki: cmdline'", str(refused.exception))

class SigningNotYetSupported(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(UKI % """
                              signing-key: /a/key.pem
            """)
        self.assertIn("not yet supported", str(refused.exception))

class MissingVersion(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                packages:
                    - name: linux-uki-amd64
                      extends:
                          uki:
                              tool: ukify
                              linux-image: linux-image-amd64
                              initrd: minimal.img
            """)
        self.assertIn("'version' is not set", str(refused.exception))

class SourceIsRefused(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse("""
                packages:
                    - source: apt://linux
                      name: linux-uki-amd64
                      version: "1"
                      extends:
                          uki:
                              tool: ukify
                              linux-image: linux-image-amd64
                              initrd: minimal.img
            """)
        self.assertIn("without a 'source:'", str(refused.exception))

class UnknownSetting(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse(UKI % """
                              bogus: yes
            """)
        self.assertIn("'extends: uki' has no 'bogus' setting", str(refused.exception))

class InitrdNotDeployedYetFailsAtParseTime(avocado.Test):
    def test(self):
        with self.assertRaises(ValueError) as refused:
            parse((UKI % "").replace("minimal.img", "never-built.img"))
        self.assertIn("'never-built.img'", str(refused.exception))
        self.assertIn("not a deployed file", str(refused.exception))
