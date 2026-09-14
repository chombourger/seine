#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import io
import lzma
import os
import shutil
import sys
import tarfile
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import kmod_sign

MAGIC = b"~Module signature appended~\n"


# Stands in for seine-kmod's 'sign' endpoint: strips whatever is there
# and appends a fixed, deterministic marker in its place -- enough to
# exercise the .deb repack without needing a real vault container.
class FakeVault:
    def __init__(self):
        self.calls = []

    def kmod_sign(self, key, raw):
        self.calls.append((key, raw))
        i = raw.find(MAGIC)
        if i >= 0:
            raw = raw[:i]
        return raw + b"<signed:%s:%s>" % (
            key.encode(), hashlib.sha256(raw).hexdigest()[:8].encode())


def _ar_member(name, content):
    return [name, "0", "0", "0", "100644", content]

# A minimal but real .deb (control.tar + data.tar) shaped like what
# kmod_sign.py reads. 'module_name' picks the compression: '.ko.xz'
# normally, bare '.ko' for the -dbg package's own build pass.
def _build_deb(path, ko_content, extra_ko_xz=True,
               module_name="./lib/modules/6.1/kernel/drivers/example.ko.xz"):
    data_buf = io.BytesIO()
    data_tar = tarfile.open(fileobj=data_buf, mode="w:", format=tarfile.GNU_FORMAT)

    other = b"not a module\n"
    info = tarfile.TarInfo("./usr/share/doc/example/README")
    info.size = len(other)
    data_tar.addfile(info, io.BytesIO(other))

    module_bytes = (lzma.compress(ko_content, format=lzma.FORMAT_XZ, preset=6)
                    if module_name.endswith(".xz") else ko_content)
    if extra_ko_xz:
        info = tarfile.TarInfo(module_name)
        info.size = len(module_bytes)
        data_tar.addfile(info, io.BytesIO(module_bytes))
    data_tar.close()
    data_tar_bytes = data_buf.getvalue()

    md5sums = "%s  usr/share/doc/example/README\n" % hashlib.md5(other).hexdigest()
    if extra_ko_xz:
        md5sums += ("%s  %s\n" % (hashlib.md5(module_bytes).hexdigest(),
                                 module_name[2:]))

    control_buf = io.BytesIO()
    control_tar = tarfile.open(fileobj=control_buf, mode="w:", format=tarfile.GNU_FORMAT)
    control = b"Package: example\nVersion: 1\n"
    info = tarfile.TarInfo("control")
    info.size = len(control)
    control_tar.addfile(info, io.BytesIO(control))
    info = tarfile.TarInfo("md5sums")
    info.size = len(md5sums.encode())
    control_tar.addfile(info, io.BytesIO(md5sums.encode()))
    control_tar.close()

    kmod_sign._ar_write(path, [
        _ar_member("debian-binary", b"2.0\n"),
        _ar_member("control.tar.xz",
                   lzma.compress(control_buf.getvalue(), preset=6,
                                format=lzma.FORMAT_XZ)),
        _ar_member("data.tar.xz",
                   lzma.compress(data_tar_bytes, preset=6,
                                format=lzma.FORMAT_XZ)),
    ])


class KmodSignFixture(avocado.Test):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="kmod-sign-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.vault = FakeVault()

    def deb(self, name="example.deb", ko_content=b"unsigned module bytes",
           extra_ko_xz=True,
           module_name="./lib/modules/6.1/kernel/drivers/example.ko.xz"):
        path = os.path.join(self.tmpdir, name)
        _build_deb(path, ko_content, extra_ko_xz, module_name)
        return path


class HasModulesFindsAndSkips(KmodSignFixture):
    def test_finds_a_ko_xz(self):
        self.assertTrue(kmod_sign.has_modules(self.deb()))

    def test_finds_a_bare_ko(self):
        self.assertTrue(kmod_sign.has_modules(self.deb(
            module_name="./lib/modules/6.1/kernel/drivers/example.ko")))

    def test_skips_a_deb_without_one(self):
        self.assertFalse(kmod_sign.has_modules(self.deb(extra_ko_xz=False)))


class ResignHandlesABareUncompressedKo(KmodSignFixture):
    # The -dbg package's own modules_install pass ('suffix-y=') ships
    # plain, uncompressed '.ko' -- no '.gz'/'.xz' suffix to key off.
    def test(self):
        path = self.deb(module_name="./lib/modules/6.1/kernel/drivers/example.ko")
        changed = kmod_sign.resign(path, self.vault, "kernel-modules")
        self.assertEqual(changed, {"lib/modules/6.1/kernel/drivers/example.ko"})
        self.assertEqual(self.vault.calls, [("kernel-modules",
                                            b"unsigned module bytes")])


