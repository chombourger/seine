# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import json
import os
import urllib.error
import urllib.request

from seine.vault.base import VaultError, VaultNotFound, VaultProvider


def _env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


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
