#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import ContainerEngine

class Workbench(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ.pop("SEINE_WORKBENCH_DIR", None)
        os.environ.pop("SEINE_BUILD_DIR", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_SEINE_WORKBENCH_DIR_overrides_SEINE_BUILD_DIR(self):
        os.environ["SEINE_BUILD_DIR"] = "/should/be/ignored"
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "bench")
        self.assertEqual(ContainerEngine.workbench(),
                         os.path.join(self.workdir, "bench"))

    def test_falls_back_to_SEINE_BUILD_DIR(self):
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.assertEqual(ContainerEngine.workbench(),
                         os.path.join(self.workdir, "workbench"))

    def test_the_directory_is_created(self):
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "new")
        ContainerEngine.workbench()
        self.assertTrue(os.path.isdir(os.path.join(self.workdir, "new")))

if __name__ == "__main__":
    avocado.main()
