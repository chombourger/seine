# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import hashlib

from seine.vault.base import VaultError, VaultNotFound, VaultProvider
from seine.vault.openbao import OpenBaoProvider

__all__ = ["VaultError", "VaultNotFound", "VaultProvider",
           "OpenBaoProvider", "for_build", "record_secret", "secrets",
           "clear_secrets", "redacted_for_digest"]

_SEEN = []


# Remote-only in v1: unset ADDR fails closed. Phase 2 adds the
# ephemeral dev instance behind this same factory.
def for_build():
    return OpenBaoProvider()


def record_secret(value):
    if value and value not in _SEEN:
        _SEEN.append(value)


def secrets():
    return list(_SEEN)


def clear_secrets():
    del _SEEN[:]


def _marker(value):
    return "<redacted:%s>" % hashlib.sha256(value.encode()).hexdigest()[:8]


# Same redacted-digest form as plan/dump, so raw values never reach
# the build digest while a changed secret still changes it.
def redacted_for_digest(spec):
    if not _SEEN:
        return spec

    def hide(value):
        if isinstance(value, dict):
            return {key: hide(item) for key, item in value.items()}
        if isinstance(value, list):
            return [hide(item) for item in value]
        if isinstance(value, str):
            for secret in _SEEN:
                if secret and secret in value:
                    value = value.replace(secret, _marker(secret))
        return value

    return hide(spec)
