# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import tempfile
import yaml

from seine.transport_bootstrap import TransportBootstrap
from seine.utils                import ContainerEngine

# Runs the spec's Ansible playbooks against a live, host-architecture-driven
# ansible-playbook process connecting into the (possibly foreign-arch)
# target container via the containers.podman connection plugin, instead of
# installing ansible into the target rootfs and running it there under
# qemu-user-static emulation. Only python3/python3-apt/attr -- what
# ansible's modules and our own xattr capture need -- ever lands in the
# target container, and it is autoremoved again once the playbooks are
# done, same as AnsibleBootstrap's ansible install was before it.
class AnsibleContainerRunner:
    def __init__(self, baseline, distro, options, verbose=False):
        self.baseline = baseline
        self.distro = distro
        self.options = options
        self.verbose = verbose
        self.cid = None

    def _exec(self, args, check=True):
        return ContainerEngine.run(["container", "exec", self.cid] + args, check=check)

    # Creates the target container, runs 'playbooks' against it and leaves
    # it running (stopped callers are expected to 'container export' it
    # then 'container rm' it) for build_tarball() to pick up. On failure,
    # the container is torn down here since there's nothing left to export.
    def run(self, playbooks):
        transport = TransportBootstrap(self.baseline, self.distro, self.options)
        if ContainerEngine.hasImage(transport.name) is False:
            transport.create()

        self.cid = ContainerEngine.check_output(
            ["container", "run", "-d", transport.name, "sleep", "infinity"]).strip()
        try:
            # Keep apt's package index in sync with what TransportBootstrap
            # baked in, same as the Dockerfile-based path used to do before
            # running its own playbooks.
            self._exec(["apt-get", "update", "-qqy"])
            self._run_playbooks(playbooks)
            self._finalize()
        except:
            ContainerEngine.run(["container", "rm", "-f", self.cid], check=False)
            self.cid = None
            raise
        return self.cid

    def _run_playbooks(self, playbooks):
        for playbook in playbooks:
            playbook["hosts"] = "all"
            # INITRD=No mirrors the old in-container 'RUN INITRD=No
            # ansible-playbook ...' -- individual package installs skip
            # their own initramfs regen, _finalize() does one pass instead.
            playbook["environment"] = {"INITRD": "No"}

        ansiblefile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(playbooks, ansiblefile)
        ansiblefile.close()

        inventoryfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        inventoryfile.write(
            "%s ansible_connection=containers.podman.podman "
            "ansible_podman_extra_args='--root %s' "
            "ansible_python_interpreter=/usr/bin/python3\n" % (
                self.cid.decode(), ContainerEngine.root()))
        inventoryfile.close()

        cmd = ["ansible-playbook", "-i", inventoryfile.name, ansiblefile.name]
        if self.verbose:
            cmd.insert(1, "-v")
        try:
            subprocess.run(cmd, check=True)
        finally:
            os.unlink(ansiblefile.name)
            os.unlink(inventoryfile.name)

    def _finalize(self):
        self._exec(["sh", "-c",
            "if ls /boot/vmlinuz-* >/dev/null 2>&1; then "
            "update-initramfs -c -k all; fi"])
        self._exec(["sh", "-c",
            "mkdir -p /var/lib/seine && "
            "getfattr -Rh -m '' -d -e hex $(find / -mindepth 1 -maxdepth 1 "
            "-type d -not -name proc -not -name sys -not -name tmp "
            "-printf '%P\\n') > /rootfs.xattr"])
        # TransportBootstrap marks its own packages "auto" so a plain
        # autoremove sweeps them away here without this runner needing to
        # know what TransportBootstrap actually installed.
        self._exec(["apt-get", "autoremove", "-qqy"])
        self._exec(["apt-get", "clean", "-qqy"])
        self._exec(["sh", "-c", "rm -rf /var/lib/apt/lists/*"])
