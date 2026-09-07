# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Test automation: a seine-native YAML front-end compiled onto Robot
# Framework's running model (robot.running.TestSuite). seine's part is
# the YAML shape (loader.py), the keyword libraries (library/), and the
# CLI/TUI plumbing (runner.py, cmd.py). See docs/testing.md.

def available():
    try:
        import robot  # noqa: F401
        return True
    except Exception:
        return False

class Unavailable(Exception):
    pass
