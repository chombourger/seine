#!/usr/bin/env python3

import avocado
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import signing
from seine import vault
from seine.container import ContainerEngine
from seine.signing import Signer, VaultSigner
from seine.vault.dev import CUSTOM_IMAGE, DevVault, ensure_image

TOKEN = "pgp-contract-token"


def forget(name):
    ContainerEngine.run(["container", "rm", "-f", name], check=False)


class GpgChecks:
    def _remember(self, home):
        self._tmpdirs.append(home)
        return home

    def _gpg_home(self, public_armor):
        home = self._remember(tempfile.mkdtemp(prefix="seine-pgp-verify-"))
        if isinstance(public_armor, str):
            public_armor = public_armor.encode()
        subprocess.run(["gpg", "--batch", "--yes", "--homedir", home,
                        "--import"], input=public_armor,
                       capture_output=True, check=True)
        return home

    def _keyring(self, home):
        exported = subprocess.run(
            ["gpg", "--batch", "--yes", "--homedir", home, "--export"],
            capture_output=True, check=True)
        ringpath = os.path.join(home, "ring.gpg")
        with open(ringpath, "wb") as f:
            f.write(exported.stdout)
        return ringpath

    def _gpg_is_happy(self, home, *args):
        verified = subprocess.run(
            ["gpg", "--batch", "--yes", "--homedir", home] + list(args),
            capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    # What apt itself will check: gpgv over a signed-by keyring, exit
    # zero, not just a Good line buried in a failure.
    def _gpgv_is_happy(self, home, *args):
        verified = subprocess.run(
            ["gpgv", "--keyring", self._keyring(home)] + list(args),
            capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def _gpg_keygen(self):
        home = self._remember(tempfile.mkdtemp(prefix="seine-pgp-gen-"))
        params = ("Key-Type: RSA\nKey-Length: 3072\n"
                  "Name-Real: import me\nName-Email: import@example.invalid\n"
                  "Expire-Date: 0\n%no-protection\n%commit\n")
        subprocess.run(["gpg", "--batch", "--yes", "--pinentry-mode",
                        "loopback", "--homedir", home, "--gen-key"],
                       input=params.encode(), capture_output=True, check=True)
        exported = subprocess.run(
            ["gpg", "--batch", "--yes", "--homedir", home, "--armor",
             "--export-secret-keys", "import me"],
            capture_output=True, check=True)
        return exported.stdout.decode()

class PgpContract(avocado.Test, GpgChecks):
    """
    :avocado: tags=container
    """
    timeout = 1800

    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to boot the vault image")
        if shutil.which("gpg") is None or shutil.which("gpgv") is None:
            self.cancel("gpg is needed to verify plugin signatures")
        ensure_image()
        self.cname = None
        self._tmpdirs = []
        self.cname = "seine-pgp-test-%s" % os.urandom(4).hex()
        ContainerEngine.check_output([
            "run", "-d", "--name", self.cname,
            "-p", "127.0.0.1::8200",
            "-e", 'BAO_LOCAL_CONFIG={"plugin_directory":"/vault/plugins"}',
            "-e", "BAO_DEV_ROOT_TOKEN_ID=" + TOKEN,
            CUSTOM_IMAGE, "server", "-dev", "-dev-listen-address=0.0.0.0:8200"])
        self.addr = self._wait_ready()
        sha = ContainerEngine.check_output(
            ["run", "--rm", CUSTOM_IMAGE, "sha256sum",
             "/vault/plugins/seine-pgp.so"]).decode().split()[0]
        self._api("PUT", "/v1/sys/plugins/catalog/secret/seine-pgp",
                  {"sha_256": sha, "command": "seine-pgp.so"})
        self._api("POST", "/v1/sys/mounts/seine-pgp", {"type": "seine-pgp"})

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

    def _sign(self, key, mode, data, stamp):
        payload = self._api("POST", "/v1/seine-pgp/keys/%s/%s" % (key, mode),
                            {"data_base64": base64.b64encode(data).decode(),
                             "timestamp": stamp})["data"]
        name = "signed_data" if mode == "clearsign" else "signature"
        return base64.b64decode(payload[name])


# Host-gpg checks shared by the contract and the signing tests: temp
# homes are tracked per test for teardown.
    def test_unknown_key_is_404(self):
        for path in ("keys/nope/clearsign", "keys/nope/detach-sign"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._api("POST", "/v1/seine-pgp/%s" % path,
                          {"data_base64": "aGk=",
                           "timestamp": "2026-01-01T00:00:00Z"})
            self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("GET", "/v1/seine-pgp/keys/nope/public")
        self.assertEqual(caught.exception.code, 404)

    def test_generate_sign_and_verify(self):
        created = self._api("POST", "/v1/seine-pgp/keys/repo", {"generate": {}})["data"]
        self.assertIn("fingerprint", created)
        public = self._api(
            "GET", "/v1/seine-pgp/keys/repo/public")["data"]
        self.assertEqual(public["fingerprint"], created["fingerprint"])
        home = self._gpg_home(public["public_key"])
        data = b"Release: bookworm main\nDate: Thu, 01 Jan 2026 00:00:00 UTC\n"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        signed = self._sign("repo", "clearsign", data, stamp)
        # No '.asc' suffix: a lone '.asc' argument makes gpg look for
        # detached data beside it and fail, whatever the bytes say.
        path = os.path.join(home, "out.clear")
        with open(path, "wb") as f:
            f.write(signed)
        self._gpg_is_happy(home, "--verify", path)
        self._gpgv_is_happy(home, path)
        signature = self._sign("repo", "detach-sign", data, stamp)
        release = os.path.join(home, "Release")
        with open(release, "wb") as f:
            f.write(data)
        sigpath = os.path.join(home, "Release.gpg")
        with open(sigpath, "wb") as f:
            f.write(signature)
        self._gpg_is_happy(home, "--verify", sigpath, release)
        self._gpgv_is_happy(home, sigpath, release)

    def test_sign_is_deterministic(self):
        self._api("POST", "/v1/seine-pgp/keys/repo", {"generate": {}})
        data = b"Release: bookworm main\n"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for mode in ("clearsign", "detach-sign"):
            self.assertEqual(self._sign("repo", mode, data, stamp),
                             self._sign("repo", mode, data, stamp))

    def test_import_flow(self):
        created = self._api("POST", "/v1/seine-pgp/keys/imported",
                            {"import": {"private_key": self._gpg_keygen()}})["data"]
        self.assertIn("fingerprint", created)
        home = self._gpg_home(self._api(
            "GET", "/v1/seine-pgp/keys/imported/public")["data"]["public_key"])
        data = b"Release: bookworm main\n"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        signature = self._sign("imported", "detach-sign", data, stamp)
        release = os.path.join(home, "Release")
        with open(release, "wb") as f:
            f.write(data)
        sigpath = os.path.join(home, "Release.gpg")
        with open(sigpath, "wb") as f:
            f.write(signature)
        self._gpg_is_happy(home, "--verify", sigpath, release)
        self._gpgv_is_happy(home, sigpath, release)

    def test_garbage_import_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("POST", "/v1/seine-pgp/keys/bad",
                      {"import": {"private_key": "not a key"}})
        self.assertEqual(caught.exception.code, 400)

    def test_timestamp_predating_the_key_is_refused(self):
        self._api("POST", "/v1/seine-pgp/keys/repo", {"generate": {}})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("POST", "/v1/seine-pgp/keys/repo/clearsign",
                      {"data_base64": "aGk=",
                       "timestamp": "2020-01-01T00:00:00Z"})
        self.assertEqual(caught.exception.code, 400)


class VaultSigning(avocado.Test, GpgChecks):
    """
    :avocado: tags=container
    """
    timeout = 900

    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to start a dev vault")
        if shutil.which("gpg") is None or shutil.which("gpgv") is None:
            self.cancel("gpg is needed to verify vault signatures")
        self._tmpdirs = []
        self._env = mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "",
                                                  "VAULT_ADDR": ""})
        self._env.start()
        self._devs = []

    def tearDown(self):
        for dev in getattr(self, "_devs", []):
            dev.close()
        for path in getattr(self, "_tmpdirs", []):
            shutil.rmtree(path, ignore_errors=True)
        vault.clear_secrets()
        if getattr(self, "_env", None) is not None:
            self._env.stop()

    def signed_release(self, dev, key, epoch):
        signer = VaultSigner(dev, key, epoch)
        where = self._remember(tempfile.mkdtemp(prefix="seine-pgp-repo-"))
        release = os.path.join(where, "Release")
        with open(release, "wb") as f:
            f.write(b"Suite: bookworm\nDate: Thu, 01 Jan 2026 00:00:00 UTC\n")
        signer.sign_release(release)
        keyring = os.path.join(where, signer.keyring())
        signer.export(keyring)
        home = self._gpg_home(open(keyring, "rb").read())
        self._gpg_is_happy(home, "--verify",
                            os.path.join(where, "Release.gpg"), release)
        self._gpgv_is_happy(home, os.path.join(where, "Release.gpg"), release)
        self._gpg_is_happy(home, "--verify", os.path.join(where, "InRelease"))
        self._gpgv_is_happy(home, os.path.join(where, "InRelease"))
        # The exported keyring as apt reads it: gpgv straight at the
        # file, no conversion -- armor fails here the way apt did.
        raw = subprocess.run(
            ["gpgv", "--keyring", keyring, os.path.join(where, "InRelease")],
            capture_output=True, text=True)
        self.assertEqual(raw.returncode, 0, raw.stderr)
        return where

    def test_release_signs_end_to_end(self):
        dev = DevVault()
        self._devs.append(dev)
        epoch = int(time.time()) + 7200
        where = self.signed_release(dev, "parity", epoch)
        self.assertTrue(os.path.isfile(os.path.join(where, "parity.gpg")))

    def test_same_epoch_signs_identically(self):
        dev = DevVault()
        self._devs.append(dev)
        epoch = int(time.time()) + 7200
        first = self.signed_release(dev, "parity", epoch)
        second = self.signed_release(dev, "parity", epoch)
        for name in ("Release.gpg", "InRelease"):
            with open(os.path.join(first, name), "rb") as f:
                before = f.read()
            with open(os.path.join(second, name), "rb") as f:
                self.assertEqual(f.read(), before)

    def test_epoch_is_respected(self):
        dev = DevVault()
        self._devs.append(dev)
        epoch = int(time.time()) + 7200
        first = self.signed_release(dev, "parity", epoch)
        second = self.signed_release(dev, "parity", epoch + 3600)
        with open(os.path.join(first, "InRelease"), "rb") as f:
            before = f.read()
        with open(os.path.join(second, "InRelease"), "rb") as f:
            self.assertNotEqual(f.read(), before)

    def test_clearsign_roundtrip(self):
        dev = DevVault()
        self._devs.append(dev)
        epoch = int(time.time()) + 7200
        signer = VaultSigner(dev, "parity", epoch)
        where = self._remember(tempfile.mkdtemp(prefix="seine-pgp-changes-"))
        changes = os.path.join(where, "pkg_1.2_amd64.changes")
        with open(changes, "wb") as f:
            f.write(b"Format: 1.8\nDate: Thu, 01 Jan 2026 00:00:00 UTC\n")
        signer.clearsign(changes)
        keyring = os.path.join(where, "parity.gpg")
        signer.export(keyring)
        home = self._gpg_home(open(keyring, "rb").read())
        self._gpg_is_happy(home, "--verify", changes)


class SignerSelection(avocado.Test):
    def test_vault_prefix_selects_the_vault(self):
        provider = mock.Mock()
        with mock.patch.object(vault, "for_build", return_value=provider) as built:
            signer = signing.signer({"sign_key": "vault:repo"})
            self.assertIsInstance(signer, VaultSigner)
            self.assertEqual(signer.name, "repo")
            built.assert_called_once_with(None)

    def test_spec_defaults_reach_the_vault(self):
        provider = mock.Mock()
        defaults = {"repo": {"private_key": "-- private --"}}
        with mock.patch.object(vault, "for_build", return_value=provider) as built:
            signer = signing.signer({"sign_key": "vault:repo"}, defaults)
            self.assertIsInstance(signer, VaultSigner)
            built.assert_called_once_with(defaults)

    # The machine wins over the spec: option, then environment, then
    # the spec's own default, then nothing.
    def test_the_machine_wins_over_the_spec(self):
        provider = mock.Mock()
        with mock.patch.object(vault, "for_build", return_value=provider):
            self.assertEqual(
                signing.signer({"sign_key": "vault:opt"}, {}, "vault:default").name,
                "opt")
        with mock.patch.dict(os.environ, {"SEINE_SIGN_KEY": "vault:env"}):
            with mock.patch.object(vault, "for_build", return_value=provider):
                self.assertEqual(
                    signing.signer({}, {}, "vault:default").name, "env")
                self.assertEqual(
                    signing.signer({"sign_key": "vault:opt"}, {}, "vault:default").name,
                    "opt")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(vault, "for_build", return_value=provider):
                self.assertEqual(
                    signing.signer({}, {}, "vault:default").name, "default")
                self.assertIsNone(signing.signer({}, {}, None))
                self.assertIsNone(signing.signer({}, {}, ""))

    def test_plain_keys_stay_on_host_gpg(self):
        with mock.patch.object(vault, "for_build") as built:
            self.assertIsInstance(signing.signer({"sign_key": "someone"}), Signer)
            self.assertIsNone(signing.signer({}))
            built.assert_not_called()

    def test_bad_vault_names_are_refused(self):
        with self.assertRaises(ValueError):
            VaultSigner(mock.Mock(), "no/slashes", 0)
        with self.assertRaises(ValueError):
            VaultSigner(mock.Mock(), "", 0)

    # apt reads the exported keyring with gpgv, which takes binary
    # like host gpg's --export -- armor fails the way apt did.
    def test_export_dearmors_the_vault_answer(self):
        import base64
        packets = b"\x99\x04\x00packet-bytes"
        armor = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
                 "Version: test\n\n%s\n=AAAA\n"
                 "-----END PGP PUBLIC KEY BLOCK-----\n"
                 % base64.b64encode(packets).decode())
        provider = mock.Mock()
        provider.pgp_public_key.return_value = armor
        path = os.path.join(self.workdir, "repo.gpg")
        VaultSigner(provider, "repo", 0).export(path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), packets)

    def test_export_refuses_garbage(self):
        provider = mock.Mock()
        provider.pgp_public_key.return_value = "not a key"
        with self.assertRaises(ValueError):
            VaultSigner(provider, "repo", 0).export(
                os.path.join(self.workdir, "repo.gpg"))


if __name__ == "__main__":
    avocado.main()
