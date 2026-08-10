#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

class TwoBuildsDoNotWriteOneCacheAtOnce(avocado.Test):
    def test(self):
        import subprocess
        import sys

        shared = os.path.join(self.workdir, "cache")
        # Two seine runs wanting the same chroot want the same bytes;
        # what they must not do is write it at the same time.
        writer = (
            "import sys, time;"
            "sys.path.insert(0, %r);"
            "from seine.utils import locked;"
            "path = %r;"
            "f = None;"
            "exec('with locked(path):\\n"
            "  open(path, \"a\").write(\"in\\\\n\")\\n"
            "  time.sleep(0.5)\\n"
            "  open(path, \"a\").write(\"out\\\\n\")\\n')"
            % (path_to_sources, shared))
        both = [subprocess.Popen([sys.executable, "-c", writer])
                for _ in range(2)]
        for process in both:
            self.assertEqual(process.wait(), 0)

        with open(shared) as f:
            # Interleaved would be in, in, out, out.
            self.assertEqual(f.read().split(), ["in", "out", "in", "out"])
