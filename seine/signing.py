# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import base64
import binascii
import os
import re
import subprocess

from seine.vault.base import VaultError

# The first key's fingerprint out of gpg's own '--with-colons' output --
# shared by whatever lists a secret key (Signer.fingerprint()) and
# whatever reads a public one (utils.py's feed 'fingerprint' check).
def _first_fingerprint(colons_output):
    for line in colons_output.decode(errors="replace").split("\n"):
        fields = line.split(":")
        if fields[0] == "fpr" and len(fields) > 9 and fields[9]:
            return fields[9]
    return None

# ASCII armor to packets: apt reads the exported keyring with gpgv,
# which takes binary like host gpg's --export, while the vault answers
# armored.
def _dearmor(text):
    lines = (text or "").splitlines()
    try:
        start = next(i for i, line in enumerate(lines)
                     if line.startswith("-----BEGIN "))
        end = next(i for i, line in enumerate(lines)
                   if line.startswith("-----END "))
    except StopIteration:
        raise ValueError("not an armored key")
    body = "".join(line for line in lines[start + 1:end]
                   if line and ":" not in line and not line.startswith("="))
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("not an armored key: %s" % e) from e

# Signs build output using the host's gpg and agent, never inside a
# container. The builder container is too privileged to trust with the
# signing key or its passphrase.
class Signer:
    def __init__(self, key):
        self.key = key
        self._fingerprint = None

    # Resolves the key (short id, long id, fingerprint, or email) to its
    # fingerprint. Called early so a missing key fails before any build
    # output exists.
    def fingerprint(self):
        if self._fingerprint is not None:
            return self._fingerprint

        listed = self._gpg(["--with-colons", "--list-secret-keys", self.key],
                           "no secret key for '%s'" % self.key)
        self._fingerprint = _first_fingerprint(listed)
        if self._fingerprint is None:
            raise ValueError(
                "gpg found a secret key for '%s' but did not say its "
                "fingerprint" % self.key)
        return self._fingerprint

    # Keyring filename, named after the key's short id so it stays
    # identifiable next to other keyrings in the image.
    def keyring(self):
        return "%s.gpg" % self.fingerprint()[-8:]

    # Exports the public key as a keyring file, for apt's 'signed-by'.
    def export(self, path):
        with open(path, "wb") as f:
            f.write(self._gpg(["--export", self.fingerprint()],
                              "cannot export the public key for '%s'" % self.key))

    # Clearsigns a .dsc or .changes file in place. Signs to a temp file
    # first so a failure leaves the original unsigned, not half-signed.
    def clearsign(self, path):
        signed = "%s.seine-signing" % path
        try:
            with open(signed, "wb") as f:
                f.write(self._gpg(["--clearsign", "--output", "-", path],
                                  "cannot sign '%s'" % os.path.basename(path)))
            os.replace(signed, path)
        except:
            if os.path.isfile(signed):
                os.unlink(signed)
            raise

    # Signs a Release file both ways apt expects: detached Release.gpg
    # and combined InRelease.
    def sign_release(self, release):
        where = os.path.dirname(release)
        with open(os.path.join(where, "Release.gpg"), "wb") as f:
            f.write(self._gpg(
                ["--detach-sign", "--armor", "--output", "-", release],
                "cannot sign the repository index"))
        with open(os.path.join(where, "InRelease"), "wb") as f:
            f.write(self._gpg(["--clearsign", "--output", "-", release],
                              "cannot sign the repository index"))

    # --batch/--yes avoid an unattended prompt; --local-user uses the
    # resolved fingerprint so an ambiguous key name can't sign wrong.
    def _gpg(self, args, complaint):
        signing = args[0] in ["--clearsign", "--detach-sign"]
        command = ["gpg", "--batch", "--yes"]
        if signing:
            command += ["--local-user", self.fingerprint()]
        try:
            return subprocess.check_output(command + args,
                                           stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise ValueError(
                "signing was asked for but gpg is not installed on this "
                "machine. seine signs on the host so that no container ever "
                "sees the key.")
        except subprocess.CalledProcessError as e:
            said = (e.stderr or b"").decode(errors="replace").strip()
            raise ValueError("%s\n%s" % (complaint, said))

# 'vault:' names a vault PGP key instead of a gpg one; shared by signer()
# and vendor_signer() now that both take a 'vault_defaults' to pass through.
def _key_to_signer(key, options, vault_defaults):
    if key is None or len(key) == 0:
        return None
    if key.startswith("vault:"):
        return vault_signer(options, key[len("vault:"):], vault_defaults)
    return Signer(key)

# Builds the signer for this build, or None. Key comes from options,
# then SEINE_SIGN_KEY, then the spec's own default (weakest -- the
# spec only suggests, the machine always wins).
def signer(options, vault_defaults=None, sign_key_default=None):
    key = options.get("sign_key") or os.environ.get("SEINE_SIGN_KEY") \
        or sign_key_default
    return _key_to_signer(key, options, vault_defaults)

# Signer for the vendor repository, kept separate from signer() so
# vendor packages (not built by seine) never share a key with ours.
# 'vault_defaults' is the spec's own 'defaults: vault:' -- same dev-only
# fallback mechanism signer() gets, just never wired through before.
def vendor_signer(options, vault_defaults=None):
    key = options.get("vendor_sign_key") or os.environ.get("SEINE_VENDOR_SIGN_KEY")
    return _key_to_signer(key, options, vault_defaults)


# Same shape as Signer, but the private key never leaves the vault:
# bytes go up, armor comes back. Timestamps are pinned to the build
# epoch rather than now, so rebuilds stay byte-identical.
class VaultSigner:
    def __init__(self, provider, name, epoch):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
            raise ValueError("vault pgp key shall name a key, got '%s'" % name)
        self.provider = provider
        self.name = name
        self.epoch = epoch
        self._fingerprint = None

    def fingerprint(self):
        if self._fingerprint is not None:
            return self._fingerprint
        try:
            self._fingerprint = self.provider.pgp_fingerprint(self.name)
        except VaultError as e:
            raise ValueError("no vault pgp key '%s': %s" % (self.name, e)) from e
        return self._fingerprint

    # Named after the vault key rather than a key id: there is no gpg
    # keyring here to take an id from.
    def keyring(self):
        return "%s.gpg" % self.name

    def export(self, path):
        try:
            public = self.provider.pgp_public_key(self.name)
        except VaultError as e:
            raise ValueError(
                "cannot export the public key for '%s': %s" % (self.name, e)) from e
        try:
            packets = _dearmor(public)
        except ValueError as e:
            raise ValueError(
                "cannot export the public key for '%s': %s" % (self.name, e)) from e
        with open(path, "wb") as f:
            f.write(packets)

    def clearsign(self, path):
        signed = "%s.seine-signing" % path
        try:
            with open(path, "rb") as f:
                data = f.read()
            try:
                result = self.provider.pgp_clearsign(self.name, data, self.epoch)
            except VaultError as e:
                raise ValueError(
                    "cannot sign '%s': %s" % (os.path.basename(path), e)) from e
            with open(signed, "wb") as f:
                f.write(result)
            os.replace(signed, path)
        except:
            if os.path.isfile(signed):
                os.unlink(signed)
            raise

    def sign_release(self, release):
        where = os.path.dirname(release)
        with open(release, "rb") as f:
            data = f.read()
        try:
            detached = self.provider.pgp_detach_sign(self.name, data, self.epoch)
            combined = self.provider.pgp_clearsign(self.name, data, self.epoch)
        except VaultError as e:
            raise ValueError(
                "cannot sign the repository index: %s" % e) from e
        with open(os.path.join(where, "Release.gpg"), "wb") as f:
            f.write(detached)
        with open(os.path.join(where, "InRelease"), "wb") as f:
            f.write(combined)


def vault_signer(options, name, vault_defaults=None):
    from seine import vault as _vault
    return VaultSigner(_vault.for_build(vault_defaults), name, _epoch(options))


# Newest spec file mtime, like Image._epoch: editing the spec still
# moves the signatures. Falls back the same way without files.
def _epoch(options):
    files = (options or {}).get("files") or []
    mtimes = [os.path.getmtime(f) for f in files if os.path.isfile(f)]
    if mtimes:
        return int(max(mtimes))
    from seine import packages
    return packages.FALLBACK_EPOCH
