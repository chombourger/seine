# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import subprocess
import tarfile

class ContainerEngine:
    @staticmethod
    def hasImage(name):
        result = ContainerEngine.run(["image", "exists", name], check=False)
        return result.returncode == 0
    # Builds a throwaway container from 'image', streams its filesystem out
    # via 'container export' and extracts it into output_dir, then removes
    # the container. 'member_filter(name)', if given, is called for every
    # tar member and only extracts the ones it accepts -- used to pull a
    # single directory or a handful of files out of an otherwise large
    # image without writing the whole thing to disk first.
    @staticmethod
    def extractImage(image, output_dir, member_filter=None):
        cid = image.replace("/", "-")
        try:
            ContainerEngine.run(["container", "create", "--name", cid, image], check=True)
            proc = ContainerEngine.Popen(["container", "export", cid], stdout=subprocess.PIPE)
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for member in tar:
                    if member_filter is None or member_filter(member.name):
                        tar.extract(member, path=output_dir)
            proc.wait()
        finally:
            ContainerEngine.run(["container", "rm", cid], check=False)
    # Rootless podman's default graph-root is shared with every other user of
    # podman on the machine; seine relocates it under its own directory so
    # concurrent builds/tests don't collide with (or get confused by) images
    # from unrelated podman use. External tools that need to reach the same
    # storage (e.g. ansible's podman connection plugin) must be pointed at
    # this same path, hence it being its own method rather than inlined.
    @staticmethod
    def root():
        home = os.path.expanduser("~")
        return os.path.join(home, ".local", "share", "seine")
    # Where large, short-lived files go while a build is running. Not
    # /tmp, which is commonly a tmpfs and so memory -- a kernel source
    # tree is several gigabytes and unpacking one there has been known to
    # take the machine down with it. Not the working directory either,
    # which is someone's checkout. TMPDIR wins if it is set, as it should.
    @staticmethod
    def scratch():
        path = os.environ.get("TMPDIR") or "/var/tmp"
        path = os.path.join(path, "seine")
        os.makedirs(path, exist_ok=True)
        return path
    # Host-side apt archives cache, bind-mounted into ansible's target
    # container so package downloads survive across builds/releases.
    # Scoped per release: package filenames already carry the architecture,
    # so arm64/amd64 fetches for the same release safely share one dir.
    @staticmethod
    def downloads(release):
        home = os.path.expanduser("~")
        path = os.path.join(home, ".cache", "seine", "downloads", release)
        os.makedirs(path, exist_ok=True)
        return path
    # Host-side apt repository holding the packages rebuilt from the spec's
    # 'packages' section, bind-mounted into ansible's target container so
    # the playbooks can install them like any other package. Unlike the
    # downloads cache this one is scoped per architecture as well: it is
    # served as a flat repository whose Packages index describes exactly
    # one architecture.
    @staticmethod
    def packages(release, architecture):
        home = os.path.expanduser("~")
        path = os.path.join(home, ".cache", "seine", "packages", release, architecture)
        os.makedirs(path, exist_ok=True)
        return path
    # Host-side cache for the buildd chroot tarballs sbuild unpacks for
    # every package it builds. Producing one costs a full mmdebstrap run
    # (~150MB, minutes), so it is kept out of the container and reused.
    # The architecture here is the chroot's own, which for a cross build
    # is the build architecture rather than the target's.
    @staticmethod
    def chroots(release, architecture):
        home = os.path.expanduser("~")
        path = os.path.join(home, ".cache", "seine", "chroots", release, architecture)
        os.makedirs(path, exist_ok=True)
        return path
    @staticmethod
    def _podman_cmd(cmd):
        cmd.insert(0, ContainerEngine.root())
        cmd.insert(0, "--root")
        cmd.insert(0, "podman")
        return cmd
    @staticmethod
    def run(cmd, check=False):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.run(cmd, check=check)
    @staticmethod
    def check_output(cmd):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.check_output(cmd)
    @staticmethod
    def Popen(cmd, stdin=None, stdout=None, stderr=None):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr)
