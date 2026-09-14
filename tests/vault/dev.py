#!/usr/bin/env python3

import avocado
import atexit
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time

from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import vault
from seine.container import ContainerEngine
from seine.vault import OpenBaoProvider
from seine.vault.base import VaultNotFound
from seine.vault.dev import DEV_DEFAULTS, DevVault, STALE_AFTER, reap_stale

_STORAGE = None


# Container storage holds files owned by subuids, which plain rmtree
# cannot unlink -- remove it the way 'seine cache clear' says to.
def _rm_storage(path):
    if shutil.which("podman") is not None:
        subprocess.run(["podman", "unshare", "rm", "-rf", path], check=False)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


# One podman storage per test process, so the dev image is pulled once
# no matter how many container tests run.
def storage(test):
    global _STORAGE
    if _STORAGE is None:
        _STORAGE = tempfile.mkdtemp(prefix="seine-vault-test-")
        atexit.register(_rm_storage, _STORAGE)
        os.environ["SEINE_BUILD_DIR"] = os.path.join(_STORAGE, "build")
        try:
            ContainerEngine.check_output(["image", "exists", DevVault.IMAGE])
        except subprocess.CalledProcessError:
            try:
                ContainerEngine.check_output(["pull", DevVault.IMAGE])
            except (OSError, subprocess.CalledProcessError) as e:
                test.cancel("the dev vault image could not be fetched: %s" % e)
    return os.path.join(_STORAGE, "build")


def exists(name):
    try:
        ContainerEngine.check_output(["container", "exists", name])
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def forget(name):
    try:
        ContainerEngine.check_output(["container", "rm", "-f", name])
    except (OSError, subprocess.CalledProcessError):
        pass


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class ContainerSetup(avocado.Test):
    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to start a dev vault")
        vault.clear_secrets()
        build = storage(self)
        self._env = mock.patch.dict(os.environ, {"SEINE_BUILD_DIR": build,
                                                  "SEINE_VAULT_ADDR": "",
                                                  "VAULT_ADDR": ""})
        self._env.start()
        self._devs = []
        self._containers = []

    # Explicit teardown: avocado never runs addCleanup cleanups, so
    # everything external to the process is removed here instead.
    def tearDown(self):
        for dev in getattr(self, "_devs", []):
            dev.close()
        for name in getattr(self, "_containers", []):
            forget(name)
        vault.clear_secrets()
        if getattr(self, "_env", None) is not None:
            self._env.stop()

    def started(self):
        dev = DevVault()
        self._devs.append(dev)
        return dev


class EphemeralLifecycle(ContainerSetup):
    """
    :avocado: tags=container
    """
    timeout = 900

    def test_lazy_empty_teardown(self):
        dev = self.started()
        self.assertFalse(dev.running())
        with self.assertRaises(VaultNotFound):
            dev.kv_read("kv/data/accounts/unknown#hash")
        self.assertTrue(dev.running())
        dev.close()
        self.assertFalse(dev.running())

    def test_miss_seeds_fixed_throwaway_and_warns(self):
        dev = self.started()
        ref = "kv/data/accounts/root#hash"
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            value = dev.kv_read(ref)
        self.assertEqual(value, DEV_DEFAULTS[ref])
        self.assertIn("warning", said.getvalue())
        self.assertIn(ref, said.getvalue())
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertEqual(dev.kv_read(ref), value)
        self.assertEqual(said.getvalue(), "")


