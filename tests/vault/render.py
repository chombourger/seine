#!/usr/bin/env python3

import avocado
import os
import sys

from unittest import mock

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import vault
from seine.analyze import spec_digest
from seine.build import BuildCmd
from seine.vault import VaultNotFound


class SpecRendering(avocado.Test):
    def setUp(self):
        vault.clear_secrets()

    def tearDown(self):
        vault.clear_secrets()

    def fake(self, values):
        provider = mock.Mock()
        provider.kv_read.side_effect = lambda ref: values[ref]
        return mock.patch.object(vault, "for_build", return_value=provider)

    def test_a_vault_lookup_renders(self):
        with self.fake({"kv/data/accounts/root#hash": "secret-value"}):
            build = BuildCmd()
            build.loads("playbook:\n  - name: x\n    password: '[[ vault(\"kv/data/accounts/root#hash\") ]]'\n")
            self.assertEqual(build.spec["playbook"][0]["password"], "secret-value")

    def test_a_lookup_failure_names_the_file(self):
        provider = mock.Mock()
        provider.kv_read.side_effect = VaultNotFound("vault has no 'kv/data/a#b'")
        with mock.patch.object(vault, "for_build", return_value=provider):
            build = BuildCmd()
            with self.assertRaises(ValueError) as caught:
                build.loads("password: '[[ vault(\"kv/data/a#b\") ]]'\n")
            self.assertIn("<string>", str(caught.exception))
            self.assertIn("kv/data/a#b", str(caught.exception))

    def test_an_unconfigured_addr_uses_the_dev_instance(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("seine.vault.dev.DevVault") as dev_class:
                dev_class.return_value.kv_read.return_value = "dev-value"
                build = BuildCmd()
                build.loads("password: '[[ vault(\"kv/data/a#b\") ]]'\n")
                self.assertEqual(build.spec["password"], "dev-value")
                dev_class.assert_called_once_with()

    def test_specs_without_vault_refs_start_no_instance(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("seine.vault.dev.DevVault") as dev_class:
                build = BuildCmd()
                build.loads("distribution:\n  release: trixie\n")
                self.assertEqual(build.spec["distribution"]["release"], "trixie")
                dev_class.assert_not_called()

    def test_probing_never_calls_the_vault(self):
        provider = mock.Mock()
        provider.kv_read.return_value = "secret-value"
        path = os.path.join(self.workdir, "main.yaml")
        with open(path, "w") as f:
            f.write("password: '[[ vault(\"kv/data/a#b\") ]]'\n")
        with mock.patch.object(vault, "for_build", return_value=provider):
            build = BuildCmd()
            build.load(path)
            provider.kv_read.assert_called_once()
            self.assertEqual(build.spec["password"], "secret-value")


class RedactionAndDigest(avocado.Test):
    SECRET = "vault-secret-value"

    def setUp(self):
        vault.clear_secrets()

    def tearDown(self):
        vault.clear_secrets()

    def loaded(self, value=None):
        provider = mock.Mock()
        provider.kv_read.return_value = value or self.SECRET
        with mock.patch.object(vault, "for_build", return_value=provider):
            build = BuildCmd()
            build.loads("playbook:\n  - name: x\n    password: '[[ vault(\"kv/data/a#b\") ]]'\n")
            return build

    def test_a_vault_value_is_not_printed(self):
        said = self.loaded().dump(self.loaded().spec)
        self.assertNotIn(self.SECRET, said)
        self.assertIn("<redacted:", said)

    def test_the_build_still_has_it(self):
        self.assertIn(self.SECRET, str(self.loaded().spec))

    def test_the_same_secret_is_stable(self):
        first = self.loaded("same-secret").dump(self.loaded("same-secret").spec)
        vault.clear_secrets()
        second = self.loaded("same-secret").dump(self.loaded("same-secret").spec)
        self.assertEqual(first, second)
        self.assertEqual(spec_digest(self.loaded("same-secret").spec),
                         spec_digest(self.loaded("same-secret").spec))

    def test_a_changed_secret_changes_digest_but_never_raw(self):
        one = self.loaded("first-secret").spec
        digest_one = spec_digest(one)
        vault.clear_secrets()
        two = self.loaded("second-secret").spec
        digest_two = spec_digest(two)
        self.assertNotEqual(digest_one, digest_two)
        self.assertNotIn("first-secret", digest_one)
        self.assertNotIn("second-secret", digest_two)


if __name__ == "__main__":
    avocado.main()
