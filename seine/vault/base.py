# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# Callers program to this, never to a backend directly. Only the
# backend modules know HTTP paths, tokens, or container names.
class VaultError(ValueError):
    pass


class VaultNotFound(VaultError):
    pass


class VaultProvider:
    def kv_read(self, ref):
        raise NotImplementedError

    # Crypto lands in phase 4; declared here so callers stay
    # backend-agnostic from the start.
    def encrypt(self, key, plaintext):
        raise NotImplementedError

    def decrypt(self, key, ciphertext):
        raise NotImplementedError

    def sign(self, key, data):
        raise NotImplementedError

    def verify(self, key, data, signature):
        raise NotImplementedError
