#!/usr/bin/env python3

import os

from seine.utils import HOST_ARCH

_EXAMPLES = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "examples")

# The example spec whose own architecture matches the host running the
# test -- most callers never touch a container, but there's no reason to
# reach for the amd64 example over the arm64 one when the host is arm64.
# Only these two are native builds today; anything else still gets the
# amd64 one, same as before this existed.
_BOARD_BY_ARCH = {"amd64": "pc-image", "arm64": "rpi4-image"}

def native_image():
    board = _BOARD_BY_ARCH.get(HOST_ARCH, "pc-image")
    return os.path.join(_EXAMPLES, board, "main.yaml")