class StaleReaping(ContainerSetup):
    """
    :avocado: tags=container
    """
    timeout = 900

    def labeled(self, role, owner, created):
        name = "seine-vault-test-%s-%s" % (role, os.urandom(4).hex())
        ContainerEngine.check_output([
            "run", "-d", "--name", name,
            "--label", "seine.vault=ephemeral",
            "--label", "seine.vault.owner=%s" % owner,
            "--label", "seine.vault.created=%s" % created,
            DevVault.IMAGE, "sleep", "300"])
        self._containers.append(name)
        return name

    def test_dead_owner_reaped_live_kept(self):
        stale = self.labeled("stale", dead_pid(), int(time.time()))
        live = self.labeled("live", os.getpid(), int(time.time()))
        reaped = reap_stale()
        self.assertIn(stale, reaped)
        self.assertNotIn(live, reaped)
        self.assertFalse(exists(stale))
        self.assertTrue(exists(live))

    def test_ancient_reaped_despite_live_owner(self):
        name = self.labeled("ancient", os.getpid(),
                            int(time.time()) - STALE_AFTER - 60)
        self.assertIn(name, reap_stale())
        self.assertFalse(exists(name))


class DevSeeding(avocado.Test):
    REF = "kv/data/accounts/root#hash"

    def seeded(self, existing=None):
        dev = DevVault()
        inner = mock.Mock()
        calls = []

        def fake(method, path, body, token):
            calls.append((method, path, body))
            if method == "GET":
                if existing is None:
                    raise VaultNotFound("nothing there")
                return {"data": {"data": dict(existing)}}
            return {}

        inner._request.side_effect = fake
        dev._inner = inner
        return dev, calls

    def test_known_ref_seeds_the_fixed_value_and_warns(self):
        dev, calls = self.seeded()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertEqual(dev._seed(self.REF), DEV_DEFAULTS[self.REF])
        self.assertIn("warning", said.getvalue())
        self.assertIn(self.REF, said.getvalue())
        puts = [call for call in calls if call[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2], {"data": {"hash": DEV_DEFAULTS[self.REF]}})

    def test_seeding_keeps_fields_already_stored(self):
        dev, calls = self.seeded({"other": "keep"})
        with contextlib.redirect_stderr(io.StringIO()):
            dev._seed(self.REF)
        puts = [call for call in calls if call[0] == "PUT"]
        self.assertEqual(puts[0][2],
                         {"data": {"other": "keep",
                                   "hash": DEV_DEFAULTS[self.REF]}})

    def test_unknown_ref_stays_a_miss(self):
        dev, calls = self.seeded()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            with self.assertRaises(VaultNotFound):
                dev._seed("kv/data/nothing-here#field")
        self.assertEqual(said.getvalue(), "")
        self.assertEqual([call for call in calls if call[0] == "PUT"], [])


class TransitKeyMint(avocado.Test):
    def minted(self, present=()):
        dev = DevVault()
        inner = mock.Mock()
        calls = []

        def fake(method, path, body, token):
            calls.append((method, path, body))
            if method == "GET":
                if path in present:
                    return {"data": {"name": "mykey"}}
                raise VaultNotFound("no key")
            return {}

        inner._request.side_effect = fake
        inner.encrypt.return_value = "vault:v1:ct"
        inner.sign.return_value = "vault:v1:sig"
        dev._inner = inner
        return dev, calls

    def test_missing_key_generated_once_and_warned(self):
        dev, calls = self.minted()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertEqual(dev.encrypt("mykey", b"hello"), "vault:v1:ct")
            self.assertEqual(dev.encrypt("mykey", b"again"), "vault:v1:ct")
        self.assertIn("warning", said.getvalue())
        self.assertIn("mykey", said.getvalue())
        self.assertIn("aes256-gcm96", said.getvalue())
        posts = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith("/v1/transit/keys/mykey"))
        self.assertEqual(posts[0][2], {"type": "aes256-gcm96"})

    def test_signing_key_is_rsa(self):
        dev, calls = self.minted()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dev.sign("sigkey", b"data"), "vault:v1:sig")
        posts = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2], {"type": "rsa-2048"})

    def test_existing_key_neither_recreated_nor_warned(self):
        dev, calls = self.minted(present={"/v1/transit/keys/mykey"})
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            self.assertEqual(dev.encrypt("mykey", b"hello"), "vault:v1:ct")
        self.assertEqual(said.getvalue(), "")
        self.assertEqual([call for call in calls if call[0] == "POST"], [])


