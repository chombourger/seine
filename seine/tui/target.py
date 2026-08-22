# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional '/target' support: driving a real device through mtda
# (github.com/siemens/mtda), a gRPC service already exposing
# power/storage/USB/console control. mtda is a system package (like
# python3-guestfs), never a pip dependency of seine -- see
# doctor.check_mtda() for the install-time report; available() below is
# the runtime gate '/target' and its AI tools check before doing
# anything.

_available = None

# A real import, not importlib.util.find_spec: proves mtda.client
# actually loads (catches a broken system install), not just that it is
# on the path. Cached after the first call -- this only needs to run
# once per process.
def available():
    global _available
    if _available is None:
        try:
            import mtda.client  # noqa: F401
            _available = True
        except Exception:
            _available = False
    return _available