class ResignSignsAndPatchesMd5sums(KmodSignFixture):
    def test(self):
        path = self.deb()
        changed = kmod_sign.resign(path, self.vault, "kernel-modules")
        self.assertEqual(
            changed, {"lib/modules/6.1/kernel/drivers/example.ko.xz"})
        self.assertEqual(self.vault.calls, [("kernel-modules",
                                            b"unsigned module bytes")])

        members = kmod_sign._ar_read(path)
        by_name = {m[0]: m[5] for m in members}
        data_tar = lzma.decompress(by_name["data.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:") as tf:
            ko = lzma.decompress(tf.extractfile(
                "./lib/modules/6.1/kernel/drivers/example.ko.xz").read())
        self.assertTrue(ko.startswith(b"unsigned module bytes<signed:"))

        control_tar = lzma.decompress(by_name["control.tar.xz"])
        with tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:") as tf:
            md5sums = tf.extractfile("md5sums").read().decode()
        expected = hashlib.md5(lzma.compress(
            ko, format=lzma.FORMAT_XZ, preset=6)).hexdigest()
        self.assertIn("%s  lib/modules/6.1/kernel/drivers/example.ko.xz"
                     % expected, md5sums)
        # Untouched: same content, same hash as before.
        self.assertIn("usr/share/doc/example/README", md5sums)


class ResignSkipsADebWithoutModules(KmodSignFixture):
    def test(self):
        path = self.deb(extra_ko_xz=False)
        with open(path, "rb") as f:
            before = f.read()
        changed = kmod_sign.resign(path, self.vault, "kernel-modules")
        self.assertEqual(changed, set())
        self.assertEqual(self.vault.calls, [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)


class ResignForwardsWhateverKbuildLeftAsIs(KmodSignFixture):
    # Stripping a pre-existing signature is the vault's own job; this
    # side never parses signature bytes, just forwards whatever kbuild
    # left, signed or not.
    def test(self):
        signed_by_kbuild = b"unsigned module bytes" + b"\x30\x82fake-cms" + \
            bytes(12) + MAGIC
        path = self.deb(ko_content=signed_by_kbuild)
        kmod_sign.resign(path, self.vault, "kernel-modules")
        self.assertEqual(self.vault.calls, [("kernel-modules", signed_by_kbuild)])


class ResignIsReproducible(KmodSignFixture):
    def test(self):
        path1 = self.deb("one.deb")
        path2 = self.deb("two.deb")
        kmod_sign.resign(path1, self.vault, "kernel-modules")
        kmod_sign.resign(path2, FakeVault(), "kernel-modules")
        with open(path1, "rb") as f:
            b1 = f.read()
        with open(path2, "rb") as f:
            b2 = f.read()
        self.assertEqual(b1, b2)


class PatchChangesRewritesOnlyChangedEntries(KmodSignFixture):
    def test(self):
        path = self.deb()
        kmod_sign.resign(path, self.vault, "kernel-modules")
        with open(path, "rb") as f:
            data = f.read()

        other = os.path.join(self.tmpdir, "other.deb")
        with open(other, "wb") as f:
            f.write(b"unrelated, unchanged .deb bytes\n")

        changes = os.path.join(self.tmpdir, "example.changes")
        with open(changes, "w") as f:
            f.write(
                "Checksums-Sha1:\n"
                " 0000000000000000000000000000000000000000 99 example.deb\n"
                " 1111111111111111111111111111111111111111 32 other.deb\n"
                "Checksums-Sha256:\n"
                " %s 99 example.deb\n"
                " %s 32 other.deb\n"
                "Files:\n"
                " %s 99 kernel optional example.deb\n"
                " %s 32 kernel optional other.deb\n"
                % ("0" * 64, "1" * 64, "0" * 32, "1" * 32))

        kmod_sign.patch_changes(changes, self.tmpdir, ["example.deb"])
        with open(changes) as f:
            patched = f.read()

        self.assertIn(" %s %d example.deb\n"
                      % (hashlib.sha1(data).hexdigest(), len(data)), patched)
        self.assertIn(" %s %d example.deb\n"
                      % (hashlib.sha256(data).hexdigest(), len(data)), patched)
        self.assertIn(" %s %d kernel optional example.deb\n"
                      % (hashlib.md5(data).hexdigest(), len(data)), patched)
        # other.deb was never touched -- its stale-on-purpose lines survive.
        self.assertIn("1111111111111111111111111111111111111111 32 other.deb",
                      patched)
        self.assertIn("1" * 64 + " 32 other.deb", patched)
        self.assertIn("1" * 32 + " 32 kernel optional other.deb", patched)


if __name__ == "__main__":
    avocado.main()
