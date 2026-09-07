# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Runs podman and lays out what seine keeps on disk between builds.
# Split out of seine/utils.py, which got too big.

import atexit
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile

from seine import tasks

# Puts the child in its own process group so Ctrl-C (which hits the
# terminal's group) does not kill it too. Done after spawn, not via
# start_new_session=True, to keep CPython's posix_spawn fast path --
# a real fork() would run grpc's atfork handler and hang while mtda
# has a live console/EVT stream open.
def spawn_own_pgroup(cmd, **kwargs):
    # posix_spawn only kicks in when executable= has a directory
    # component, so resolve a bare "podman" via PATH ourselves.
    if "executable" not in kwargs and not os.path.dirname(cmd[0]):
        resolved = shutil.which(cmd[0])
        if resolved:
            kwargs["executable"] = resolved
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        os.setpgid(proc.pid, proc.pid)
    except OSError:
        pass  # too late to matter: process already exited or exec'd
    return proc

class ContainerEngine:
    @staticmethod
    def hasImage(name):
        result = ContainerEngine.run(["image", "exists", name], check=False)
        return result.returncode == 0
    # Changes whenever the image is rebuilt, even under the same name.
    # Used to detect a stale image built from it.
    @staticmethod
    def imageId(name):
        if ContainerEngine.hasImage(name) == False:
            return None
        return ContainerEngine.check_output(
            ["image", "inspect", "-f", "{{.Id}}", name]).decode().strip()
    # A label seine set when it built the image. Missing if an older
    # seine built it without labels; that image then gets rebuilt.
    @staticmethod
    def imageLabel(name, label):
        if ContainerEngine.hasImage(name) == False:
            return None
        value = ContainerEngine.check_output(
            ["image", "inspect", "-f", "{{index .Labels \"%s\"}}" % label,
             name]).decode().strip()
        return None if value in ["", "<no value>"] else value
    # Random suffix, not the pid: two threads of the same build process
    # can extract different images at once, and a name built only from
    # the image (its old form) collided when two builds did that.
    @staticmethod
    def _scratch_name(image):
        return "%s-%s" % (image.replace("/", "-"), os.urandom(4).hex())

    # Creates a container from image, streams it out via 'container
    # export', extracts into output_dir, then removes it. member_filter
    # (name) -> bool, if given, skips tar members it rejects.
    @staticmethod
    def extractImage(image, output_dir, member_filter=None):
        cid = ContainerEngine._scratch_name(image)
        failed = True
        try:
            ContainerEngine.run(["container", "create", "--name", cid, image], check=True)
            proc = ContainerEngine.Popen(["container", "export", cid], stdout=subprocess.PIPE)
            with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                for member in tar:
                    if member_filter is None or member_filter(member.name):
                        tar.extract(member, path=output_dir)
            proc.wait()
            failed = False
        finally:
            ContainerEngine.discard(cid, failed=failed)
    # Root of everything seine creates while building (not a spec's own
    # deliverable). Each SEINE_*_DIR below can override its own piece.
    # Absolute, so a step that chdirs (podman, ansible) still finds it.
    @staticmethod
    def build_dir():
        return os.path.abspath(os.environ.get("SEINE_BUILD_DIR") or "./build")
    # Rootless podman's default graph-root is shared machine-wide; move
    # it under our own dir so concurrent builds don't collide with other
    # podman use. Its own method since external tools (ansible's podman
    # plugin) need to point at the same path.
    @staticmethod
    def root():
        return os.path.join(ContainerEngine.build_dir(), "containers")
    # Large short-lived build files. Not /tmp (often tmpfs/RAM -- a
    # multi-GB kernel tree can OOM the machine), not the checkout either.
    @staticmethod
    def scratch():
        path = os.environ.get("SEINE_TMP_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "tmp")
        os.makedirs(path, exist_ok=True)
        return path
    # Things kept across builds to save re-doing work, all under one
    # root so 'seine cache' has one place to look and to empty.
    @staticmethod
    def cache(*names):
        root = os.environ.get("SEINE_CACHE_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "cache")
        return os.path.join(root, *names)
    # Host-side apt archive cache, bind-mounted into ansible's target so
    # downloads survive across builds. Scoped per suite; arm64/amd64 can
    # share one dir since .deb filenames already carry the architecture.
    @staticmethod
    def downloads_root():
        return os.environ.get("SEINE_DL_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "downloads")
    @staticmethod
    def downloads(suite):
        path = os.path.join(ContainerEngine.downloads_root(), suite)
        os.makedirs(path, exist_ok=True)
        return path
    # apt's Sources/Packages indices, not the .debs themselves (that's
    # downloads()). No caller yet.
    @staticmethod
    def downloads_lists(suite):
        path = os.path.join(ContainerEngine.downloads_root(), suite, "lists")
        os.makedirs(path, exist_ok=True)
        return path
    # Host-side apt repo of packages rebuilt from the spec's 'packages'
    # section. One per release, all architectures together like a real
    # apt archive, since apt itself sorts out what it can use.
    @staticmethod
    def packages(release):
        path = ContainerEngine.cache("packages", release)
        os.makedirs(path, exist_ok=True)
        return path
    # sbuild's buildd chroot tarballs, cached host-side since building one
    # costs a full mmdebstrap run. architecture is the chroot's own build
    # architecture, not the target's, for a cross build.
    @staticmethod
    def chroots(release, architecture):
        path = ContainerEngine.cache("chroots", release, architecture)
        os.makedirs(path, exist_ok=True)
        return path
    # podman's runtime state, paired with our own root() storage so two
    # builds never share podman's idea of what is mounted.
    @staticmethod
    def runroot():
        return os.path.join(ContainerEngine.root(), "run")
    @staticmethod
    def deploy_root():
        return os.environ.get("SEINE_DEPLOY_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "deploy")
    # Fetched vendor .debs (seine/vendor's deploy_repository()) are a
    # shared, machine-independent input, unlike the rest of deploy_root()
    # which is per-build output -- so it gets its own override to point
    # at a network mount without dragging deploy_root() along.
    @staticmethod
    def vendor_root():
        return os.environ.get("SEINE_VENDOR_DIR") \
               or os.path.join(ContainerEngine.deploy_root(), "vendor")
    # Kept apart from scratch(), so logs survive 'seine cache clear'.
    @staticmethod
    def logs_root():
        return os.environ.get("SEINE_LOG_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "logs")
    # One JSON file per AI chat conversation (seine/tui/ai owns the
    # content, this only says where it lives).
    @staticmethod
    def chats():
        return os.environ.get("SEINE_CHAT_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "chats")
    # One append-only JSONL file per day of gated AI tool calls
    # (seine/tui/ai's _audit()), across all conversations. Kept apart
    # from scratch()/cache like logs_root(), for the same reason.
    @staticmethod
    def audit():
        import datetime
        root = os.environ.get("SEINE_AUDIT_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "audit")
        os.makedirs(root, exist_ok=True)
        stamp = datetime.date.today().strftime("%Y%m%d")
        return os.path.join(root, "%s.jsonl" % stamp)
    # Pulled package sources (seine/sources.py) or 'bash' tool output.
    # Kept across builds on purpose, unlike scratch() above.
    @staticmethod
    def workbench():
        path = os.environ.get("SEINE_WORKBENCH_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "workbench")
        os.makedirs(path, exist_ok=True)
        return path
    # Lock on the storage a seine run uses. Shared for readers, exclusive
    # for sweepers -- two builds run side by side, while 'seine cache
    # clear' waits for both instead of removing what they stand on.
    @staticmethod
    def storage_lock():
        return os.path.join(ContainerEngine.root(), "images")

    # Containers normally get removed on teardown; SEINE_KEEP_DEAD_CONTAINERS
    # keeps failed ones instead, since conmon's exit info dies with the
    # container. Off by default -- these are gigabytes each, unreaped.
    _kept = []

    @staticmethod
    def keep_dead_containers():
        value = os.environ.get("SEINE_KEEP_DEAD_CONTAINERS")
        return value is not None and value not in ["", "0", "no"]

    # Removes a container, unless 'failed' and keep_dead_containers() --
    # then keeps it and reports it at exit instead.
    @staticmethod
    def discard(cid, force=False, failed=False):
        if failed and ContainerEngine.keep_dead_containers():
            if len(ContainerEngine._kept) == 0:
                atexit.register(ContainerEngine._report_kept)
            ContainerEngine._kept.append(
                cid.decode() if isinstance(cid, bytes) else cid)
            return
        ContainerEngine.run(
            ["container", "rm"] + (["-f"] if force else []) + [cid],
            check=False)

    # Printed last, as a footnote after the build's own failure output.
    @staticmethod
    def _report_kept():
        if len(ContainerEngine._kept) == 0:
            return
        print("kept %d container(s) SEINE_KEEP_DEAD_CONTAINERS asked for:"
              % len(ContainerEngine._kept))
        for cid in ContainerEngine._kept:
            print("  %s" % cid)
        print("  remove them with: %s rm -f %s"
              % (" ".join(ContainerEngine._podman_cmd([])),
                 " ".join(ContainerEngine._kept)))

    # conmon's console-socket file lands under TMPDIR too, and AF_UNIX
    # caps socket paths at 108 bytes -- easy to exceed under a deep CI
    # workdir. Symlink a short hashed name under XDG_RUNTIME_DIR instead,
    # reused across calls sharing the same 'cache'.
    @staticmethod
    def _short_tmpdir(cache):
        base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        short = os.path.join(base, "seine-tmp-%s" %
                             hashlib.sha1(cache.encode()).hexdigest()[:12])
        if not os.path.islink(short):
            try:
                os.symlink(cache, short)
            except FileExistsError:
                pass
        return short

    # TMPDIR here is where '--mount=type=cache' build steps cache their
    # fetched packages. Redirected under our own cache dir so 'seine
    # cache info'/'clear' see and empty it too, instead of /var/tmp.
    @staticmethod
    def _podman_env():
        cache = ContainerEngine.cache("bootstraps")
        os.makedirs(cache, exist_ok=True)
        return dict(os.environ, TMPDIR=ContainerEngine._short_tmpdir(cache))

    @staticmethod
    def _podman_cmd(cmd):
        cmd.insert(0, ContainerEngine.runroot())
        cmd.insert(0, "--runroot")
        cmd.insert(0, ContainerEngine.root())
        cmd.insert(0, "--root")
        cmd.insert(0, "podman")
        return cmd
    # Podman writes straight to the task's log file (not a pipe we read),
    # so a long build stays tail-able while it runs. On failure, _said()
    # reads the tail back from that file for the error message.
    SAID_LINES = 8
    SAID_BYTES = 8192

    @staticmethod
    def _said(output):
        name = getattr(output, "name", None)
        if not isinstance(name, str):
            return None
        try:
            with open(name, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - ContainerEngine.SAID_BYTES))
                tail = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        said = tail.splitlines()[-ContainerEngine.SAID_LINES:]
        return "\n".join(line.rstrip() for line in said).strip() or None

    @staticmethod
    def run(cmd, check=False):
        cmd = ContainerEngine._podman_cmd(cmd)
        output = tasks.output()
        run = spawn_own_pgroup(cmd, stdout=output,
                               stderr=subprocess.STDOUT if output else None,
                               env=ContainerEngine._podman_env())
        run.wait()
        if check and run.returncode != 0:
            raise subprocess.CalledProcessError(
                run.returncode, cmd, output=ContainerEngine._said(output))
        return run
    @staticmethod
    def check_output(cmd):
        cmd = ContainerEngine._podman_cmd(cmd)
        run = spawn_own_pgroup(cmd, stdout=subprocess.PIPE,
                               env=ContainerEngine._podman_env())
        out, _ = run.communicate()
        if run.returncode != 0:
            raise subprocess.CalledProcessError(run.returncode, cmd, output=out)
        return out
    @staticmethod
    def Popen(cmd, stdin=None, stdout=None, stderr=None):
        cmd = ContainerEngine._podman_cmd(cmd)
        return spawn_own_pgroup(cmd, stdin=stdin, stdout=stdout, stderr=stderr,
                                env=ContainerEngine._podman_env())
    # Like run(), but returns (exit code, combined output) instead of
    # raising or writing to the terminal -- for 'bash', which often
    # exits non-zero on purpose (e.g. grep finding nothing).
    @staticmethod
    def run_captured(cmd):
        cmd = ContainerEngine._podman_cmd(cmd)
        run = spawn_own_pgroup(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=ContainerEngine._podman_env())
        out, _ = run.communicate()
        return run.returncode, out.decode("utf-8", "replace")
