# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import contextlib
import os
import subprocess
import tarfile
import tempfile

from seine               import analyze
from seine               import cache_index
from seine               import packages
from seine               import progress
from seine               import tasks
from seine               import utils
from seine.ansible_runner import AnsibleContainerRunner
from seine.bootstrap      import HostBootstrap
from seine.bootstrap      import TargetBootstrap
from seine.imager         import Imager
from seine.imager_appliance import ImagerAppliance
from seine.imager_kernel  import ImagerKernel
from seine.transport_bootstrap import TransportBootstrap
from seine.sbom           import SBOM
from seine.sbuild         import BuilderImage
from seine.tasks          import Task
from seine.utils          import ContainerEngine

class Image:
    def __init__(self, partitionHandler, options=None):
        self.partitionHandler = partitionHandler
        self.options = options if options is not None else {}
        self.hostBootstrap = None
        self._cid = None
        self.targetBootstrap = None
        self._from = None
        self._image = None
        self._keep = options["keep"]
        self._output = None
        self._tarball = None
        self._verbose = options["verbose"]

    def __del__(self):
        if self._tarball:
            self._unlink(self._tarball, "root file-system as a tarball")

    def _unlink(self, path, descr):
        if self._keep:
            print("keeping '%s' (%s) as requested" % (path, descr))
        else:
            os.unlink(path)

    def parse(self, spec):
        if "image" not in spec:
            raise ValueError("'image' not found in provided specification!")

        distro = spec["distribution"] if "distribution" in spec else {}
        if "source" not in distro:
            distro["source"] = "debian"
        if "release" not in distro:
            distro["release"] = "buster"
        if "architecture" not in distro:
            distro["architecture"] = "amd64"
        if "uri" not in distro:
            distro["uri"] = "http://ftp.debian.org/debian"
        spec["distribution"] = distro

        # Validated here rather than when a bootstrap first reads them, so
        # a mistyped feed is reported against the specification instead of
        # halfway through building an image from it.
        utils.feeds(distro)

        image = spec["image"]
        if "filename" not in image:
            raise ValueError("output 'filename' not specified in 'image' section!")
        self._output = image["filename"]

        # Validated here so a bad 'packages' section is reported when the
        # specification is parsed rather than once the build reaches it.
        self.packages = packages.parse(spec)

        # And so is this: whether a source has anything vouching for it is
        # knowable without fetching it, so a specification that asked for
        # hashes and has none is told now rather than after a download.
        if self.options.get("require_hashes"):
            self._require_hashes()

        spec = self._parse_playbooks(spec)

        # Make selected 'baseline' visible in the parsed spec (for our test-suite)
        if self._from:
            spec["baseline"] = self._from

        self.spec = spec
        return self.spec

    def _require_hashes(self):
        missing = packages.unvouched(self.packages)
        if len(missing) == 0:
            return
        report = ["--require-hashes was given and %d source%s has nothing "
                  "vouching for it:" % (len(missing),
                                        "" if len(missing) == 1 else "s")]
        for uri, where, setting in missing:
            report.append("  %s" % uri)
            report.append("    add '%s:'%s"
                          % (setting, " to %s" % where if where else ""))
        report.append("apt:// and git:// sources need none: an archive "
                      "signature and a commit hash answer for themselves.")
        raise ValueError("\n".join(report))

    def _parse_playbooks(self, spec):

        playbooks = spec["playbook"] if "playbook" in spec else []
        if type(playbooks) != type([]):
            raise ValueError("'playbook' shall be a list of Ansible playbooks!")

        # Check provided playbooks
        index = 1
        for playbook in playbooks:
            if type(playbook) != type({}):
                raise ValueError("playbook #%d is not a dictionary!" % index)
            playbook["hosts"] = "all"
            if "priority" not in playbook:
                playbook["priority"] = 500
            index = index + 1

        # Order them by ascending priority
        playbooks = sorted(playbooks, key=lambda p: p["priority"])

        # Get selected baseline and remove the "priority" setting since not understood
        # by Ansible (and not needed anymore)
        for playbook in playbooks:
            if "baseline" in playbook:
                if self._from is None:
                    # highest prio 'baseline' wins
                    self._from = playbook["baseline"]
                playbook.pop("baseline", None)
            playbook.pop("priority", None)

        spec["playbook"] = playbooks
        return spec

    def rootfs(self):
        if self._from is None:
            self._from = self.targetBootstrap.name

        runner = AnsibleContainerRunner(
            self._from, self.spec["distribution"], self.options, verbose=self._verbose)
        self._cid = runner.run(self.spec["playbook"])

    # What was exported is a root file-system, rather than whatever came out
    # of a podman that said nothing was wrong.
    #
    # 'check=True' catches an export that failed and nothing else. One that
    # exits zero having written a tar with no file-system in it is taken as
    # good, written into the image, and reported three steps later by
    # libguestfs as
    #
    #   internal_write: open: /etc/fstab: No such file or directory
    #
    # which says nothing about where it went wrong. A root file-system has
    # an /etc: looking for it costs a walk of the first few thousand
    # members of a tar this build is about to read in full anyway.
    ROOT_EVIDENCE = 5000

    def _exported(self, tarball):
        with tarfile.open(tarball) as tar:
            for count, member in enumerate(tar):
                if member.name.lstrip("./").startswith("etc/"):
                    return tarball
                if count >= Image.ROOT_EVIDENCE:
                    break
        raise RuntimeError(
            "the exported root file-system holds no '/etc' (%s): the "
            "container it came from was empty or the export was cut short"
            % tarball)

    def build_tarball(self):
        failed = True
        try:
            self._tarball = None
            # In the scratch space rather than the working directory: a
            # root file-system nobody asked to keep is large, and the
            # working directory is someone's checkout. A build that dies
            # leaves it where 'seine cache clear scratch' will find it.
            image = tempfile.NamedTemporaryFile(
                mode="w", delete=False, dir=ContainerEngine.scratch(),
                prefix="root-", suffix=".tar")
            ContainerEngine.run(["container", "export", "-o", image.name, self._cid], check=True)
            self._tarball = self._exported(image.name)
            failed = False
        except subprocess.CalledProcessError:
            os.unlink(image.name)
            raise
        finally:
            if self._cid:
                # The container is still running ('sleep infinity', kept
                # alive for ansible to connect into) at this point, so it
                # needs a forceful removal rather than a plain 'rm'.
                ContainerEngine.discard(self._cid, force=True, failed=failed)
                self._cid = None
            # No prune here. It is machine-wide -- the appliance being
            # prepared beside this is made of images it would consider
            # dangling -- so it happens once, when the build is done and
            # holds nothing, and only if no other build is running.

    def _size_partitions(self):
        tar = tarfile.open(self._tarball, "r")
        files = tar.getmembers()
        for f in files:
            self.partitionHandler.distribute(f)
        tar.close()
        self.partitionHandler.compute_sizes()
        self.partitionHandler.print_stats()

    # Beside the image it is about to become, and not in the scratch space
    # with the rest: the imager finishes by renaming this to the filename the
    # specification asked for, and a rename only works within one filesystem.
    # Written where the output goes rather than in the working directory, so
    # a specification writing to another drive renames rather than failing
    # with EXDEV.
    def _empty_disk(self):
        size = self.partitionHandler.disk_size()
        image = tempfile.NamedTemporaryFile(
            mode="wb", delete=False,
            dir=os.path.dirname(os.path.abspath(self._output)))
        image.truncate(size)
        image.close()
        self._image = image.name

    # What a build is made of, and what each step waits for. The order this
    # produces is the order the steps used to be written in; what is new is
    # that it is derived from the dependencies rather than from the layout
    # of the source.
    #
    # Two of them are worth reading twice. Packages need the host bootstrap
    # and not the target one: they are built in a chroot of the build
    # architecture. And the imager needs the packages -- its own kernel is
    # installed from the repository they land in, so it boots the kernel
    # the specification rebuilt rather than the distribution's.
    def tasks(self):
        distro = self.spec["distribution"]

        self.hostBootstrap = HostBootstrap(distro, self.options)
        self.targetBootstrap = TargetBootstrap(distro, self.options)
        builder = packages.Builder(
            distro, self.options, BuilderImage(distro, self.options))

        # '--packages-only' stops here: the packages are what a build makes
        # that another machine can be handed, and a machine filling a cache
        # for others to import has no use for the image at the end of it.
        # The target bootstrap goes with the rest of the image -- packages
        # are built in a chroot of the build architecture and never touch
        # it.
        if self.options.get("packages_only"):
            return [self.hostBootstrap.task()] \
                   + builder.tasks(self.packages, self.hostBootstrap)

        # Each of these is declared beside the code that runs it, so what
        # a step needs is written where someone changing that step will
        # see it. What is left here is the handful of steps this class
        # implements itself, and the order they make between them.
        return [
            self.hostBootstrap.task(),
            self.targetBootstrap.task(self.hostBootstrap),
        ] + builder.tasks(self.packages, self.hostBootstrap) + [
            Task("rootfs", self.rootfs, needs=["bootstrap-target", "packages"]),
            Task("tarball", self.build_tarball, needs=["rootfs"]),
            SBOM(distro, self.options).task(self),
            Task("disk", self._prepare_disk, needs=["tarball"]),
        ] + Imager(self).tasks()

    # Every container image a build of this specification would use, named
    # without building any of them.
    #
    # Asked of the same classes the build instantiates rather than worked out
    # from the shape of their names: what an image is called is theirs to
    # decide, and a copy of the formula over here would go quietly wrong the
    # day one of them changes.
    #
    # The appliance is a cross build's, so it is named only for one. The
    # imager's kernel needs a target bootstrap to stand on, which is what a
    # build gives it when it reaches that step; here it only has to exist for
    # the name to be asked of it.
    def images(self):
        distro = self.spec["distribution"]
        if self.targetBootstrap is None:
            self.targetBootstrap = TargetBootstrap(distro, self.options)
        named = [HostBootstrap(distro, self.options).name,
                 self.targetBootstrap.name,
                 BuilderImage(distro, self.options).name]
        try:
            kernel = ImagerKernel(self)
            named.append(kernel.name)
            if distro["architecture"] != utils.HOST_ARCH:
                named.append(ImagerAppliance(self, kernel).name)
        except ValueError:
            # A specification with no imager kernel for its architecture
            # names none; that is the build's complaint to make, not ours.
            pass
        named.append(TransportBootstrap(
            self._from or self.targetBootstrap.name, distro, self.options).name)
        return named

    def _prepare_disk(self):
        self._size_partitions()
        self._empty_disk()

    # Everything the build would do, and what it would leave alone.
    # Nothing is fetched, built or written: the same graph run() would
    # walk, printed instead of walked.
    def plan(self):
        distro = self.spec["distribution"]
        what = "the packages" if self.options.get("packages_only") \
               else "'%s'" % self._output
        # How many at a time only when it is more than one: a build that runs
        # its steps in a row is what a plan describes by default, and saying
        # '1 step at a time' says nothing.
        jobs = self.options.get("jobs", 1)
        print("would build %s for %s/%s%s"
              % (what, distro["release"], distro["architecture"],
                 ", %d steps at a time" % jobs if jobs > 1 else ""))

        builder = packages.Builder(
            distro, self.options, BuilderImage(distro, self.options))
        current = builder.current(self.packages)
        if len(current) > 0:
            print("\nalready built, and not built again:")
            for package, architecture, stamp in current:
                print("  %-30s %s" % (builder.label(package, architecture),
                                      os.path.basename(stamp)))
            print("\n'--rebuild' builds them anyway.")

        print("\nsteps:")
        tasks.describe(self.tasks())
        return 0

    def build(self):
        if self.options.get("dry_run"):
            return self.plan()
        try:
            jobs = self.options.get("jobs", 1)
            verbose = self.options.get("verbose", False)

            # Whose downloads this build is about to use. Not per .deb:
            # which of them apt takes out of the archive cache is decided by
            # apt inside the container, and a release is the smallest thing
            # seine can honestly say was used.
            release = self.spec["distribution"]["release"]
            cache_index.Index().hit(cache_index.DOWNLOADS, release)

            # A step's output goes to a file of its own unless someone
            # asked to watch it go by: several steps at once cannot share
            # a terminal, and one step at a time buries what is worth
            # knowing in what is not.
            logs = None
            if verbose == False or jobs > 1:
                logs = tempfile.mkdtemp(dir=ContainerEngine.scratch(),
                                        prefix="logs-")
                print("output under %s" % logs)

            steps = self.tasks()
            display = None
            if verbose == False:
                display = progress.Display(total=len(steps),
                                           environment=os.environ)
            # What every step cost, kept for 'seine analyze' to read back.
            # Written in a finally because a build that failed is the one
            # worth reading: the steps that did run still say where the
            # time went, and the build that resumes this one is filed
            # with it.
            ok = False
            machine = analyze.watching()
            try:
                with machine, (display if display is not None
                               else contextlib.nullcontext()):
                    tasks.run(steps, jobs=jobs, logs=logs, verbose=verbose,
                              display=display)
                ok = True
            finally:
                analyze.record(steps, analyze.spec_digest(self.spec),
                               jobs=jobs, ok=ok, machine=machine)

            # What the caches spared this build, and what it had to make.
            # Printed after the steps rather than by them: it is the answer
            # to 'is the cache working', and one line at the end is where
            # someone looks for it.
            said = cache_index.summary()
            if said is not None:
                print(said)
        except:
            if self._image is not None:
                os.unlink(self._image)
            raise
