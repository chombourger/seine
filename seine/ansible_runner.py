# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import tempfile
import yaml

from seine                      import packages
from seine.transport_bootstrap import TransportBootstrap
from seine import tasks
from seine.container import ContainerEngine, spawn_own_pgroup
from seine.utils                import feeds
from seine.utils                import offline_apt_script
from seine.utils                import vendor_mountpoint

# Mount point for the shared downloads cache. Not apt's own archives dir:
# that dir is shared by all builds, so two builds installing packages at
# once would fight over apt's lock there.
DOWNLOADS = "/var/cache/seine/downloads"

# /run and /tmp must not leak into the exported image (e.g. podman's own
# /run/.containerenv), so mount them as tmpfs instead of writing to disk.
TMPFS = ["--tmpfs", "/run", "--tmpfs", "/tmp"]

ARCHIVES = "/var/cache/apt/archives"

# File TargetBootstrap leaves feeds in. Separate from sources.list so we
# don't have to parse/rewrite what mmdebstrap already wrote there.
FEEDS_LIST = "/etc/apt/sources.list.d/seine-feeds.list"

# Runs the spec's playbooks with a host-side ansible-playbook connecting
# into the (possibly foreign-arch) target container over containers.podman,
# instead of running ansible inside the target under qemu emulation.
class AnsibleContainerRunner:
    # vendor_digest comes from offline_dockerfile_digest() and is passed
    # through to TransportBootstrap.
    def __init__(self, baseline, distro, options, verbose=False, vendor_digest=None):
        self.baseline = baseline
        self.distro = distro
        self.options = options
        self.verbose = verbose
        self.vendor_digest = vendor_digest
        self.cid = None

    def _exec(self, args, check=True):
        return ContainerEngine.run(["container", "exec", self.cid] + args, check=check)

    # Container the playbooks run against; also what gets exported as the image.
    def container_command(self, image):
        return (["container", "run", "-d"] + self._volumes() + TMPFS
                + [image, "sleep", "infinity"])

    def _volumes(self):
        volumes = ["-v", "%s:%s" % (
            ContainerEngine.downloads(self.distro["release"]), DOWNLOADS)]
        # Packages rebuilt from the spec's 'packages' section (if any), so
        # playbooks can install them via a plain apt task.
        if packages.has_packages(self.distro):
            volumes += ["-v", "%s:%s" % (packages.repository(self.distro),
                                         packages.REPOSITORY)]
        # 'vendor:'s delivered repository for this release, read-only:
        # nothing in the target should add to a vendor repo. Built by
        # 'seine build's own vendor task, which 'rootfs' already waits on.
        if self.distro.get("apt-pull-mode") == "offline":
            from seine import vendor
            release = self.distro["release"]
            volumes += ["-v", "%s:%s:ro" % (vendor.deploy_repository(release),
                                            vendor_mountpoint(release))]
        return volumes

    # Copy cached .debs into apt's archives dir so they aren't re-fetched.
    def _seed_downloads(self):
        self._exec(["sh", "-c",
                    "mkdir -p %(to)s && "
                    "cp -n %(from)s/*.deb %(to)s/ 2>/dev/null; "
                    "true" % {"from": DOWNLOADS, "to": ARCHIVES}])

    # Copy .debs back to the cache for the next build. Written one at a
    # time via a temp name + rename, so a concurrent build never sees a
    # half-written file. Never fails the build: this is just a saving.
    def _save_downloads(self):
        self._exec(["sh", "-c",
                    'for deb in %(from)s/*.deb; do '
                    '  [ -e "$deb" ] || continue; '
                    '  name=$(basename "$deb"); '
                    '  [ -e "%(to)s/$name" ] && continue; '
                    '  cp "$deb" "%(to)s/.$name.$$" 2>/dev/null && '
                    '    mv "%(to)s/.$name.$$" "%(to)s/$name"; '
                    'done; true' % {"from": ARCHIVES, "to": DOWNLOADS}], check=False)

    # Adds the feeds beyond base_feed() (already baked in by
    # TargetBootstrap). Offline mode instead replaces all feeds outright
    # with the vendor repo, so apt never falls back to the network.
    def _configure_feeds(self):
        if self.distro.get("apt-pull-mode") != "offline":
            extra = feeds(self.distro)[1:]
            if len(extra) == 0:
                return
            script = offline_apt_script(self.distro, extra, FEEDS_LIST)
            self._exec(["sh", "-c", script])
            return
        # vendor's repo covers the whole release in one place, so one
        # deb + deb-src line (both components) replaces every apt source.
        from seine import vendor
        release = self.distro["release"]
        where = vendor_mountpoint(release)
        keyring = vendor.keyring(release)
        options = ("[signed-by=%s/%s]" % (where, keyring) if keyring is not None
                  else "[trusted=yes]")
        lines = ["deb %s file:%s %s main extra" % (options, where, release),
                 "deb-src %s file:%s %s main extra" % (options, where, release)]
        script = ("rm -f /etc/apt/sources.list "
                 "/etc/apt/sources.list.d/*.sources "
                 "/etc/apt/sources.list.d/*.list; ")
        script += "".join("echo '%s' >> %s; " % (line, FEEDS_LIST) for line in lines)
        self._exec(["sh", "-c", script])

    # Undoes _configure_feeds()'s offline swap: the shipped image must
    # read from the real feeds, not a vendor path only the build host has.
    def _restore_online_feeds(self):
        if self.distro.get("apt-pull-mode") != "offline":
            return
        script = "rm -f %s; " % FEEDS_LIST
        script += offline_apt_script(self.distro, feeds(self.distro), FEEDS_LIST)
        self._exec(["sh", "-c", script])

    # Creates the target container, runs 'playbooks' against it and leaves
    # it running (stopped callers are expected to 'container export' it
    # then 'container rm' it) for build_tarball() to pick up. On failure,
    # the container is torn down here since there's nothing left to export.
    def run(self, playbooks):
        transport = TransportBootstrap(self.baseline, self.distro, self.options,
                                       vendor_digest=self.vendor_digest)
        transport.create()

        self.cid = ContainerEngine.check_output(
            self.container_command(transport.name)).strip()
        try:
            if packages.has_packages(self.distro):
                self._exec(["sh", "-c", packages.apt_configuration(
                    packages.REPOSITORY,
                    keyring=packages.keyring(self.distro))])
            self._seed_downloads()
            self._configure_feeds()
            self._exec(["apt-get", "update", "-qqy"])
            self._run_playbooks(playbooks)
            self._save_downloads()
            self._restore_online_feeds()
            self._finalize()
        except:
            ContainerEngine.discard(self.cid, force=True, failed=True)
            self.cid = None
            raise
        return self.cid

    def _run_playbooks(self, playbooks):
        # ansible-playbook errors on an empty play list, so skip it here.
        if len(playbooks) == 0:
            return

        # Mutate copies, not 'playbooks' itself (it's 'spec["playbook"]'):
        # changing the spec here would break digest matching in 'seine analyze'.
        run = []
        for playbook in playbooks:
            playbook = dict(playbook)
            # Individual package installs skip their own initramfs regen,
            # _finalize() does one pass instead.
            playbook["environment"] = {"INITRD": "No"}
            run.append(playbook)

        ansiblefile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(run, ansiblefile)
        ansiblefile.close()

        inventoryfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        # Must match our own podman calls' storage/runroot, or the
        # connection plugin looks in the wrong place.
        storage = "--root %s --runroot %s" % (
            ContainerEngine.root(), ContainerEngine.runroot())
        inventoryfile.write(
            "%s ansible_connection=containers.podman.podman "
            "ansible_podman_extra_args='%s' "
            "ansible_python_interpreter=/usr/bin/python3\n" % (
                self.cid.decode(), storage))
        inventoryfile.close()

        cmd = ["ansible-playbook", "-i", inventoryfile.name, ansiblefile.name]
        if self.verbose:
            cmd.insert(1, "-v")

        # A fragment's own 'library/' directories, collected while the
        # specification loaded -- see build.py. ANSIBLE_LIBRARY is a search
        # path, colon-joined, same as PATH.
        env = os.environ.copy()
        library = self.options.get("ansible_library")
        if library:
            env["ANSIBLE_LIBRARY"] = ":".join(library)
        # Pinned, not left to ansible.cfg: the TUI scrapes this
        # callback's own PLAY/TASK lines from the log to highlight the
        # spec tree.
        env["ANSIBLE_STDOUT_CALLBACK"] = "default"

        try:
            # To the task's file when one is capturing, so a playbook's
            # output stays with the rest of what that step did.
            output = tasks.output()
            # In a process group of its own, as the container engine is
            # run (see spawn_own_pgroup): a playbook killed half-way
            # leaves a half-customized root file-system.
            proc = spawn_own_pgroup(cmd, stdout=output, env=env,
                                    stderr=subprocess.STDOUT if output else None)
            proc.wait()
            if proc.returncode != 0:
                raise subprocess.CalledProcessError(proc.returncode, cmd)
        finally:
            os.unlink(ansiblefile.name)
            os.unlink(inventoryfile.name)

    # Rebuilds every kernel's initrd with whichever generator is installed
    # (only one ever is: dracut Conflicts: initramfs-tools). dracut's
    # output name is spelled out to match what imager.py expects.
    def _finalize(self):
        self._exec(["sh", "-c",
            "for k in /boot/vmlinuz-*; do "
            "[ -e \"$k\" ] || continue; "
            "v=${k#/boot/vmlinuz-}; "
            "if command -v update-initramfs >/dev/null 2>&1; then "
            "update-initramfs -c -k \"$v\"; "
            "elif command -v dracut >/dev/null 2>&1; then "
            "dracut --force \"/boot/initrd.img-$v\" \"$v\"; "
            "fi; "
            "done"])
        self._exec(["sh", "-c",
            "mkdir -p /var/lib/seine && "
            "getfattr -Rh -m '' -d -e hex $(find / -mindepth 1 -maxdepth 1 "
            "-type d -not -name proc -not -name sys -not -name tmp "
            "-printf '%P\\n') > /rootfs.xattr"])
        # TransportBootstrap marks its own packages "auto" so a plain
        # autoremove sweeps them away here without this runner needing to
        # know what TransportBootstrap actually installed.
        self._exec(["apt-get", "autoremove", "-qqy"])
        # The rebuilt packages are installed by now; leaving apt pointed at
        # a repository that only exists on the machine that built the image
        # would break the first 'apt-get update' run on the target.
        self._exec(["sh", "-c", packages.apt_deconfiguration()])
        self._exec(["sh", "-c", "rm -rf /var/lib/apt/lists/*"])
