# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from seine.vault.base import VaultError, VaultNotFound, VaultProvider


def _env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


# Transit takes base64 in, returns base64 out; bytes outside the API.
def _b64(data):
    if not isinstance(data, bytes):
        raise VaultError("transit expects bytes, got %s" % type(data).__name__)
    return base64.b64encode(data).decode()


def _b64decode(text):
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as e:
        raise VaultError("vault answered with bad base64: %s" % e) from e


def _field(reply, *names):
    try:
        for name in names:
            reply = reply[name]
        return reply
    except (KeyError, TypeError) as e:
        raise VaultError("vault answered unexpectedly: %s" % e) from e


# Remote OpenBao over HTTP. Fail closed: any error raises, never an
# empty value. Token lives in memory only.
class OpenBaoProvider(VaultProvider):
    def __init__(self, addr=None, token=None, auth=None, namespace=None):
        self.addr = (addr or _env("SEINE_VAULT_ADDR", "VAULT_ADDR") or "").rstrip("/")
        if not self.addr:
            raise VaultError("no vault configured: set SEINE_VAULT_ADDR")
        self.namespace = namespace or os.environ.get("SEINE_VAULT_NAMESPACE")
        self._token = token or _env("SEINE_VAULT_TOKEN", "VAULT_TOKEN")
        auth = auth or os.environ.get("SEINE_VAULT_AUTH") or "token"
        if auth != "token":
            self._token = self._login(auth)
        if not self._token:
            raise VaultError("no vault token: set SEINE_VAULT_TOKEN")

    # v1 auth: "token" or "userpass:<user>:<password>". The password may
    # come from SEINE_VAULT_PASSWORD instead, to keep it out of ps output.
    def _login(self, auth):
        method, _, rest = auth.partition(":")
        if method != "userpass":
            raise VaultError("unsupported SEINE_VAULT_AUTH method '%s'" % method)
        user, _, password = rest.partition(":")
        password = password or os.environ.get("SEINE_VAULT_PASSWORD") or ""
        if not user or not password:
            raise VaultError("userpass auth needs a user and a password")
        reply = self._request("POST", "/v1/auth/userpass/login/%s" % user,
                              {"password": password}, token=None)
        token = (reply.get("auth") or {}).get("client_token")
        if not token:
            raise VaultError("vault login did not return a token")
        return token

    # "kv/data/accounts/root#hash" -> ("kv/data/accounts/root", "hash").
    @staticmethod
    def _split(ref):
        path, _, field = (ref or "").partition("#")
        path = path.strip().lstrip("/").removeprefix("v1/")
        if not path or not field:
            raise VaultError("vault ref shall be 'path#field', got '%s'" % ref)
        return path, field

    def kv_read(self, ref):
        path, field = self._split(ref)
        try:
            reply = self._request("GET", "/v1/%s" % path, None, token=self._token)
        except VaultNotFound:
            raise VaultNotFound("vault has no '%s'" % ref) from None
        data = reply.get("data") or {}
        fields = data.get("data") if isinstance(data.get("data"), dict) else data
        if field not in fields:
            raise VaultNotFound("vault has no '%s'" % ref)
        value = fields[field]
        return value if isinstance(value, str) else str(value)

    # Native Transit: bytes up, ciphertext/signature back. Missing keys
    # fail closed here; the dev backend mints its own instead.
    def encrypt(self, key, plaintext):
        reply = self._request(
            "POST", "/v1/transit/encrypt/%s" % urllib.parse.quote(key, safe=""),
            {"plaintext": _b64(plaintext)}, token=self._token)
        return _field(reply, "data", "ciphertext")

    def decrypt(self, key, ciphertext):
        if not isinstance(ciphertext, str):
            raise VaultError("decrypt expects a ciphertext string")
        reply = self._request(
            "POST", "/v1/transit/decrypt/%s" % urllib.parse.quote(key, safe=""),
            {"ciphertext": ciphertext}, token=self._token)
        return _b64decode(_field(reply, "data", "plaintext"))

    def sign(self, key, data):
        reply = self._request(
            "POST", "/v1/transit/sign/%s" % urllib.parse.quote(key, safe=""),
            {"input": _b64(data)}, token=self._token)
        return _field(reply, "data", "signature")

    def verify(self, key, data, signature):
        if not isinstance(signature, str):
            raise VaultError("verify expects a signature string")
        reply = self._request(
            "POST", "/v1/transit/verify/%s" % urllib.parse.quote(key, safe=""),
            {"input": _b64(data), "signature": signature}, token=self._token)
        return bool(_field(reply, "data", "valid"))

    # Apt signing through the seine-pgp plugin. Unknown keys fail
    # closed; only explicit generate/import calls create keys.
    def pgp_fingerprint(self, name):
        reply = self._request(
            "GET", "/v1/seine-pgp/keys/%s/public" % urllib.parse.quote(name, safe=""),
            None, token=self._token)
        return _field(reply, "data", "fingerprint")

    def pgp_public_key(self, name):
        reply = self._request(
            "GET", "/v1/seine-pgp/keys/%s/public" % urllib.parse.quote(name, safe=""),
            None, token=self._token)
        return _field(reply, "data", "public_key")

    def pgp_clearsign(self, name, data, timestamp):
        return self._pgp_sign(name, "clearsign", data, timestamp)

    def pgp_detach_sign(self, name, data, timestamp):
        return self._pgp_sign(name, "detach-sign", data, timestamp)

    def _pgp_sign(self, name, mode, data, timestamp):
        if not isinstance(timestamp, int) or timestamp < 0:
            raise VaultError("pgp signing expects a unix epoch timestamp")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
        reply = self._request(
            "POST", "/v1/seine-pgp/keys/%s/%s"
            % (urllib.parse.quote(name, safe=""), mode),
            {"data_base64": _b64(data), "timestamp": stamp},
            token=self._token)
        key = "signed_data" if mode == "clearsign" else "signature"
        return _b64decode(_field(reply, "data", key))

    # Secure Boot signing through the seine-sbsign plugin. Unknown
    # keys fail closed; only explicit generate/import calls create.
    def sbsign_cert(self, name):
        reply = self._request(
            "GET", "/v1/seine-sbsign/keys/%s/cert" % urllib.parse.quote(name, safe=""),
            None, token=self._token)
        return _field(reply, "data", "cert_pem")

    def sbsign_sign(self, name, pe, timestamp):
        if not isinstance(pe, bytes):
            raise VaultError("secure-boot signing expects bytes, got %s"
                             % type(pe).__name__)
        if not isinstance(timestamp, int) or timestamp < 0:
            raise VaultError("secure-boot signing expects a unix epoch timestamp")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
        reply = self._request(
            "POST", "/v1/seine-sbsign/keys/%s/sign" % urllib.parse.quote(name, safe=""),
            {"pe_base64": base64.b64encode(pe).decode(), "signing_time": stamp},
            token=self._token)
        return _b64decode(_field(reply, "data", "signed_pe_base64"))

    def _request(self, method, url_path, body, token):
        request = urllib.request.Request(
            self.addr + url_path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method)
        if token:
            request.add_header("X-Vault-Token", token)
        if self.namespace:
            request.add_header("X-Vault-Namespace", self.namespace)
        try:
            with urllib.request.urlopen(request) as reply:
                return json.loads(reply.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise VaultNotFound("vault has nothing at '%s'" % url_path) from None
            raise VaultError("vault request failed: %s" % e) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise VaultError("vault request failed: %s" % e) from e
