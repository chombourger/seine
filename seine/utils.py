# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import contextlib
import fcntl
import os
import platform
import subprocess
import tarfile

from seine import tasks

# Debian architecture of the machine seine itself runs on. Anything the
# specification asks for that differs from this is a cross build, whether
# that means emulating the target or compiling for it.
HOST_MACHINE_TO_ARCH = {
    "x86_64":  "amd64",
    "aarch64": "arm64",
    "armv7l":  "armhf",
}
HOST_ARCH = HOST_MACHINE_TO_ARCH.get(platform.machine(), platform.machine())

# The apt feeds a system is built from. A specification lists them
# explicitly rather than having suites guessed for it: which of -updates
# and -security a release has, and where they are served from, differs
# between distributions and between a release and its development
# version, and a suite that does not exist fails every build that follows.
#
# With none listed, the release itself is the one feed, which is what
# seine did before feeds could be listed at all.
#
# Each feed is assumed to carry sources as well as binaries, since that
# is what an archive normally serves and what rebuilding a package needs.
# 'sources: false' says otherwise, for the vendor feeds that ship
# binaries alone.
#
# 'valid-until: false' is for archives meant to stay expired -- a
# snapshot.debian.org timestamp, or a frozen mirror -- which apt would
# otherwise refuse.
def feeds(distro):
    entries = distro.get("feeds")
    if entries is None:
        entries = [{"suite": distro["release"]}]
    if type(entries) != type([]):
        raise ValueError("'feeds' shall be a list of apt feeds!")

    parsed = []
    for index, entry in enumerate(entries):
        if type(entry) != type({}):
            raise ValueError("feed #%d is not a dictionary!" % (index + 1))
        if "suite" not in entry:
            raise ValueError("feed #%d has no 'suite' specified!" % (index + 1))
        for setting in entry:
            if setting not in ["components", "sources", "suite", "uri",
                               "valid-until"]:
                raise ValueError(
                    "feed #%d ('%s') has no '%s' setting, expected one of "
                    "components, sources, suite, uri, valid-until"
                    % (index + 1, entry["suite"], setting))
        parsed.append({
            "uri":         entry.get("uri", distro["uri"]),
            "suite":       entry["suite"],
            # A distribution-wide default, so a file needing a component --
            # firmware for a board, say -- says so once without naming the
            # suites. A feed saying otherwise still decides for itself.
            "components":  entry.get("components",
                                     distro.get("components", "main")),
            "sources":     entry.get("sources", True),
            "valid_until": entry.get("valid-until", True),
        })
    return parsed

# Those feeds as apt would write them down. 'sources' adds a deb-src line
# for every feed that has said it carries any.
#
# An expired feed says so in the entry rather than in an apt.conf.d
# fragment: these lines reach every container a build talks to an archive
# from, and the option stays scoped to the feed that asked for it.
def apt_sources(distro, sources=False):
    lines = []
    for feed in feeds(distro):
        feed = dict(feed, options="" if feed["valid_until"]
                                    else "[check-valid-until=no] ")
        lines.append("deb %(options)s%(uri)s %(suite)s %(components)s" % feed)
        if sources and feed["sources"]:
            lines.append("deb-src %(options)s%(uri)s %(suite)s %(components)s" % feed)
    return lines

