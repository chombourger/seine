# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# Callers program to this, never to a backend directly. Only the
# backend modules know HTTP paths, tokens, or container names.
class VaultError(ValueError):
    pass


class VaultNotFound(VaultError):
    pass


class VaultProvider:
    # A "kv/data/path#field" value, as a string.
    def kv_read(self, ref):
        raise NotImplementedError

    # Bytes up, ciphertext back; keys never leave the vault. Plaintext
    # and signatures travel base64 inside the API, bytes outside it.
    def encrypt(self, key, plaintext):
        raise NotImplementedError

    def decrypt(self, key, ciphertext):
        raise NotImplementedError

    def sign(self, key, data):
        raise NotImplementedError

    # True on a match, False on a mismatch -- never raises for one.
    def verify(self, key, data, signature):
        raise NotImplementedError

    # Apt repository signing through the seine-pgp plugin: named keys
    # via explicit generate/import, bytes in and armor back, public
    # halves out. Timestamps are unix epochs, pinned by the caller.
    def pgp_fingerprint(self, name):
        raise NotImplementedError

    def pgp_public_key(self, name):
        raise NotImplementedError

    def pgp_clearsign(self, name, data, timestamp):
        raise NotImplementedError

    def pgp_detach_sign(self, name, data, timestamp):
        raise NotImplementedError
