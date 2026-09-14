#!/usr/bin/env python3

import avocado
import base64
import json
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

TOKEN = "sbsign-contract-token"
EFI_FIXTURE = "/usr/lib/systemd/boot/efi/systemd-bootx64.efi"


def forget(name):
    ContainerEngine.run(["container", "rm", "-f", name], check=False)


class SbsignContract(avocado.Test):
    """
    :avocado: tags=container
    """
    timeout = 1800

    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to boot the vault image")
        for tool in ("sbsign", "sbverify", "openssl"):
            if shutil.which(tool) is None:
                self.cancel("%s is needed to check plugin signatures" % tool)
        if not os.path.isfile(EFI_FIXTURE):
            self.cancel("no PE fixture to sign (%s missing)" % EFI_FIXTURE)
        ensure_image()
        self.cname = None
        self._tmpdirs = []
        self.cname = "seine-sbsign-test-%s" % os.urandom(4).hex()
        ContainerEngine.check_output([
            "run", "-d", "--name", self.cname,
            "-p", "127.0.0.1::8200",
            "-e", 'BAO_LOCAL_CONFIG={"plugin_directory":"/vault/plugins"}',
            "-e", "BAO_DEV_ROOT_TOKEN_ID=" + TOKEN,
            CUSTOM_IMAGE, "server", "-dev", "-dev-listen-address=0.0.0.0:8200"])
        self.addr = self._wait_ready()
        sha = ContainerEngine.check_output(
            ["run", "--rm", CUSTOM_IMAGE, "sha256sum",
             "/vault/plugins/seine-sbsign.so"]).decode().split()[0]
        self._api("PUT", "/v1/sys/plugins/catalog/secret/seine-sbsign",
                  {"sha_256": sha, "command": "seine-sbsign.so"})
        self._api("POST", "/v1/sys/mounts/seine-sbsign", {"type": "seine-sbsign"})

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
        path = tempfile.mkdtemp(prefix="seine-sbsign-")
        self._tmpdirs.append(path)
        return path

    def _keypair(self, where):
        subprocess.run(["openssl", "genrsa", "-out", os.path.join(where, "db.key"),
                        "2048"], capture_output=True, check=True)
        subprocess.run(["openssl", "req", "-new", "-x509",
                        "-key", os.path.join(where, "db.key"),
                        "-out", os.path.join(where, "db.crt"), "-days", "2",
                        "-subj", "/CN=test"], capture_output=True, check=True)
        with open(os.path.join(where, "db.key")) as f:
            key = f.read()
        with open(os.path.join(where, "db.crt")) as f:
            cert = f.read()
        return key, cert

    def _sign(self, key, data, stamp=None):
        body = {"pe_base64": base64.b64encode(data).decode()}
        if stamp is not None:
            body["signing_time"] = stamp
        payload = self._api("POST", "/v1/seine-sbsign/keys/%s/sign" % key,
                            body)["data"]
        return base64.b64decode(payload["signed_pe_base64"])

    def _sbverify(self, cert_path, blob):
        path = os.path.join(self._workdir(), "check.efi")
        with open(path, "wb") as f:
            f.write(blob)
        verified = subprocess.run(["sbverify", "--cert", cert_path, path],
                                  capture_output=True, text=True)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    # The signing time a host sbsign run embedded, so the API can sign
    # at exactly it: same key, same file, same second, no clock race.
    def _host_time(self, signed):
        with open(signed, "rb") as f:
            data = f.read()
        e_lfanew = int.from_bytes(data[0x3c:0x40], "little")
        addr, size = struct.unpack(
            "<II", data[e_lfanew + 4 + 20 + 144:e_lfanew + 4 + 20 + 152])
        cms = os.path.join(self._workdir(), "host.der")
        with open(cms, "wb") as f:
            f.write(data[addr + 8:addr + size])
        listed = subprocess.run(
            ["openssl", "asn1parse", "-inform", "DER", "-in", cms],
            capture_output=True, text=True, check=True)
        signing = False
        for line in listed.stdout.splitlines():
            if "OBJECT" in line and "signingTime" in line:
                signing = True
            elif signing and "UTCTIME" in line and line.strip().endswith("Z"):
                stamp = line.split(":")[-1]
                return "20%s-%s-%sT%s:%s:%sZ" % (
                    stamp[0:2], stamp[2:4], stamp[4:6],
                    stamp[6:8], stamp[8:10], stamp[10:12])
        self.fail("host signature carries no readable signing time")

    def test_unknown_key_is_404(self):
        with open(EFI_FIXTURE, "rb") as f:
            data = f.read()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._sign("nope", data, "2026-01-01T00:00:00Z")
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("GET", "/v1/seine-sbsign/keys/nope/cert")
        self.assertEqual(caught.exception.code, 404)

    def test_generate_flow_verifies(self):
        created = self._api("POST", "/v1/seine-sbsign/keys/db",
                            {"generate": {}})["data"]
        where = self._workdir()
        with open(os.path.join(where, "db.crt"), "w") as f:
            f.write(created["cert_pem"])
        with open(EFI_FIXTURE, "rb") as f:
            data = f.read()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._sbverify(os.path.join(where, "db.crt"),
                       self._sign("db", data, stamp))

    def test_import_matches_host_sbsign_byte_for_byte(self):
        where = self._workdir()
        key, cert = self._keypair(where)
        self._api("POST", "/v1/seine-sbsign/keys/db",
                  {"import": {"key_pem": key, "cert_pem": cert}})
        with open(EFI_FIXTURE, "rb") as f:
            data = f.read()
        direct = os.path.join(where, "direct.efi")
        subprocess.run(["sbsign", "--key", os.path.join(where, "db.key"),
                        "--cert", os.path.join(where, "db.crt"),
                        "--output", direct, EFI_FIXTURE],
                       capture_output=True, check=True)
        via_api = self._sign("db", data, self._host_time(direct))
        with open(direct, "rb") as f:
            self.assertEqual(via_api, f.read())
        self._sbverify(os.path.join(where, "db.crt"), via_api)

    def test_sign_is_deterministic(self):
        where = self._workdir()
        key, cert = self._keypair(where)
        self._api("POST", "/v1/seine-sbsign/keys/db",
                  {"import": {"key_pem": key, "cert_pem": cert}})
        with open(EFI_FIXTURE, "rb") as f:
            data = f.read()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.assertEqual(self._sign("db", data, stamp),
                         self._sign("db", data, stamp))

    def test_garbage_import_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._api("POST", "/v1/seine-sbsign/keys/bad",
                      {"import": {"key_pem": "not a key",
                                  "cert_pem": "not a cert"}})
        self.assertEqual(caught.exception.code, 400)

    def test_resign_is_stable(self):
        where = self._workdir()
        key, cert = self._keypair(where)
        self._api("POST", "/v1/seine-sbsign/keys/db",
                  {"import": {"key_pem": key, "cert_pem": cert}})
        with open(EFI_FIXTURE, "rb") as f:
            data = f.read()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        once = self._sign("db", data, stamp)
        self._sbverify(os.path.join(where, "db.crt"), once)
        # An already-signed binary takes the same path: strip, then
        # sign, ending with one signature that still verifies.
        twice = self._sign("db", once, stamp)
        self._sbverify(os.path.join(where, "db.crt"), twice)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    avocado.main()
