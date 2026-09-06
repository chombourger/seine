# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Everything about running podman and laying out what seine keeps on
# disk between builds (build_dir() and everything under it) -- split
# out of seine/utils.py, which grew too large to navigate.

import atexit
import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile

from seine import tasks

# A process group of its own for whatever cmd is (podman, ansible-playbook):
# Ctrl-C goes to the terminal's process group, so the key asking seine to
# start no more steps would otherwise kill the ones it waits for. Moving
# the child into its own group *after* spawning it -- rather than asking
# subprocess for a new session up front with start_new_session=True --
# keeps CPython on its posix_spawn fast path instead of a real fork():
# fork() runs every pthread_atfork handler registered in this process,
# including grpc's (mtda's transport, once '/target connect' opens a
# channel), which waits for its own thread pool to go idle before letting
# the fork proceed -- and hangs forever once a live console/EVT stream
# keeps that pool permanently busy. posix_spawn never calls fork(), so it
# never trips that wait. A plain process group, not a full session, is all
# Ctrl-C isolation ever needed.
#
# That fast path also demands an executable with a directory component
# (CPython's own eligibility check on os.path.dirname(executable)) --
# 'podman'/'ansible-playbook' alone does not qualify, only what PATH
# resolves them to. Passing that resolved path as executable=, argv[0]
# left as cmd[0], gets the fast path without changing what the process
# sees itself invoked as.
def spawn_own_pgroup(cmd, **kwargs):
    if "executable" not in kwargs and not os.path.dirname(cmd[0]):
        resolved = shutil.which(cmd[0])
        if resolved:
            kwargs["executable"] = resolved
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        os.setpgid(proc.pid, proc.pid)
    except OSError:
        pass  # already past the point where it matters (exited/exec'd)
    return proc