class TransitConsumer(ContainerSetup):
    """
    :avocado: tags=container
    """
    timeout = 900

    def test_encrypt_decrypt_roundtrip(self):
        dev = self.started()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            ciphertext = dev.encrypt("seine-test-enc", b"secret-bytes")
        self.assertTrue(ciphertext.startswith("vault:v"))
        self.assertNotIn("secret-bytes", ciphertext)
        self.assertIn("warning", said.getvalue())
        self.assertEqual(dev.decrypt("seine-test-enc", ciphertext), b"secret-bytes")

    def test_sign_verify_roundtrip(self):
        dev = self.started()
        signature = dev.sign("seine-test-signer", b"data-bytes")
        self.assertTrue(dev.verify("seine-test-signer", b"data-bytes", signature))
        self.assertFalse(dev.verify("seine-test-signer", b"other-bytes", signature))


class PgpKeyMint(avocado.Test):
    EPOCH = 1767225600

    def minted(self, present=False):
        dev = DevVault()
        inner = mock.Mock()
        calls = []

        def fake(method, path, body, token):
            calls.append((method, path, body))
            if method == "GET":
                if present:
                    return {"data": {"fingerprint": "AB", "public_key": "armor"}}
                raise VaultNotFound("no key")
            return {"data": {"fingerprint": "AB", "public_key": "armor"}}

        inner._request.side_effect = fake
        inner.pgp_clearsign.return_value = b"signed"
        dev._inner = inner
        return dev, calls

    def test_missing_key_generated_once_and_warned(self):
        dev, calls = self.minted()
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            first = dev.pgp_clearsign("repo", b"data", self.EPOCH)
            second = dev.pgp_clearsign("repo", b"data", self.EPOCH)
        self.assertEqual(first, b"signed")
        self.assertEqual(second, b"signed")
        self.assertIn("warning", said.getvalue())
        self.assertIn("repo", said.getvalue())
        posts = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith("/v1/seine-pgp/keys/repo"))
        self.assertEqual(posts[0][2], {"generate": {}})
        # The epoch predates the minted key, so both signs went out at
        # key birth instead.
        birth = dev._pgp_keys["repo"]
        self.assertGreater(birth, self.EPOCH)
        for call in dev._inner.pgp_clearsign.call_args_list:
            self.assertEqual(call[0][2], birth)

    def test_current_timestamp_passes_through_unwarned(self):
        dev, calls = self.minted()
        future = int(time.time()) + 3600
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            dev.pgp_clearsign("repo", b"data", future)
        dev._inner.pgp_clearsign.assert_called_once_with("repo", b"data", future)
        self.assertNotIn("newer than the build epoch", said.getvalue())

    def test_existing_key_neither_regenerated_nor_warned(self):
        dev, calls = self.minted(present=True)
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            dev.pgp_clearsign("repo", b"data", self.EPOCH)
        self.assertEqual(said.getvalue(), "")
        self.assertEqual([call for call in calls if call[0] == "POST"], [])
        self.assertEqual(dev._pgp_keys["repo"], 0)


class VaultSelection(avocado.Test):
    def test_remote_when_configured(self):
        env = {"SEINE_VAULT_ADDR": "https://vault:8200", "VAULT_ADDR": "",
               "SEINE_VAULT_TOKEN": "tok"}
        with mock.patch.dict(os.environ, env):
            self.assertIsInstance(vault.for_build(), OpenBaoProvider)

    def test_dev_when_not(self):
        env = {"SEINE_VAULT_ADDR": "", "VAULT_ADDR": ""}
        with mock.patch.dict(os.environ, env):
            dev = vault.for_build()
            self.assertIsInstance(dev, DevVault)
            self.assertFalse(dev.running())


class ImagePin(avocado.Test):
    def test_dockerfile_matches_dev_image(self):
        with open(os.path.join(path_to_sources, "vault-image", "Dockerfile")) as f:
            shipped = [line for line in f if line.startswith("FROM ")]
        self.assertEqual(shipped[-1].split()[1], DevVault.IMAGE)


if __name__ == "__main__":
    avocado.main()
