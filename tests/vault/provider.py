#!/usr/bin/env python3

import avocado
import base64
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
        self.env = mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "https://vault:8200",
                                                 "SEINE_VAULT_TOKEN": "tok"})
        self.env.start()

    def tearDown(self):
        vault.clear_secrets()
        self.env.stop()

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

    def test_vault_cert_verifies_the_connection(self):
        cafile = "/etc/seine/vault-ca.pem"
        with mock.patch.dict(os.environ, {"SEINE_VAULT_CERT": cafile}):
            with mock.patch("ssl.create_default_context") as create_context:
                provider = OpenBaoProvider()
                with reply_with({"data": {"data": {"hash": "v"}}}) as urlopen:
                    provider.kv_read("kv/data/accounts/root#hash")
        create_context.assert_called_once_with(cafile=cafile)
        self.assertEqual(urlopen.call_args.kwargs["context"],
                         create_context.return_value)

    def test_no_vault_cert_leaves_default_verification(self):
        self.assertIsNone(OpenBaoProvider()._ssl_context)


class TransitMapping(avocado.Test):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "https://vault:8200",
                                                 "SEINE_VAULT_TOKEN": "tok"})
        self.env.start()
        self.calls = []

    def tearDown(self):
        vault.clear_secrets()
        self.env.stop()

    def serving(self, payloads):
        def fake(request, *args, **kwargs):
            self.calls.append((request.get_method(), request.full_url,
                               json.loads(request.data.decode() or "{}")))
            for suffix, payload in payloads.items():
                if request.full_url.endswith(suffix):
                    if isinstance(payload, Exception):
                        raise payload
                    return FakeReply(payload)
            raise AssertionError("unexpected vault call %s" % request.full_url)

        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_encrypt_posts_base64(self):
        routes = {"/v1/transit/encrypt/mykey":
                  {"data": {"ciphertext": "vault:v1:abc"}}}
        with self.serving(routes):
            self.assertEqual(OpenBaoProvider().encrypt("mykey", b"hello"),
                             "vault:v1:abc")
        self.assertEqual(self.calls,
                         [("POST", "https://vault:8200/v1/transit/encrypt/mykey",
                           {"plaintext": base64.b64encode(b"hello").decode()})])

    def test_decrypt_returns_bytes(self):
        encoded = base64.b64encode(b"hello").decode()
        routes = {"/v1/transit/decrypt/mykey": {"data": {"plaintext": encoded}}}
        with self.serving(routes):
            self.assertEqual(OpenBaoProvider().decrypt("mykey", "vault:v1:abc"),
                             b"hello")

    def test_sign_and_verify(self):
        routes = {"/v1/transit/sign/mykey": {"data": {"signature": "vault:v1:sig"}},
                  "/v1/transit/verify/mykey": {"data": {"valid": True}}}
        with self.serving(routes):
            provider = OpenBaoProvider()
            signature = provider.sign("mykey", b"hello")
            self.assertEqual(signature, "vault:v1:sig")
            self.assertTrue(provider.verify("mykey", b"hello", signature))

    def test_verify_is_false_on_mismatch(self):
        routes = {"/v1/transit/verify/mykey": {"data": {"valid": False}}}
        with self.serving(routes):
            self.assertFalse(OpenBaoProvider().verify(
                "mykey", b"tampered", "vault:v1:sig"))

    def test_missing_key_fails_closed_without_creating(self):
        missing = urllib.error.HTTPError("https://vault:8200/v1/transit/encrypt/x",
                                         400, "Bad Request", None, io.BytesIO(b"{}"))
        with self.serving({"/v1/transit/encrypt/x": missing}):
            with self.assertRaises(VaultError):
                OpenBaoProvider().encrypt("x", b"hello")
        self.assertEqual([url for _, url, _ in self.calls
                          if "/transit/keys/" in url], [])

    def test_non_bytes_plaintext_is_refused(self):
        with self.serving({}):
            with self.assertRaises(VaultError):
                OpenBaoProvider().encrypt("mykey", "hello")
        self.assertEqual(self.calls, [])


