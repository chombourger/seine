# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import subprocess
import tempfile
import yaml

from seine                      import packages
from seine.transport_bootstrap import TransportBootstrap
from seine import tasks
from seine.utils                import ContainerEngine
from seine.utils                import spawn_own_pgroup
from seine.utils                import feeds
from seine.utils                import offline_apt_script
from seine.utils                import vendor_mountpoint

# Where the downloads cache is mounted, which is deliberately not apt's own
# archives directory.
#
# It used to be exactly that, and the cache is one directory per release
# shared by every build on the machine -- so two builds reaching the point
# of installing packages at the same time put two apts on one archives
# directory, and apt takes a lock there. The second does not wait for it:
#
#   E: Unable to lock directory /var/cache/apt/archives/
#
# apt gets an archives directory of the container's own instead, seeded
# from the cache and copied back when the playbooks are done -- which is
# what the bootstrap already does with mmdebstrap's sync-in and sync-out
# hooks, for the same reason.
DOWNLOADS = "/var/cache/seine/downloads"

# What a running system keeps in memory rather than on the disk. This
# container's file-system is exported as the image, so what a maintainer
# script writes to either of these would otherwise ship: podman's own
# /run/.containerenv did, saying in every image that it was made in a
# container. A tmpfs is not part of the export, and it is what the target
# mounts there anyway -- /run always, /tmp from trixie on.
#
# podman's defaults are what a system would use: /tmp keeps its 1777, both
# are nosuid and nodev, and neither is noexec, which a maintainer script
# running something out of /tmp would need.
TMPFS = ["--tmpfs", "/run", "--tmpfs", "/tmp"]

# apt's own, inside the container and nobody else's.
ARCHIVES = "/var/cache/apt/archives"

# Where the feeds TargetBootstrap left for here are written. Its own file,
# not sources.list itself, so nothing here has to parse or rewrite what
# mmdebstrap already put there.
FEEDS_LIST = "/etc/apt/sources.list.d/seine-feeds.list"