class ContainerEngine:
    @staticmethod
    def hasImage(name):
        result = ContainerEngine.run(["image", "exists", name], check=False)
        return result.returncode == 0
    # The image's own id, which changes whenever it is rebuilt even under
    # the same name -- what tells an image built from it that it is stale.
    @staticmethod
    def imageId(name):
        if ContainerEngine.hasImage(name) == False:
            return None
        return ContainerEngine.check_output(
            ["image", "inspect", "-f", "{{.Id}}", name]).decode().strip()
    # A label seine put on an image when it built it. Missing for images
    # built by a seine that did not label them, which then rebuilds them.
    @staticmethod
    def imageLabel(name, label):
        if ContainerEngine.hasImage(name) == False:
            return None
        value = ContainerEngine.check_output(
            ["image", "inspect", "-f", "{{index .Labels \"%s\"}}" % label,
             name]).decode().strip()
        return None if value in ["", "<no value>"] else value
    # Builds a throwaway container from 'image', streams its filesystem out
    # via 'container export' and extracts it into output_dir, then removes
    # the container. 'member_filter(name)', if given, is called for every
    # tar member and only extracts the ones it accepts -- used to pull a
    # single directory or a handful of files out of an otherwise large
    # image without writing the whole thing to disk first.
    # A name for a container that exists only to be read and removed again.
    #
    # It was the image's own name with the slashes taken out, which two
    # builds extracting from one image both picked: the second was told the
    # name was in use and failed the step. Nothing waits for this one --
    # it is created, streamed out and removed inside one call, so there is
    # nothing for a waiter to find -- and two of them have no reason not to
    # run at once. So each takes a name of its own.
    #
    # Random rather than the pid: a build's steps run in threads of one
    # process, and two of those extract different images at the same time.
    @staticmethod
    def _scratch_name(image):
        return "%s-%s" % (image.replace("/", "-"), os.urandom(4).hex())

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
    # Everything seine creates while building, as opposed to a spec's own
    # deliverable, which this never moves. One variable relocates it all;
    # each SEINE_*_DIR below still overrides its own piece and wins when
    # both are set. Unset, it defaults to ./build under the working
    # directory rather than scattering things under the home directory --
    # made absolute so a step that changes its own working directory
    # (podman, ansible) still resolves it to the same place.
    @staticmethod
    def build_dir():
        return os.path.abspath(os.environ.get("SEINE_BUILD_DIR") or "./build")
    # Rootless podman's default graph-root is shared with every other user of
    # podman on the machine; seine relocates it under its own directory so
    # concurrent builds/tests don't collide with (or get confused by) images
    # from unrelated podman use. External tools that need to reach the same
    # storage (e.g. ansible's podman connection plugin) must be pointed at
    # this same path, hence it being its own method rather than inlined.
    @staticmethod
    def root():
        return os.path.join(ContainerEngine.build_dir(), "containers")
    # Where large, short-lived files go while a build is running. Not
    # /tmp, which is commonly a tmpfs and so memory -- a kernel source
    # tree is several gigabytes and unpacking one there has been known to
    # take the machine down with it. Not the working directory either,
    # which is someone's checkout. SEINE_TMP_DIR overrides SEINE_BUILD_DIR.
    @staticmethod
    def scratch():
        path = os.environ.get("SEINE_TMP_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "tmp")
        os.makedirs(path, exist_ok=True)
        return path
    # Everything seine keeps between builds because making it again costs
    # time rather than because a build needs it kept. One place for it, so
    # 'seine cache' has one place to look and to empty.
    #
    # SEINE_CACHE_DIR names that place outright; unset, it is under
    # SEINE_BUILD_DIR, on the same reasoning as everything else it moves.
    @staticmethod
    def cache(*names):
        root = os.environ.get("SEINE_CACHE_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "cache")
        return os.path.join(root, *names)
    # Host-side apt archives cache, bind-mounted into ansible's target
    # container so package downloads survive across builds/suites.
    # Scoped per suite: package filenames already carry the architecture,
    # so arm64/amd64 fetches for the same suite safely share one dir.
    #
    # A root of its own, not a cache subdirectory: SEINE_DL_DIR moves it
    # the way SEINE_CACHE_DIR moves the rest. Unset, it is under
    # SEINE_BUILD_DIR.
    @staticmethod
    def downloads_root():
        return os.environ.get("SEINE_DL_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "downloads")
    @staticmethod
    def downloads(suite):
        path = os.path.join(ContainerEngine.downloads_root(), suite)
        os.makedirs(path, exist_ok=True)
        return path
    # A sibling of downloads(suite), not a subdirectory of it: that one is
    # synced flat against /var/cache/apt/archives, and apt's lists are a
    # different cache with a different shape. No caller yet -- this is
    # host-side storage for whatever resolves a suite's package graph
    # against apt's indices without re-fetching Sources/Packages every run.
    @staticmethod
    def downloads_lists(suite):
        path = os.path.join(ContainerEngine.downloads_root(), suite, "lists")
        os.makedirs(path, exist_ok=True)
        return path
    # Host-side apt repository holding the packages rebuilt from the spec's
    # 'packages' section, bind-mounted into ansible's target container so
    # the playbooks can install them like any other package.
    #
    # One per release, holding every architecture, the way a distribution's
    # archive does: a package's own name says which architecture it is for,
    # an 'Architecture: all' package is for all of them and has no business
    # being kept per architecture, and a source package belongs to none of
    # them. apt reads a flat repository's index once and takes from it what
    # the architecture it was asked about can use.
    @staticmethod
    def packages(release):
        path = ContainerEngine.cache("packages", release)
        os.makedirs(path, exist_ok=True)
        return path
    # Host-side cache for the buildd chroot tarballs sbuild unpacks for
    # every package it builds. Producing one costs a full mmdebstrap run
    # (~150MB, minutes), so it is kept out of the container and reused.
    # The architecture here is the chroot's own, which for a cross build
    # is the build architecture rather than the target's.
    @staticmethod
    def chroots(release, architecture):
        path = ContainerEngine.cache("chroots", release, architecture)
        os.makedirs(path, exist_ok=True)
        return path
    # Where podman keeps the state of what is running, as opposed to the
    # images it has stored. The two belong together: podman's default is one
    # runroot per user, so two storages sharing it are two builds sharing
    # podman's idea of what is mounted. seine's storage is always its own
    # (under build_dir()'s root()), so it always pairs its own runroot too.
    @staticmethod
    def runroot():
        return os.path.join(ContainerEngine.root(), "run")
    # Where a spec's bare filename lands -- a path of its own is never
    # touched. SEINE_DEPLOY_DIR says outright; unset, it is under
    # SEINE_BUILD_DIR.
    @staticmethod
    def deploy_root():
        return os.environ.get("SEINE_DEPLOY_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "deploy")
    # A vendor repository (seine/vendor's own deploy_repository()) is
    # unlike the rest of deploy_root(): a shared, machine-independent
    # *input* -- the same fetched .debs regardless of which spec or which
    # machine last built against them -- rather than a build's own local
    # output (a spec's own artifact, packages()). Worth relocating on its
    # own, separately from deploy_root(), so it can be pointed at a
    # network mount several machines share without dragging every other,
    # genuinely per-machine deploy/ artifact onto that same mount.
    # SEINE_VENDOR_DIR names it outright; unset, it stays right where it
    # always was, under deploy_root().
    @staticmethod
    def vendor_root():
        return os.environ.get("SEINE_VENDOR_DIR") \
               or os.path.join(ContainerEngine.deploy_root(), "vendor")
    # Kept apart from the scratch space, so a log survives what
    # 'seine cache clear scratch' or a stray 'rm -rf tmp' takes with it.
    # SEINE_LOG_DIR names it outright; unset, it is under SEINE_BUILD_DIR.
    @staticmethod
    def logs_root():
        return os.environ.get("SEINE_LOG_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "logs")
    # One JSON file per conversation with the AI chat (seine/tui/ai,
    # which owns what goes in it) -- this only says where.
    # SEINE_CHAT_DIR names it outright; unset, it is under
    # SEINE_BUILD_DIR, the same as everything else in this family.
    @staticmethod
    def chats():
        return os.environ.get("SEINE_CHAT_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "chats")
    # One append-only JSONL file per day, every gated AI tool call
    # (seine/tui/ai's own '_audit()') -- across conversations, unlike
    # chats() above, since "did the AI ever run source-rm today" cuts
    # across them. Kept apart from scratch()/cache the same way
    # logs_root() is: 'seine cache clear' must not eat it.
    # SEINE_AUDIT_DIR names the root outright; unset, it is under
    # SEINE_BUILD_DIR.
    @staticmethod
    def audit():
        import datetime
        root = os.environ.get("SEINE_AUDIT_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "audit")
        os.makedirs(root, exist_ok=True)
        stamp = datetime.date.today().strftime("%Y%m%d")
        return os.path.join(root, "%s.jsonl" % stamp)
    # Where a pulled package source (seine/sources.py) or a 'bash' tool
    # call lands -- unlike scratch(), kept across builds on purpose, so
    # a source stays pulled until 'source rm' says otherwise. Not named
    # 'scratch' for that reason: that name is already taken by the
    # ephemeral, build-local tmp above. SEINE_WORKBENCH_DIR names it
    # outright; unset, it is under SEINE_BUILD_DIR.
    @staticmethod
    def workbench():
        path = os.environ.get("SEINE_WORKBENCH_DIR") \
               or os.path.join(ContainerEngine.build_dir(), "workbench")
        os.makedirs(path, exist_ok=True)
        return path
    # The image builds cache the packages they fetch, through whatever the
    # engine offers for it -- '--mount=type=cache' today, kept under
    # TMPDIR. Unset, that is /var/tmp, so the archives end up somewhere
    # 'seine cache info' does not count and 'seine cache clear' does not
    # empty: a cleared cache that still feeds the next bootstrap.
    # The lock a seine invocation takes on the storage it uses. Shared by
    # what reads it or adds to it, exclusively by what sweeps it: two
    # builds started from two terminals run beside each other, and a
    # 'seine cache clear' typed in a third waits for both rather than
    # removing the chroots and images they are standing on.
    @staticmethod
    def storage_lock():
        return os.path.join(ContainerEngine.root(), "images")

    # Containers a build would have removed on its way out, kept instead
    # when SEINE_KEEP_DEAD_CONTAINERS is set. What podman recorded about an
    # exec -- the exit file conmon writes among it -- lives under the
    # container and goes when the container does, so a teardown running
    # while someone is still reading the failure takes the evidence with
    # it. Off by default: these are gigabytes each and nothing reaps them.
    _kept = []

    @staticmethod
    def keep_dead_containers():
        value = os.environ.get("SEINE_KEEP_DEAD_CONTAINERS")
        return value is not None and value not in ["", "0", "no"]

    # Removes a container, or keeps it and says so at the end. 'failed'
    # tells the two apart: a container a successful step is finished with
    # is not evidence of anything, and keeping those would fill a disk.
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

    # Said once, when everything else has been said: a build prints its
    # failure first, and this is a footnote to it. With the storage named,
    # since it is not podman's default and 'podman rm' would not find them.
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

    # conmon's own console-socket file -- created for a tty container
    # (sbuild's own, see BuilderImage.exec()'s 'tty') -- is a Go temp file
    # under TMPDIR too, and AF_UNIX cuts a socket path off at 108 bytes.
    # 'cache' can easily be that long on its own once it is a deep CI
    # workdir (an avocado job-results tree, say); a short-named symlink
    # under XDG_RUNTIME_DIR keeps the joined path under the limit without
    # moving where anything actually lands. Reused across calls that share
    # the same 'cache' (its own name is a hash of it), so this never grows
    # unbounded within one XDG_RUNTIME_DIR.
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
    # A container's output belongs to the task that started it: with
    # nothing capturing, it goes to the terminal as it always has, and
    # with a task capturing it goes to that task's file. Passing the file
    # rather than reading the pipe ourselves keeps podman writing straight
    # into it, so a long build can be watched with tail while it runs.
    # The last of what a failing command wrote, so that an error is a
    # sentence rather than a number.
    #
    # Read back from the task's log rather than captured through a pipe:
    # podman is handed the file descriptor and writes into it itself, which
    # is what lets a long step be watched with tail while it runs. A pipe
    # would take that away to say the same thing.
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
    # stdout+stderr combined as text plus the exit status: run()'s output
    # would otherwise hit the terminal raw (garbling the TUI, mid-chat),
    # and check_output() raises on a non-zero exit rather than handing it
    # back -- wrong for 'bash', whose command commonly exits non-zero on
    # purpose (grep finding nothing).
    @staticmethod
    def run_captured(cmd):
        cmd = ContainerEngine._podman_cmd(cmd)
        run = spawn_own_pgroup(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=ContainerEngine._podman_env())
        out, _ = run.communicate()
        return run.returncode, out.decode("utf-8", "replace")