# One build at a time for the things two builds share.
#
# seine's caches are keyed by what they are made from, so two builds
# wanting the same one want the same bytes. What they must not do is
# write it at the same time: two mmdebstraps producing one tarball leave
# a file that is neither, and the build that trips over it does so much
# later and for no visible reason. The lock file sits beside the thing it
# guards and is held only while it is written.
#
# 'shared' is for what many may do at once but none may do while one does
# something else: several builds may add images to a storage together, and
# a prune that removes what none of them has tagged yet may not run while
# any of them is.
#
# 'blocking' says what to do when it is not free. Unset, wait; set, raise
# BlockingIOError rather than queue behind something long, for work that
# another holder will end up doing anyway.
@contextlib.contextmanager
def locked(path, shared=False, blocking=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    how = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    with open("%s.lock" % path, "w") as lock:
        fcntl.flock(lock, how if blocking else how | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

# Label carrying the digest of what an image was built from.
INPUTS_LABEL = "seine.inputs"

# Label saying what an image is, which decides whether it is worth carrying
# to another machine. Every image seine builds says so for itself: a label is
# inherited by whatever is built FROM an image, so an image that said nothing
# would answer with whatever its base said -- and the imager's kernel, built
# on the target bootstrap, would call itself a root file-system.
KIND_LABEL = "seine.kind"

# The kinds there are. Two of them can be used by another machine as they
# are; the rest stand on the root file-system, which is not carried, and an
# image whose base is not the same image is rebuilt whatever else happens to
# it.
TOOLING_KIND = "tooling"      # apt and mmdebstrap: what makes the rest
BUILDER_KIND = "builder"      # where packages are built, holding the chroot
ROOTFS_KIND = "rootfs"        # what mmdebstrap made of the archive
IMAGER_KIND = "imager"        # the kernel libguestfs boots, and its appliance
TRANSPORT_KIND = "transport"  # a baseline plus what ansible needs

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
    # What a build makes for itself, as opposed to what it keeps: the
    # container images, in podman storage of seine's own, and the scratch
    # space sources and images are assembled in. Both are gigabytes, and a
    # home directory is commonly the smallest filesystem on a build
    # machine, so SEINE_BUILD_DIR moves them to another drive together.
    # Unset, each stays where it was.
    @staticmethod
    def build_dir():
        return os.environ.get("SEINE_BUILD_DIR") or None
    # Rootless podman's default graph-root is shared with every other user of
    # podman on the machine; seine relocates it under its own directory so
    # concurrent builds/tests don't collide with (or get confused by) images
    # from unrelated podman use. External tools that need to reach the same
    # storage (e.g. ansible's podman connection plugin) must be pointed at
    # this same path, hence it being its own method rather than inlined.
    @staticmethod
    def root():
        build = ContainerEngine.build_dir()
        if build is not None:
            return os.path.join(build, "storage")
        home = os.path.expanduser("~")
        return os.path.join(home, ".local", "share", "seine")
    # Where large, short-lived files go while a build is running. Not
    # /tmp, which is commonly a tmpfs and so memory -- a kernel source
    # tree is several gigabytes and unpacking one there has been known to
    # take the machine down with it. Not the working directory either,
    # which is someone's checkout. TMPDIR wins if it is set, as it should,
    # and SEINE_BUILD_DIR over both: someone who has said where seine's
    # large files go has said it for this one too.
    @staticmethod
    def scratch():
        build = ContainerEngine.build_dir()
        path = os.path.join(build, "tmp") if build is not None \
               else os.path.join(os.environ.get("TMPDIR") or "/var/tmp", "seine")
        os.makedirs(path, exist_ok=True)
        return path
    # Everything seine keeps between builds because making it again costs
    # time rather than because a build needs it kept. One place for it, so
    # 'seine cache' has one place to look and to empty.
    #
    # SEINE_CACHE_DIR names that place outright; XDG_CACHE_HOME is honoured
    # as the standard it is, for someone who has already moved every cache
    # on the machine and did not mean to leave this one behind.
    @staticmethod
    def cache(*names):
        root = os.environ.get("SEINE_CACHE_DIR")
        if not root:
            root = os.path.join(
                os.environ.get("XDG_CACHE_HOME")
                or os.path.join(os.path.expanduser("~"), ".cache"), "seine")
        return os.path.join(root, *names)
    # Host-side apt archives cache, bind-mounted into ansible's target
    # container so package downloads survive across builds/releases.
    # Scoped per release: package filenames already carry the architecture,
    # so arm64/amd64 fetches for the same release safely share one dir.
    @staticmethod
    def downloads(release):
        path = ContainerEngine.cache("downloads", release)
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
    # podman's idea of what is mounted.
    #
    # Only for a storage seine was told to put elsewhere, which is a storage
    # being made now. The default one keeps podman's default runroot because
    # it already has it: podman records the runroot a storage was created
    # with and refuses to open it with another, so pairing them there would
    # ask everyone with a storage to throw it away first.
    @staticmethod
    def runroot():
        if ContainerEngine.build_dir() is None:
            return None
        return "%s-run" % ContainerEngine.root()
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

    @staticmethod
    def _podman_env():
        cache = ContainerEngine.cache("bootstraps")
        os.makedirs(cache, exist_ok=True)
        return dict(os.environ, TMPDIR=cache)

    @staticmethod
    def _podman_cmd(cmd):
        runroot = ContainerEngine.runroot()
        if runroot is not None:
            cmd.insert(0, runroot)
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
        run = subprocess.run(cmd, check=False, stdout=output,
                             stderr=subprocess.STDOUT if output else None,
                             env=ContainerEngine._podman_env())
        if check and run.returncode != 0:
            raise subprocess.CalledProcessError(
                run.returncode, cmd, output=ContainerEngine._said(output))
        return run
    @staticmethod
    def check_output(cmd):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.check_output(cmd, env=ContainerEngine._podman_env())
    @staticmethod
    def Popen(cmd, stdin=None, stdout=None, stderr=None):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr,
                                env=ContainerEngine._podman_env())
