# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import tempfile

from seine.bootstrap import Bootstrap
from seine.utils     import ContainerEngine

# Installing python3/python3-apt/attr is the same small apt install on
# every single build, immediately made redundant by the next build against
# the same baseline. Cache that install as its own layer, keyed by the baseline
# it's built on top of and the target architecture -- a custom 'baseline:'
# is just an opaque image reference, and the same string could resolve to
# different content per architecture, so both are needed to avoid a
# wrong-arch cache hit.
class TransportBootstrap(Bootstrap):
    def __init__(self, baseline, distro, options):
        self.baseline = baseline
        super().__init__(distro, options)

    def defaultName(self):
        baseline_id = self.baseline.replace("/", "-").replace(":", "-")
        return os.path.join("transport-bootstrap", self.distro["architecture"], baseline_id)

    def create(self):
        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(TRANSPORT_BOOTSTRAP_SCRIPT.format(self.baseline))
        dockerfile.close()
        try:
            ContainerEngine.run([
                "build", "--rm",
                "-t", self.name,
                "-f", dockerfile.name], check=True)
        finally:
            if self.options["keep"]:
                print("keeping '%s' (dockerfile for the transport bootstrap) as requested" % dockerfile.name)
            else:
                os.unlink(dockerfile.name)

TRANSPORT_BOOTSTRAP_SCRIPT = """
FROM {0}
RUN apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends python3 python3-apt attr && \\
    apt-mark auto python3 python3-apt attr && \\
    rm -rf /var/lib/apt/lists/*
CMD /bin/true
"""