# Runs the spec's Ansible playbooks against a live, host-architecture-driven
# ansible-playbook process connecting into the (possibly foreign-arch)
# target container via the containers.podman connection plugin, instead of
# installing ansible into the target rootfs and running it there under
# qemu-user-static emulation. Only python3/python3-apt/attr -- what
# ansible's modules and our own xattr capture need -- ever lands in the
# target container, and it is autoremoved again once the playbooks are
# done, same as AnsibleBootstrap's ansible install was before it.
class AnsibleContainerRunner:
    # 'vendor_digest' is offline_dockerfile_digest()'s return, passed
    # straight through to TransportBootstrap -- see its own comment on
    # why it has to be folded into the Dockerfile text.
    def __init__(self, baseline, distro, options, verbose=False, vendor_digest=None):
        self.baseline = baseline
        self.distro = distro
        self.options = options
        self.verbose = verbose
        self.vendor_digest = vendor_digest
        self.cid = None

    def _exec(self, args, check=True):
        return ContainerEngine.run(["container", "exec", self.cid] + args, check=check)

    # The container the playbooks run against, which is also what is
    # exported as the image.
    def container_command(self, image):
        return (["container", "run", "-d"] + self._volumes() + TMPFS
                + [image, "sleep", "infinity"])

    def _volumes(self):
        volumes = ["-v", "%s:%s" % (
            ContainerEngine.downloads(self.distro["release"]), DOWNLOADS)]
        # Packages rebuilt from the spec's 'packages' section, if any, so the
        # playbooks can install them with a plain apt task. One repository
        # holds every architecture that was built for, and apt takes from
        # it what this root file-system's architecture can use.
        if packages.has_packages(self.distro):
            volumes += ["-v", "%s:%s" % (packages.repository(self.distro),
                                         packages.REPOSITORY)]
        # 'vendor:'s own *delivered* repository for this build's own
        # release -- read-only, since nothing running inside the target
        # has any business adding to a vendor. Not vendor.repository()
        # (the cache of raw fetched files, which carries no pool/dists
        # of its own), and not one mount per suite: 'seine build's own
        # vendor task (Image._vendor_task()) only ever vendors the
        # release itself -- a resolve sees every feed of the release
        # (main, updates, security -- see _suite_distro()'s own
        # comment), but what it delivers is one repository, named for
        # the release alone. Built by that task, and by the time this
        # runs it already has, since 'rootfs' waits on it.
        if self.distro.get("apt-pull-mode") == "offline":
            from seine import vendor
            release = self.distro["release"]
            volumes += ["-v", "%s:%s:ro" % (vendor.deploy_repository(release),
                                            vendor_mountpoint(release))]
        return volumes

    # The cache into apt's own archives directory, so what another build
    # already fetched is not fetched again.
    def _seed_downloads(self):
        self._exec(["sh", "-c",
                    "mkdir -p %(to)s && "
                    "cp -n %(from)s/*.deb %(to)s/ 2>/dev/null; "
                    "true" % {"from": DOWNLOADS, "to": ARCHIVES}])

    # And back again, for the build after this one. One file at a time and
    # through a rename, since another build may be seeding itself from this
    # directory while this runs: a whole .deb appears under its name or
    # nothing does, and the dot keeps what is half-written out of the glob
    # that reads it.
    #
    # Nothing here fails a build. What this writes is a saving for the next
    # one rather than anything this one needs.
    def _save_downloads(self):
        self._exec(["sh", "-c",
                    'for deb in %(from)s/*.deb; do '
                    '  [ -e "$deb" ] || continue; '
                    '  name=$(basename "$deb"); '
                    '  [ -e "%(to)s/$name" ] && continue; '
                    '  cp "$deb" "%(to)s/.$name.$$" 2>/dev/null && '
                    '    mv "%(to)s/.$name.$$" "%(to)s/$name"; '
                    'done; true' % {"from": ARCHIVES, "to": DOWNLOADS}], check=False)

    # Every feed but base_feed() -- see TargetBootstrap.create() for why
    # those aren't baked in. Nothing to do for the common case of one
    # feed, unless this went offline: base_feed() is baked into the
    # image pointing at the network same as ever (vendor:'s own repository
    # never covers the minimal set TargetBootstrap bootstraps from -- see
    # docs/specification.md's own 'vendor' section), so what apt reads
    # from here on has to be replaced outright rather than added to, or
    # the baked-in entry would still reach for the network right beside
    # the one just written for it.
    def _configure_feeds(self):
        if self.distro.get("apt-pull-mode") != "offline":
            extra = feeds(self.distro)[1:]
            if len(extra) == 0:
                return
            script = offline_apt_script(self.distro, extra, FEEDS_LIST)
            self._exec(["sh", "-c", script])
            return
        # Offline: every apt source this container had -- base_feed()
        # included -- is replaced by a single vendor entry for the
        # build's own release, not one per suite/component: the
        # delivered repository already covers the whole release in one
        # 'dists/<release>/' (main and extra alike -- see vendor.py's
        # own index()), so one deb line and one deb-src line naming
        # both components is everything apt needs to read from it.
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

    # 'apt-pull-mode: offline' above replaced every feed -- base_feed()
    # included -- with the local vendor for the length of this run alone.
    # What ships has to read from the real feeds like any other image, or
    # the first 'apt-get update' the device itself ever runs fails
    # reaching for a container path that only ever existed on the machine
    # that built it. Rewritten fresh rather than restored from a backup:
    # _configure_feeds() deleted the base feed TargetBootstrap baked in
    # along with everything else, going offline, so there is nothing left
    # to put back other than by asking apt_sources() again.
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
            # Keep apt's package index in sync with what TransportBootstrap
            # baked in and the feeds just configured above, same as the
            # Dockerfile-based path used to do before running its own
            # playbooks.
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
        # Nothing to configure beyond what the bootstrap already baked in
        # -- 'ansible-playbook' itself refuses an empty play list rather
        # than treating it as a no-op. A disk-owning specification whose
        # every partition/volume names a 'source:' (nothing of its own to
        # build) is exactly this case.
        if len(playbooks) == 0:
            return

        # On copies, not 'playbooks' itself: it is 'spec["playbook"]', and
        # a step that changed it after 'seine analyze' recorded a digest
        # of the specification would leave nothing later reloading those
        # same files could ever match again.
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
        # The same storage and runroot our own podman calls use: a
        # connection plugin talking to our storage with podman's default
        # runroot is the mismatch this pair exists to avoid.
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
        # The rebuilt packages are installed by now; leaving apt pointed at
        # a repository that only exists on the machine that built the image
        # would break the first 'apt-get update' run on the target.
        self._exec(["sh", "-c", packages.apt_deconfiguration()])
        self._exec(["sh", "-c", "rm -rf /var/lib/apt/lists/*"])
