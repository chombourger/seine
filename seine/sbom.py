# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import tarfile
import tempfile

from seine.tasks import Task
from seine.container import ContainerEngine

# Reports source packages, which CVEs are filed against, unlike generic scanners.
DEBSBOM_IMAGE = "ghcr.io/siemens/debsbom:latest"

# Files debsbom needs from a root filesystem: dpkg's package list and
# apt's caches. Extracting these avoids unpacking the whole tarball.
SBOM_INPUTS = ["var/lib/dpkg/status", "var/lib/apt/extended_states",
               "var/lib/apt/lists/"]

# Entries ending in '/' match everything under them, others match exactly.
def wanted(name):
    name = name.removeprefix("./")
    return any(name == path or (path.endswith("/") and name.startswith(path))
               for path in SBOM_INPUTS)

# Name/version/Installed-Size for each package, read from dpkg's status
# file in the tarball. Sorted largest first.
def installed_packages(tarball):
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

    packages = []
    for stanza in status.split("\n\n"):
        name = version = kib = None
        for line in stanza.splitlines():
            if line.startswith("Package:"):
                name = line[len("Package:"):].strip()
            elif line.startswith("Version:"):
                version = line[len("Version:"):].strip()
            elif line.startswith("Installed-Size:"):
                try:
                    kib = int(line[len("Installed-Size:"):].strip())
                except ValueError:
                    kib = None
        if name and kib is not None:
            packages.append((name, version or "?", kib))
    return sorted(packages, key=lambda entry: entry[2], reverse=True)

# Path a prior 'seine build --sbom' would have written to, whether or not
# it exists. Same suffix rule as SBOM._output_file(), but without its
# gate on the current build options.
def output_path(image_output):
    path = os.path.realpath(image_output)
    if path.endswith(".img"):
        path = path[:-len(".img")]
    return path + "-sbom.spdx.json"

class SBOM:
    def __init__(self, distro, options=None):
        self.distro = distro
        self.options = options if options is not None else {}

    # debsbom appends '.spdx.json' itself, giving '<image>-sbom.spdx.json'.
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

    # Needs the tarball, not the disk image, for dpkg's package list.
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
                # Passed explicitly: mmdebstrap rootfs lack arch-native.
                debsbom_cmd = ['debsbom', 'generate', '-r', '/rootfs', '-t', 'spdx',
                               '--distro-arch', self.distro["architecture"],
                               '-o', output]
                ContainerEngine.run([*run_cmd, *debsbom_cmd], check=True)
