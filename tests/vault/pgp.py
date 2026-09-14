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

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.container import ContainerEngine

IMAGE = "localhost/seine-vault"
TOKEN = "pgp-contract-token"

_BUILT = False


# The image under test, built once per test process from the committed
# sources (Dockerfile compiles the plugin, so no Go is needed here).
def image():
    global _BUILT
    if _BUILT:
        return
    ContainerEngine.run([
        "build", "-t", IMAGE, "-f",
        os.path.join(path_to_sources, "vault-image", "Dockerfile"),
        path_to_sources], check=True)
    _BUILT = True


def forget(name):
    ContainerEngine.run(["container", "rm", "-f", name], check=False)


class PgpContract(avocado.Test):
    """
    :avocado: tags=container
    """
    timeout = 1800

    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to boot the vault image")
        if shutil.which("gpg") is None or shutil.which("gpgv") is None:
            self.cancel("gpg is needed to verify plugin signatures")
        image()
        self.cname = "seine-pgp-test-%s" % os.urandom(4).hex()
        ContainerEngine.check_output([
            "run", "-d", "--name", self.cname,
            "-p", "127.0.0.1::8200",
            "-e", 'BAO_LOCAL_CONFIG={"plugin_directory":"/vault/plugins"}',
            "-e", "BAO_DEV_ROOT_TOKEN_ID=" + TOKEN,
            IMAGE, "server", "-dev", "-dev-listen-address=0.0.0.0:8200"])
        self.addCleanup(forget, self.cname)
        self.addr = self._wait_ready()
        sha = ContainerEngine.check_output(
            ["run", "--rm", IMAGE, "sha256sum",
             "/vault/plugins/seine-pgp.so"]).decode().split()[0]
        self._api("PUT", "/v1/sys/plugins/catalog/secret/seine-pgp",
                  {"sha_256": sha, "command": "seine-pgp.so"})
        self._api("POST", "/v1/sys/mounts/seine-pgp", {"type": "seine-pgp"})

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

    def _gpg_home(self, public_armor):
        home = tempfile.mkdtemp(prefix="seine-pgp-verify-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        subprocess.run(["gpg", "--batch", "--yes", "--homedir", home,
                        "--import"], input=public_armor.encode(),
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
        home = tempfile.mkdtemp(prefix="seine-pgp-gen-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
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
        home = self._gpg_home(self._api(
            "GET", "/v1/seine-pgp/keys/repo/public")["data"]["public_key"])
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


if __name__ == "__main__":
    avocado.main()
