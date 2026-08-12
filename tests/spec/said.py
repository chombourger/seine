#!/usr/bin/env python3

import avocado
import os
import subprocess
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tasks import Failed
from seine.utils import ContainerEngine

# What a failing container command wrote, kept where a person reading the
# error will find it. 'returned non-zero exit status 255' is a number:
# whether podman failed or what it ran did is in the line above it.
class WhatAFailingCommandSaidIsKept(avocado.Test):
    def written(self, text):
        path = os.path.join(self.workdir, "step.log")
        with open(path, "w") as f:
            f.write(text)
        return open(path, "a")

    def test_the_last_lines_are_read_back(self):
        output = self.written("earlier\nError: timed out waiting for file /x/y\n")
        self.assertEqual(ContainerEngine._said(output),
                         "earlier\nError: timed out waiting for file /x/y")

    # Only the tail: a package build's log is hundreds of megabytes and the
    # answer is at the end of it.
    def test_only_the_tail_is_read(self):
        output = self.written("noise\n" * 4000 + "Error: the last word\n")
        said = ContainerEngine._said(output)
        self.assertLessEqual(len(said.splitlines()), ContainerEngine.SAID_LINES)
        self.assertIn("the last word", said)

    # Nothing is capturing, so the command wrote to the terminal and there
    # is nothing to read back.
    def test_an_uncaptured_command_says_nothing(self):
        self.assertIsNone(ContainerEngine._said(None))

    def test_an_empty_log_says_nothing(self):
        self.assertIsNone(ContainerEngine._said(self.written("")))

class AFailedStepRepeatsIt(avocado.Test):
    def error(self, said):
        return subprocess.CalledProcessError(255, ["podman", "exec"], output=said)

    def test_the_message_carries_what_was_said(self):
        failed = Failed([("rootfs", self.error("Error: timed out waiting for file"))], [])
        self.assertIn("rootfs", str(failed))
        self.assertIn("exit status 255", str(failed))
        self.assertIn("timed out waiting for file", str(failed))

    def test_bytes_are_read_as_well(self):
        failed = Failed([("image", self.error(b"Error: no space left on device"))], [])
        self.assertIn("no space left on device", str(failed))

    # An error that said nothing reads as it did before.
    def test_an_error_with_nothing_to_add_is_unchanged(self):
        failed = Failed([("disk", RuntimeError("the image is empty"))], ["image"])
        self.assertIn("disk: the image is empty", str(failed))
        self.assertIn("image did not run", str(failed))

if __name__ == "__main__":
    avocado.main()
