# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

from seine.bootstrap import Bootstrap
from seine.utils import apt_sources_dockerfile
from seine.utils import APT_LISTS_CLEANUP
from seine.utils import base_feed
from seine.utils import feed_digest
from seine.utils import TRANSPORT_KIND
from seine.utils import vendor_mountpoint

# Caches the python3/python3-apt/attr install (needed by ansible) as one
# layer, keyed by baseline + arch since a 'baseline:' string can resolve to
# different content per architecture.
class TransportBootstrap(Bootstrap):
    kind = TRANSPORT_KIND

    # vendor_digest comes from offline_dockerfile_digest(); see HostBootstrap
    # for why it must be baked into the Dockerfile text, not left to digest().
    def __init__(self, baseline, distro, options, vendor_digest=None):
        self.baseline = baseline
        self.vendor_digest = vendor_digest
        super().__init__(distro, options)

    # Bakes feed_digest() into the tag so specs sharing a baseline but
    # different mirrors don't collide on one image.
    def defaultName(self):
        baseline_id = self.baseline.replace("/", "-").replace(":", "-")
        return os.path.join("transport-bootstrap", self.distro["architecture"],
                            baseline_id, feed_digest(self.distro))

    def _offline(self):
        return self.distro.get("apt-pull-mode") == "offline"

    def create(self):
        build_options = []
        mount = ""
        digest_comment = ""
        if self._offline():
            from seine import vendor
            release = self.distro["release"]
            where = vendor.offline_build_context(release)
            build_options += ["--build-context",
                              "%s=%s" % (vendor.BUILD_CONTEXT, where)]
            mount = "--mount=type=bind,from=%s,target=%s,ro" % (
                vendor.BUILD_CONTEXT, vendor_mountpoint(release))
            digest_comment = "# vendor digest: %s" % self.vendor_digest
        return self.build(TRANSPORT_BOOTSTRAP_SCRIPT.format(
            self.baseline, self._sources(), mount, digest_comment,
            APT_LISTS_CLEANUP), base=self.baseline, options=build_options)

    def _sources(self):
        return apt_sources_dockerfile(self.distro, [base_feed(self.distro)],
                                      offline=self._offline())

TRANSPORT_BOOTSTRAP_SCRIPT = """
FROM {0}
{3}
RUN {2} rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources \\
           /etc/apt/sources.list.d/*.list && \\
    {1} && \\
    apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends python3 python3-apt attr && \\
    apt-mark auto python3 python3-apt attr && \\
    {4}
CMD /bin/true
"""