class PgpMapping(avocado.Test):
    EPOCH = 1767225600

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "https://vault:8200",
                                                 "SEINE_VAULT_TOKEN": "tok"})
        self.env.start()
        self.calls = []

    def tearDown(self):
        vault.clear_secrets()
        self.env.stop()

    def serving(self, payloads):
        def fake(request, *args, **kwargs):
            self.calls.append((request.get_method(), request.full_url,
                               json.loads((request.data or b"{}").decode() or "{}")))
            for suffix, payload in payloads.items():
                if request.full_url.endswith(suffix):
                    if isinstance(payload, Exception):
                        raise payload
                    return FakeReply(payload)
            raise AssertionError("unexpected vault call %s" % request.full_url)

        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_clearsign_pins_the_timestamp(self):
        routes = {"/v1/seine-pgp/keys/repo/clearsign":
                  {"data": {"signed_data": base64.b64encode(b"signed").decode()}}}
        with self.serving(routes):
            self.assertEqual(OpenBaoProvider().pgp_clearsign(
                "repo", b"data", self.EPOCH), b"signed")
        self.assertEqual(self.calls,
                         [("POST", "https://vault:8200/v1/seine-pgp/keys/repo/clearsign",
                           {"data_base64": base64.b64encode(b"data").decode(),
                            "timestamp": "2026-01-01T00:00:00Z"})])

    def test_detach_sign(self):
        routes = {"/v1/seine-pgp/keys/repo/detach-sign":
                  {"data": {"signature": base64.b64encode(b"sig").decode()}}}
        with self.serving(routes):
            self.assertEqual(OpenBaoProvider().pgp_detach_sign(
                "repo", b"data", self.EPOCH), b"sig")

    def test_public_and_fingerprint(self):
        routes = {"/v1/seine-pgp/keys/repo/public":
                  {"data": {"fingerprint": "ABCD", "public_key": "armor"}}}
        with self.serving(routes):
            provider = OpenBaoProvider()
            self.assertEqual(provider.pgp_fingerprint("repo"), "ABCD")
            self.assertEqual(provider.pgp_public_key("repo"), "armor")

    def test_unknown_key_fails_closed_without_creating(self):
        missing = urllib.error.HTTPError("https://vault:8200/v1/seine-pgp/keys/x/public",
                                         404, "missing", None, io.BytesIO(b"{}"))
        with self.serving({"/v1/seine-pgp/keys/x/public": missing,
                           "/v1/seine-pgp/keys/x/clearsign": missing}):
            provider = OpenBaoProvider()
            with self.assertRaises(VaultNotFound):
                provider.pgp_public_key("x")
            with self.assertRaises(VaultNotFound):
                provider.pgp_clearsign("x", b"data", self.EPOCH)
        self.assertEqual([body for _, _, body in self.calls
                          if "generate" in body], [])

    def test_bad_arguments_are_refused(self):
        with self.serving({}):
            provider = OpenBaoProvider()
            with self.assertRaises(VaultError):
                provider.pgp_clearsign("repo", "data", self.EPOCH)
            with self.assertRaises(VaultError):
                provider.pgp_clearsign("repo", b"data", "yesterday")
        self.assertEqual(self.calls, [])


class SbsignMapping(avocado.Test):
    EPOCH = 1767225600

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"SEINE_VAULT_ADDR": "https://vault:8200",
                                                 "SEINE_VAULT_TOKEN": "tok"})
        self.env.start()
        self.calls = []

    def tearDown(self):
        vault.clear_secrets()
        self.env.stop()

    def serving(self, payloads):
        def fake(request, *args, **kwargs):
            self.calls.append((request.get_method(), request.full_url,
                               json.loads((request.data or b"{}").decode() or "{}")))
            for suffix, payload in payloads.items():
                if request.full_url.endswith(suffix):
                    if isinstance(payload, Exception):
                        raise payload
                    return FakeReply(payload)
            raise AssertionError("unexpected vault call %s" % request.full_url)

        return mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_sign_pins_the_timestamp(self):
        routes = {"/v1/seine-sbsign/keys/db/sign":
                  {"data": {"signed_pe_base64": base64.b64encode(b"signed").decode()}}}
        with self.serving(routes):
            self.assertEqual(OpenBaoProvider().sbsign_sign(
                "db", b"pe", self.EPOCH), b"signed")
        self.assertEqual(self.calls,
                         [("POST", "https://vault:8200/v1/seine-sbsign/keys/db/sign",
                           {"pe_base64": base64.b64encode(b"pe").decode(),
                            "signing_time": "2026-01-01T00:00:00Z"})])

    def test_cert(self):
        routes = {"/v1/seine-sbsign/keys/db/cert":
                  {"data": {"cert_pem": "cert"}}}
        with self.serving(routes):
            self.assertEqual(OpenBaoProvider().sbsign_cert("db"), "cert")

    def test_unknown_key_fails_closed_without_creating(self):
        missing = urllib.error.HTTPError("https://vault:8200/v1/seine-sbsign/keys/x/sign",
                                         404, "missing", None, io.BytesIO(b"{}"))
        with self.serving({"/v1/seine-sbsign/keys/x/sign": missing}):
            with self.assertRaises(VaultNotFound):
                OpenBaoProvider().sbsign_sign("x", b"pe", self.EPOCH)
        self.assertEqual([body for _, _, body in self.calls
                          if "generate" in body], [])

    def test_bad_arguments_are_refused(self):
        with self.serving({}):
            provider = OpenBaoProvider()
            with self.assertRaises(VaultError):
                provider.sbsign_sign("db", "pe", self.EPOCH)
            with self.assertRaises(VaultError):
                provider.sbsign_sign("db", b"pe", -1)
        self.assertEqual(self.calls, [])


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
