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

# Installing python3/python3-apt/attr is the same small apt install on
# every single build, immediately made redundant by the next build against
# the same baseline. Cache that install as its own layer, keyed by the baseline
# it's built on top of and the target architecture -- a custom 'baseline:'
# is just an opaque image reference, and the same string could resolve to
# different content per architecture, so both are needed to avoid a
# wrong-arch cache hit.
class TransportBootstrap(Bootstrap):
    # A root file-system too, but a baseline image plus the two packages
    # ansible needs rather than an archive's output, so it does not go
    # stale when the archive moves and is worth carrying.
    kind = TRANSPORT_KIND

    # 'vendor_digest' is offline_dockerfile_digest()'s return -- see
    # HostBootstrap's own comment on why it has to be folded into the
    # Dockerfile text rather than left to Bootstrap.digest() alone.
    def __init__(self, baseline, distro, options, vendor_digest=None):
        self.baseline = baseline
        self.vendor_digest = vendor_digest
        super().__init__(distro, options)

    # feed_digest() folded in: this bakes base_feed() into the image the
    # same way TargetBootstrap does, and two specifications sharing a
    # baseline but not a mirror would otherwise collide on one tag.
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

    # base_feed() alone: same reasoning as HostBootstrap's own _sources().
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
