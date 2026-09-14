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
        build = storage(self)
        env = mock.patch.dict(os.environ, {"SEINE_BUILD_DIR": build,
                                            "SEINE_VAULT_ADDR": "",
                                            "VAULT_ADDR": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(vault.clear_secrets)
        self._devs = []
        self.addCleanup(self._close_all)

    def started(self):
        dev = DevVault()
        self._devs.append(dev)
        return dev

    def _close_all(self):
        for dev in self._devs:
            dev.close()


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
        self.addCleanup(forget, name)
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
            from_line = next(line for line in f if line.startswith("FROM "))
        self.assertEqual(from_line.split()[1], DevVault.IMAGE)


if __name__ == "__main__":
    avocado.main()
