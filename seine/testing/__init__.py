# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Test automation: a seine-native YAML front-end compiled onto Robot
# Framework's own running model (robot.running.TestSuite) -- IF/FOR/
# WHILE/TRY/keywords/variables/tags/setup/teardown are Robot's, not
# reimplemented here. seine's own part is the YAML shape (loader.py),
# the keyword libraries that expose seine/mtda actions to it
# (library/), and the CLI/TUI plumbing (runner.py, cmd.py). See
# docs/testing.md for the user-facing model and why robotframework was
# chosen over a bespoke control-flow language.

def available():
    try:
        import robot  # noqa: F401
        return True
    except Exception:
        return False

class Unavailable(Exception):
    pass
