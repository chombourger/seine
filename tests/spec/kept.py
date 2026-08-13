#!/usr/bin/env python3

import avocado
import io
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import ContainerEngine

# A container removed on the way out of a failed build takes with it what
# podman recorded about the execs that ran in it. SEINE_KEEP_DEAD_CONTAINERS
# leaves them where a post-mortem can read them, and seine says which.
class DeadContainersAreKeptWhenAsked(avocado.Test):
    def setUp(self):
        self.removed = []
        self.run = ContainerEngine.run
        ContainerEngine.run = lambda cmd, check=False: self.removed.append(cmd)
        ContainerEngine._kept = []
        self.environment = dict(os.environ)

    def tearDown(self):
        ContainerEngine.run = self.run
        # Nothing left for the handler discard() registers to print when
        # this process ends.
        ContainerEngine._kept = []
        os.environ.clear()
        os.environ.update(self.environment)

    def asked(self, value):
        if value is None:
            os.environ.pop("SEINE_KEEP_DEAD_CONTAINERS", None)
        else:
            os.environ["SEINE_KEEP_DEAD_CONTAINERS"] = value

    def reported(self):
        output = io.StringIO()
        stdout = sys.stdout
        sys.stdout = output
        try:
            ContainerEngine._report_kept()
        finally:
            sys.stdout = stdout
        return output.getvalue()

    def test_unset_removes_as_it_always_did(self):
        self.asked(None)
        ContainerEngine.discard("dead", force=True, failed=True)
        self.assertEqual(self.removed, [["container", "rm", "-f", "dead"]])
        self.assertEqual(ContainerEngine._kept, [])

    def test_set_keeps_what_a_failure_left(self):
        self.asked("1")
        ContainerEngine.discard("dead", force=True, failed=True)
        self.assertEqual(self.removed, [])
        self.assertEqual(ContainerEngine._kept, ["dead"])

    # A step that succeeded is finished with its container and that
    # container is evidence of nothing. Keeping those too would fill a
    # disk with every build.
    def test_a_container_that_did_its_job_still_goes(self):
        self.asked("1")
        ContainerEngine.discard("alive", failed=False)
        self.assertEqual(self.removed, [["container", "rm", "alive"]])
        self.assertEqual(ContainerEngine._kept, [])

    def test_off_is_off(self):
        for value in ["", "0", "no"]:
            ContainerEngine._kept = []
            self.removed = []
            self.asked(value)
            ContainerEngine.discard("dead", failed=True)
            self.assertEqual(ContainerEngine._kept, [], value)

    # podman is run against a storage of seine's own, so the command has
    # to name it: a plain 'podman rm' looks in the default one and reports
    # no such container.
    def test_the_report_says_how_to_remove_them(self):
        self.asked("1")
        ContainerEngine.discard(b"one", force=True, failed=True)
        ContainerEngine.discard("two", force=True, failed=True)
        report = self.reported()
        # A container id read back from podman is bytes; what is printed
        # has to be the id, not "b'one'".
        self.assertIn("\n  one\n", report)
        self.assertIn("\n  two\n", report)
        self.assertIn("--root %s" % ContainerEngine.root(), report)
        self.assertIn("rm -f one two", report)

    def test_nothing_kept_says_nothing(self):
        self.assertEqual(self.reported(), "")

if __name__ == "__main__":
    avocado.main()
