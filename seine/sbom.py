# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import tarfile
import tempfile

from seine.tasks import Task
from seine.utils import ContainerEngine

# debsbom, run from the image its authors publish. Preferred over a
# generic scanner because it reports the source package every binary was
# built from, which is what CVEs are filed against.
DEBSBOM_IMAGE = "ghcr.io/siemens/debsbom:latest"

# What debsbom reads out of a root file-system: dpkg's package list, and
# the apt caches saying where each package came from. Pulling those out of
# the tarball beats unpacking a whole root file-system to reach them.
SBOM_INPUTS = ["var/lib/dpkg/status", "var/lib/apt/extended_states",
               "var/lib/apt/lists/"]

# An entry ending in '/' takes everything under it, anything else is that
# one file -- 'status-old' is not the status file, and would land in the
# SBOM as a duplicate package list.
def wanted(name):
    name = name.removeprefix("./")
    return any(name == path or (path.endswith("/") and name.startswith(path))
               for path in SBOM_INPUTS)

# Every installed package's own Installed-Size (KiB), read from the
# same var/lib/dpkg/status wanted() pulls from the tarball -- not from
# the SBOM debsbom itself writes. Largest first. A missing or unreadable
# tarball is an empty answer, not a crash.
def installed_sizes(tarball):
    try:
        with tarfile.open(tarball, "r") as tar:
            member = next((m for m in tar.getmembers()
                          if m.name.removeprefix("./") == "var/lib/dpkg/status"),
                         None)
            if member is None:
                return []
            status = tar.extractfile(member).read().decode("utf-8", "replace")
    except (OSError, tarfile.TarError):
        return []

    sizes = []
    for stanza in status.split("\n\n"):
        name = kib = None
        for line in stanza.splitlines():
            if line.startswith("Package:"):
                name = line[len("Package:"):].strip()
            elif line.startswith("Installed-Size:"):
                try:
                    kib = int(line[len("Installed-Size:"):].strip())
                except ValueError:
                    kib = None
        if name and kib is not None:
            sizes.append((name, kib))
    return sorted(sizes, key=lambda entry: entry[1], reverse=True)

class SBOM:
    def __init__(self, distro, options=None):
        self.distro = distro
        self.options = options if options is not None else {}

    # The name debsbom is asked to write to. It appends '.spdx.json' to it
    # itself, so the produced file is '<image>-sbom.spdx.json'.
    def _output_file(self, image):
        output = None
        if 'sbom' in self.options and self.options['sbom'] is True:
            output = os.path.realpath(image)
            if output.endswith('.img'):
                output = output[:-len('.img')]
            output = output + '-sbom'
        return output

    def _extract(self, tarball, root):
        with tarfile.open(tarball, "r") as tar:
            for member in tar:
                if wanted(member.name):
                    tar.extract(member, path=root)

    # Made from the exported root file-system rather than from the disk
    # image: debsbom reads dpkg's own package list, which the tarball has
    # and a not-yet-assembled image has not.
    def task(self, image):
        return Task("sbom",
                    lambda: self.generate(image._tarball, image._output),
                    needs=["tarball"])

    def generate(self, tarball, image):
        output = self._output_file(image)
        if output is not None:
            dir = os.path.dirname(output)
            with tempfile.TemporaryDirectory(dir=ContainerEngine.scratch()) as root:
                self._extract(tarball, root)
                run_cmd = ['run', '--rm',
                           '-v', '{}:/rootfs:ro,z'.format(root),
                           '-v', '{}:{}:z'.format(dir, dir),
                           DEBSBOM_IMAGE]
                # The architecture is told rather than detected: debsbom
                # reads it from 'var/lib/dpkg/arch-native', which the root
                # file-systems mmdebstrap produces do not have.
                debsbom_cmd = ['debsbom', 'generate', '-r', '/rootfs', '-t', 'spdx',
                               '--distro-arch', self.distro["architecture"],
                               '-o', output]
                ContainerEngine.run([*run_cmd, *debsbom_cmd], check=True)
