#!/usr/bin/env python3

import avocado
import base64
import glob
import hashlib
import json
import lzma
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.container import ContainerEngine
from seine.vault.dev import CUSTOM_IMAGE, ensure_image

TOKEN = "kmod-contract-token"
MAGIC = b"~Module signature appended~\n"


def forget(name):
    ContainerEngine.run(["container", "rm", "-f", name], check=False)


def sign_file():
    found = glob.glob("/usr/lib/linux-kbuild-*/scripts/sign-file")
    found += glob.glob("/usr/src/linux-headers-*/scripts/sign-file")
    return found[0] if found else None


class KmodContract(avocado.Test):
    """
    :avocado: tags=container
    """
    timeout = 1800

    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to boot the vault image")
        if shutil.which("openssl") is None:
            self.cancel("openssl is needed to check key material")
        if sign_file() is None:
            self.cancel("sign-file is needed to compare against")
        ensure_image()
        self.cname = None
        self._tmpdirs = []
        self.cname = "seine-kmod-test-%s" % os.urandom(4).hex()
        ContainerEngine.check_output([
            "run", "-d", "--name", self.cname,
            "-p", "127.0.0.1::8200",
            "-e", 'BAO_LOCAL_CONFIG={"plugin_directory":"/vault/plugins"}',
            "-e", "BAO_DEV_ROOT_TOKEN_ID=" + TOKEN,
            CUSTOM_IMAGE, "server", "-dev", "-dev-listen-address=0.0.0.0:8200"])
        self.addr = self._wait_ready()
        sha = ContainerEngine.check_output(
            ["run", "--rm", CUSTOM_IMAGE, "sha256sum",
             "/vault/plugins/seine-kmod.so"]).decode().split()[0]
        self._api("PUT", "/v1/sys/plugins/catalog/secret/seine-kmod",
                  {"sha_256": sha, "command": "seine-kmod.so"})
        self._api("POST", "/v1/sys/mounts/seine-kmod", {"type": "seine-kmod"})

    # Explicit teardown: avocado never runs addCleanup cleanups, so a
    # container left here would outlive the test process.
    def tearDown(self):
        for path in getattr(self, "_tmpdirs", []):
            shutil.rmtree(path, ignore_errors=True)
        if getattr(self, "cname", None) is not None:
            forget(self.cname)

    def _wait_ready(self):
        port = int(ContainerEngine.check_output(
            ["port", self.cname, "8200"]).decode().strip().rsplit(":", 1)[1])
        addr = "http://127.0.0.1:%d" % port
        deadline = time.time() + 120
        while True:
            try:
                with urllib.request.urlopen(
                        "%s/v1/sys/seal-status" % addr, timeout=5) as reply:
                    body = json.loads(reply.read().decode() or "{}")
                if body.get("initialized") and not body.get("sealed"):
                    return addr
            except (OSError, ValueError):
                pass
            if time.time() > deadline:
                self.fail("the vault image did not come up in time")
            time.sleep(0.5)

    def _api(self, method, path, body=None):
        request = urllib.request.Request(
            self.addr + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers={"X-Vault-Token": TOKEN})
        with urllib.request.urlopen(request) as reply:
            return json.loads(reply.read().decode() or "{}")

    def _workdir(self):
        path = tempfile.mkdtemp(prefix="seine-kmod-")
        self._tmpdirs.append(path)
        return path

    def _keypair(self, where):
        subprocess.run(["openssl", "genrsa", "-out", os.path.join(where, "key.pem"),
                        "2048"], capture_output=True, check=True)
        subprocess.run(["openssl", "req", "-new", "-x509",
                        "-key", os.path.join(where, "key.pem"),
                        "-out", os.path.join(where, "cert.pem"), "-days", "2",
                        "-subj", "/CN=test"], capture_output=True, check=True)
        with open(os.path.join(where, "key.pem")) as f:
            key = f.read()
        with open(os.path.join(where, "cert.pem")) as f:
            cert = f.read()
        return key, cert

    # A small unsigned module: the host's own, decompressed and
    # stripped of the distribution signature.
    def _module(self, where):
        matches = sorted(glob.glob("/lib/modules/*/kernel/arch/x86/crypto/aesni-intel.ko.xz"))
        if not matches:
            self.cancel("no kernel module fixture on this machine")
        with lzma.open(matches[0], "rb") as f:
            data = f.read()
        if MAGIC in data:
            data = data[:data.index(MAGIC)]
        path = os.path.join(where, "test.ko")
        with open(path, "wb") as f:
            f.write(data)
        return path
    def _sign(self, key, ko):
        with open(ko, "rb") as f:
            data = f.read()
        payload = self._api("POST", "/v1/seine-kmod/keys/%s/sign" % key,
                            {"ko_base64": base64.b64encode(data).decode()})["data"]
        return base64.b64decode(payload["signed_ko_base64"])

    # Splits a signed module the way the kernel does: module, CMS,
    # info struct, marker. Asserts the shape while at it.
    def _split(self, signed):
        self.assertTrue(signed.endswith(MAGIC))
        self.assertEqual(signed.count(MAGIC), 1)
        info = signed[-len(MAGIC) - 12:-len(MAGIC)]
        algo, hash, id_type, signer_len, key_id_len = struct.unpack("BBBBB", info[:5])
        cms_len = struct.unpack(">I", info[8:12])[0]
        self.assertEqual((algo, hash, id_type, signer_len, key_id_len),
                         (0, 0, 2, 0, 0))
        module = signed[:len(signed) - len(MAGIC) - 12 - cms_len]
        cms = signed[len(module):len(module) + cms_len]
        self.assertTrue(cms.startswith(b"\x30\x82"))
        return module, cms

    # The RSA math itself, the way the kernel checks it: decrypt the
    # signature with the signer's public halves and compare digests.
    # Both key sources mint e=65537 keys.
    def _rsa_verified(self, signed, unsigned, cert_path):
        self.assertTrue(signed.startswith(unsigned))
        module, cms = self._split(signed)
        self.assertEqual(module, unsigned)
        modulus = subprocess.run(
            ["openssl", "x509", "-noout", "-modulus", "-in", cert_path],
            capture_output=True, check=True).stdout.decode().strip()
        n = int(modulus.split("=")[1], 16)
        sig = cms[-256:]
        digest_info = pow(int.from_bytes(sig, "big"), 65537, n).to_bytes(256, "big")
        marker = bytes.fromhex("3051300d060960864801650304020305000440")
        self.assertIn(marker, digest_info)
        self.assertEqual(
            digest_info.split(marker)[1][:64], hashlib.sha512(unsigned).digest())

    def test_unknown_key_is_404(self):
        where = self._workdir()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._sign("nope", self._module(where))
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("GET", "/v1/seine-kmod/keys/nope/cert")
        self.assertEqual(caught.exception.code, 404)

    def test_generate_flow_verifies(self):
        where = self._workdir()
        created = self._api("POST", "/v1/seine-kmod/keys/kmod",
                            {"generate": {"key_bits": 2048}})["data"]
        with open(os.path.join(where, "cert.pem"), "w") as f:
            f.write(created["cert_pem"])
        module = self._module(where)
        with open(module, "rb") as f:
            unsigned = f.read()
        signed = self._sign("kmod", module)
        self._rsa_verified(signed, unsigned, os.path.join(where, "cert.pem"))

    def test_import_matches_sign_file_byte_for_byte(self):
        where = self._workdir()
        key, cert = self._keypair(where)
        self._api("POST", "/v1/seine-kmod/keys/kmod",
                  {"import": {"key_pem": key, "cert_pem": cert}})
        module = self._module(where)
        with open(module, "rb") as f:
            unsigned = f.read()
        direct = os.path.join(where, "direct.ko")
        shutil.copy(module, direct)
        subprocess.run([sign_file(), "sha512",
                        os.path.join(where, "key.pem"),
                        os.path.join(where, "cert.pem"), direct],
                       capture_output=True, check=True)
        with open(direct, "rb") as f:
            self.assertEqual(self._sign("kmod", module), f.read())

    def test_sign_is_deterministic(self):
        where = self._workdir()
        self._api("POST", "/v1/seine-kmod/keys/kmod",
                  {"generate": {"key_bits": 2048}})
        module = self._module(where)
        self.assertEqual(self._sign("kmod", module), self._sign("kmod", module))

    def test_signed_input_is_stripped_first(self):
        where = self._workdir()
        self._api("POST", "/v1/seine-kmod/keys/kmod",
                  {"generate": {"key_bits": 2048}})
        matches = sorted(glob.glob("/lib/modules/*/kernel/arch/x86/crypto/aesni-intel.ko.xz"))
        with lzma.open(matches[0], "rb") as f:
            distro_signed = f.read()
        self.assertIn(MAGIC, distro_signed)
        path = os.path.join(where, "distro.ko")
        with open(path, "wb") as f:
            f.write(distro_signed)
        signed = self._sign("kmod", path)
        self.assertEqual(signed.count(MAGIC), 1)
        module, _ = self._split(signed)
        self.assertEqual(module, distro_signed[:distro_signed.index(MAGIC)])

    def test_garbage_import_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("POST", "/v1/seine-kmod/keys/bad",
                      {"import": {"key_pem": "not a key",
                                  "cert_pem": "not a cert"}})
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    avocado.main()
