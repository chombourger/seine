#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0
#
# Builds the OCI bundle a 'seine-oci-<release>-<hostarch>' package
# ships: the plain 'debian:<release>' base image HostBootstrap (and the
# force_online=True HostBootstrap vendor.py builds) pull it FROM --
# the one and only podman/docker registry pull anywhere in seine's own
# container chain, everything else building FROM an already-local,
# seine-produced tag. Loaded into podman ahead of time (seine/oci_bundle.py),
# this is what lets seine never need a registry pull at all, on a real
# host matching HOSTARCH -- HostBootstrap's own apt-get/mmdebstrap work
# still runs for real, straight to the Debian archive, same as always.
# Not shipped in any package: run only at package-build time, straight
# from the checkout, never installed.
#
# Needs network access to pull 'debian:<release>', unlike a normal
# 'dpkg-buildpackage' run.
#
# HOSTARCH picks which host to pull for (default: this machine's own),
# via podman's own '--platform' -- so this runs the same on any machine,
# producing either host's bundle.
#
# Usage: HOSTARCH=<arch> build-oci.py <output-dir>

import gzip
import os
import shutil
import subprocess
import sys

RELEASES = os.environ.get("RELEASES", "bookworm trixie").split()
SOURCE = "debian"

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from seine.utils import ContainerEngine
from seine.utils import HOST_ARCH

HOSTARCH = os.environ.get("HOSTARCH", HOST_ARCH)

def build_release(release, out):
    tag = "%s:%s" % (SOURCE, release)
    pull = ["pull"]
    if HOSTARCH != HOST_ARCH:
        pull += ["--platform", "linux/%s" % HOSTARCH]
    ContainerEngine.run(pull + [tag], check=True)

    release_out = os.path.join(out, release)
    os.makedirs(release_out, exist_ok=True)
    with gzip.open(os.path.join(release_out, "images.tar.gz"), "wb",
                   compresslevel=1) as gz:
        podman = ContainerEngine.Popen(["save", tag], stdout=subprocess.PIPE)
        shutil.copyfileobj(podman.stdout, gz)
        podman.stdout.close()
        if podman.wait() != 0:
            raise RuntimeError("podman could not save %s!" % tag)

def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)

    print("building for: host %s, releases %s" % (HOSTARCH, RELEASES))
    for release in RELEASES:
        build_release(release, out)

if __name__ == "__main__":
    main()
