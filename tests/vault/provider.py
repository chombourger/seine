#!/usr/bin/env python3

import avocado
import io
import json
import os
import sys
import urllib.error

from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import vault
from seine.vault import OpenBaoProvider, VaultError, VaultNotFound


class FakeReply:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def reply_with(payload):
    return mock.patch("urllib.request.urlopen", return_value=FakeReply(payload))


class RemoteReads(avocado.Test):
    def setUp(self):
        vault.clear_secrets()
        self.addCleanup(vault.clear_secrets)
        self.env = mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "https://vault:8200",
                                                 "SEINE_VAULT_TOKEN": "tok"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_kv_field_reads(self):
        with reply_with({"data": {"data": {"hash": "secret-value"}}}):
            self.assertEqual(OpenBaoProvider().kv_read("kv/data/accounts/root#hash"),
                             "secret-value")

    def test_a_missing_path_fails_closed(self):
        error = urllib.error.HTTPError("https://vault:8200/v1/kv/data/x", 404,
                                       "missing", None, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(VaultNotFound):
                OpenBaoProvider().kv_read("kv/data/x#hash")

    def test_a_missing_field_fails_closed(self):
        with reply_with({"data": {"data": {"other": "1"}}}):
            with self.assertRaises(VaultNotFound):
                OpenBaoProvider().kv_read("kv/data/accounts/root#hash")

    def test_a_ref_without_a_field_is_refused(self):
        with self.assertRaises(VaultError):
            OpenBaoProvider().kv_read("kv/data/accounts/root")

    def test_an_unreachable_vault_fails_closed(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")):
            with self.assertRaises(VaultError):
                OpenBaoProvider().kv_read("kv/data/accounts/root#hash")

    def test_a_miss_never_writes(self):
        calls = []

        def fake(request, *args, **kwargs):
            calls.append((request.get_method(), request.full_url))
            raise urllib.error.HTTPError(request.full_url, 404, "missing",
                                         None, io.BytesIO(b"{}"))

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            with self.assertRaises(VaultNotFound):
                OpenBaoProvider().kv_read("kv/data/x#hash")
        self.assertEqual([method for method, _ in calls], ["GET"])

    def test_no_address_fails_closed(self):
        with mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "",
                                           "VAULT_ADDR": ""}):
            with self.assertRaises(VaultError):
                OpenBaoProvider()


class RemoteAuth(avocado.Test):
    def test_userpass_logs_in_once(self):
        env = {"SEINE_VAULT_ADDR": "https://vault:8200",
               "SEINE_VAULT_AUTH": "userpass:alice:hunter2"}
        seen = []

        def fake(request, *args, **kwargs):
            seen.append(request.full_url)
            if request.full_url.endswith("/v1/auth/userpass/login/alice"):
                return FakeReply({"auth": {"client_token": "derived"}})
            return FakeReply({"data": {"data": {"hash": "v"}}})

        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("SEINE_VAULT_TOKEN", None)
            os.environ.pop("VAULT_TOKEN", None)
            with mock.patch("urllib.request.urlopen", side_effect=fake):
                self.assertEqual(OpenBaoProvider().kv_read("kv/data/a#hash"), "v")
        self.assertTrue(any("/v1/auth/userpass/login/alice" in url for url in seen))

    def test_password_may_come_from_the_environment(self):
        env = {"SEINE_VAULT_ADDR": "https://vault:8200",
               "SEINE_VAULT_AUTH": "userpass:alice",
               "SEINE_VAULT_PASSWORD": "hunter2"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("SEINE_VAULT_TOKEN", None)
            os.environ.pop("VAULT_TOKEN", None)
            with reply_with({"auth": {"client_token": "derived"}}):
                self.assertEqual(OpenBaoProvider()._token, "derived")


if __name__ == "__main__":
    avocado.main()
