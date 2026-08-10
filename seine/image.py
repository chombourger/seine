# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import contextlib
import os
import subprocess
import tarfile
import tempfile

from seine               import packages
from seine               import progress
from seine               import tasks
from seine               import utils
from seine.ansible_runner import AnsibleContainerRunner
from seine.bootstrap      import HostBootstrap
from seine.bootstrap      import TargetBootstrap
from seine.imager         import Imager
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

    def build_tarball(self):
        try:
            self._tarball = None
            image = tempfile.NamedTemporaryFile(mode="w", delete=False, dir=os.getcwd(), prefix='root-', suffix='.tar')
            ContainerEngine.run(["container", "export", "-o", image.name, self._cid], check=True)
            self._tarball = image.name
        except subprocess.CalledProcessError:
            os.unlink(image.name)
            raise
        finally:
            if self._cid:
                # The container is still running ('sleep infinity', kept
                # alive for ansible to connect into) at this point, so it
                # needs a forceful removal rather than a plain 'rm'.
                ContainerEngine.run(["container", "rm", "-f", self._cid], check=False)
                self._cid = None
            # Not while anything else is running: 'image prune' is
            # machine-wide, and the appliance being prepared beside this
            # is made of images it would consider dangling.
            if self.options.get("jobs", 1) <= 1:
                ContainerEngine.run(["image", "prune", "-f"], check=False)

    def _size_partitions(self):
        tar = tarfile.open(self._tarball, "r")
        files = tar.getmembers()
        for f in files:
            self.partitionHandler.distribute(f)
        tar.close()
        self.partitionHandler.compute_sizes()
        self.partitionHandler.print_stats()

    def _empty_disk(self):
        size = self.partitionHandler.disk_size()
        image = tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=os.getcwd())
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
        print("would build %s for %s/%s, %d step%s at a time"
              % (what, distro["release"], distro["architecture"],
                 self.options.get("jobs", 1),
                 "" if self.options.get("jobs", 1) == 1 else "s"))

        builder = packages.Builder(
            distro, self.options, BuilderImage(distro, self.options))
        current = builder.current(self.packages)
        if len(current) > 0:
            print("\nalready built, and not built again:")
            for package, stamp in current:
                print("  %-30s %s" % (package.name, os.path.basename(stamp)))
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
            with display if display is not None else contextlib.nullcontext():
                tasks.run(steps, jobs=jobs, logs=logs, verbose=verbose,
                          display=display)
        except:
            if self._image is not None:
                os.unlink(self._image)
            raise
