# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import contextlib
import functools
import os
import subprocess
import tarfile
import tempfile
import time

from seine               import analyze
from seine               import cache_index
from seine               import packages
from seine               import progress
from seine               import tasks
from seine               import utils
from seine.utils          import redactions
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
        # Both only ever set for real by parse() -- defaulted here too so
        # a specification with no 'image:' section (a vendor-only one,
        # say) still has something safe to read: 'packages' as "nothing
        # rebuilt from source" rather than an AttributeError, and 'spec'
        # as the tell plan()/tasks() use to refuse cleanly instead of
        # reaching into a spec that was never parsed.
        self.packages = []
        self.spec = None
        # A specification's own 'multiconfig:' groups (BuildCmd.
        # _parse_multiconfig()), name -> the BuildCmd that parsed it.
        # Empty for a specification with none -- tasks() below then adds
        # nothing beyond what it always built.
        self.subbuilds = {}

    def __del__(self):
        if self._tarball:
            self._unlink(self._tarball, "root file-system as a tarball")

    def _unlink(self, path, descr):
        if self._keep:
            print("keeping '%s' (%s) as requested" % (path, descr))
        else:
            os.unlink(path)

    def parse(self, spec):
        distro = utils.distribution(spec)

        if "image" in spec:
            image = spec["image"]
            if "filename" not in image:
                raise ValueError("output 'filename' not specified in 'image' section!")
            filename = image["filename"]
            # A relative filename follows SEINE_DEPLOY_DIR/SEINE_BUILD_DIR
            # the way seine's other output does, scoped per release like
            # the caches are, so two releases built from one checkout
            # don't overwrite each other's image; an absolute one says
            # where it goes and is never redirected.
            if os.path.isabs(filename) == False:
                deploy = os.path.join(ContainerEngine.deploy_root(), distro["release"])
                os.makedirs(deploy, exist_ok=True)
                filename = os.path.join(deploy, filename)
            self._output = filename
        else:
            # No 'image:' section: nothing to partition, so the root
            # file-system tarball built for it is this build's real
            # output instead (own_tasks() below).
            self._output = self._rootfs_output(distro)

        # Validated here so a bad 'packages' section is reported when the
        # specification is parsed rather than once the build reaches it.
        self.packages = packages.parse(spec)

        # And so is 'vendor:', for the same reason -- 'seine build' never
        # acts on it, but a specification with a typo in it should not
        # wait for 'seine vendor' to say so.
        from seine import vendor
        vendor.suites(vendor.parse(spec), distro)
        vendor.exclusions(spec)

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

    # Named from the spec file's own basename ('main.yaml' -> 'main.tar')
    # rather than the release: two image-less specs sharing a release
    # would otherwise collide on one filename. Falls back to the release
    # when no file is known (e.g. BuildCmd.loads() in a test).
    def _rootfs_output(self, distro):
        files = self.options.get("files") or []
        stem = os.path.splitext(os.path.basename(files[0]))[0] \
            if len(files) > 0 else distro["release"]
        deploy = os.path.join(ContainerEngine.deploy_root(), distro["release"])
        os.makedirs(deploy, exist_ok=True)
        return os.path.join(deploy, "%s.tar" % stem)

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
        from seine import vendor
        if self._from is None:
            self._from = self.targetBootstrap.name

        distro = self.spec["distribution"]
        runner = AnsibleContainerRunner(
            self._from, distro, self.options, verbose=self._verbose,
            vendor_digest=vendor.offline_dockerfile_digest(self.spec, distro))
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

    # A 'vendor' task, resolving/fetching/indexing what 'vendor:' asks for
    # before 'packages:' reaches for it -- only when there is anything to
    # do: a 'vendor:' section that feeds no suite 'apt-pull-mode: offline'
    # actually needs is left for a plain 'seine vendor' to build whenever
    # someone wants it, same as today, since nothing here would read it.
    #
    # Narrowed to this build's own release, like 'seine vendor --suite
    # <release>' -- 'vendor:' entries scoped to some other suite are no
    # business of this build's own 'packages:'. Fetching (not resolving,
    # which still walks every architecture 'vendor:' asks for -- see
    # VendorCmd.main()'s own comment) is narrowed the same way to this
    # build's own architecture, like 'seine vendor --architecture
    # <architecture>': a foreign architecture's binaries are no more this
    # build's business than a foreign suite's.
    #
    # No task at all, rather than one that would find nothing to do, once
    # deploy/vendor/<release> already has an index in it (vendor.
    # is_deployed()): trusted outright as complete, not reresolved,
    # refetched or reindexed against the manifest -- a 'seine build' run
    # this way never touches the network, or apt, or a resolver
    # container, on account of vendoring at all.
    #
    # Short of that, reuses VendorCmd._run() itself rather than a copy of
    # it, the same way the TUI's own start_vendor() already does -- an
    # ordinary rerun with nothing changed still just freezes what an
    # earlier resolve found (see manifest_digest()), so this costs
    # nothing when the vendor repository is already current.
    def _vendor_task(self, distro):
        from seine import vendor
        entries = vendor.parse(self.spec)
        if len(entries) == 0:
            return None
        release = distro["release"]
        if release not in utils.offline_suites(distro):
            return None
        if len(vendor.entries_for(entries, release)) == 0:
            return None
        if vendor.is_deployed(release):
            return None
        wanted = [release]
        exclude = vendor.exclusions(self.spec)
        # Resolving still has to cover whatever 'distribution:
        # architectures:' asks for beyond this build's own architecture
        # -- only fetching (via 'archs=' below) is narrowed to it, same
        # as with a foreign suite.
        extra_archs = vendor.extra_architectures(self.spec)
        cmd = vendor.VendorCmd()
        cmd.options = dict(cmd.options, jobs=self.options.get("jobs", 1),
                           verbose=self.options.get("verbose", False))
        # No 'needs=["bootstrap-host"]': this task bootstraps its own
        # force_online=True HostBootstrap (see bootstrap.py's own comment
        # on why that is a distinct, always-online image), so it never
        # touches whatever 'bootstrap-host' builds -- and once that can
        # itself depend on 'vendor' finishing first (shared_tasks(),
        # below), a dependency back the other way would be a cycle.
        return Task("vendor",
                    functools.partial(cmd._run, distro, entries, exclude,
                                      wanted, False,
                                      archs=[distro["architecture"]],
                                      extra_archs=extra_archs))

    # The host bootstrap and the packages built in a chroot of it -- the
    # half of a build several specifications can share when they agree on
    # a release, since neither varies by architecture. A caller building
    # several images together passes its own 'hostBootstrap' and the union
    # of every image's 'requested' packages; left unset, this builds its own.
    def shared_tasks(self, hostBootstrap=None, requested=None):
        from seine import vendor
        distro = self.spec["distribution"]
        if hostBootstrap is not None:
            self.hostBootstrap = hostBootstrap
        else:
            vendor_digest = vendor.offline_dockerfile_digest(self.spec, distro)
            self.hostBootstrap = HostBootstrap(distro, self.options,
                                               vendor_digest=vendor_digest)
        vendor_task = self._vendor_task(distro)
        builder = packages.Builder(
            distro, self.options, BuilderImage(distro, self.options),
            redactions(self.spec))
        # 'bootstrap-host' waits on 'vendor' finishing first exactly
        # when going offline actually needs a fresh vendor run: without
        # this, HostBootstrap's own apt-get (now itself vendor-backed
        # when offline) would look for a repository the 'vendor' task
        # below exists to build, and find nothing there yet.
        shared = [self.hostBootstrap.task(
            needs=["vendor"] if vendor_task is not None else None)]
        if vendor_task is not None:
            shared.append(vendor_task)
        return shared + builder.tasks(
            requested if requested is not None else self.packages,
            self.hostBootstrap,
            vendor_task=vendor_task.name if vendor_task is not None else None)

    # The rest: the target bootstrap this image's own root file-system is
    # assembled in, and everything built on top of it -- never shared
    # between specifications, though two naming the same tag still
    # collapse into one task if whoever merges them notices. 'needs_packages'
    # names the barrier shared_tasks() ends with; 'hostBootstrap' must be
    # set by here or by an earlier shared_tasks() call.
    def own_tasks(self, hostBootstrap=None, needs_packages="packages"):
        distro = self.spec["distribution"]
        if hostBootstrap is not None:
            self.hostBootstrap = hostBootstrap
        self.targetBootstrap = TargetBootstrap(distro, self.options)

        # Each of these is declared beside the code that runs it, so what
        # a step needs is written where someone changing that step will
        # see it. What is left here is the handful of steps this class
        # implements itself, and the order they make between them.
        common = [
            self.targetBootstrap.task(self.hostBootstrap),
            Task("rootfs", self.rootfs,
                needs=["bootstrap-target", needs_packages]),
            Task("tarball", self.build_tarball, needs=["rootfs"]),
            SBOM(distro, self.options).task(self),
        ]

        # No 'image:' section: the tarball built above is the real
        # output, so move it to its deploy path instead of leaving it
        # for __del__ to discard as scratch. Needs 'sbom' too: it also
        # reads '_tarball' and must finish before this renames it away.
        if "image" not in self.spec:
            return common + [
                Task("deploy-rootfs", self._deploy_tarball,
                    needs=["tarball", "sbom"]),
            ]

        # '--rootfs-only' stops here: a tarball is what somebody wants to
        # look inside, and the disk it would be written to, and the
        # appliance that writes it, are both for booting it.
        if self.options.get("rootfs_only"):
            return common

        return common + [
            Task("disk", self._prepare_disk, needs=["tarball"]),
        ] + Imager(self).tasks(needs_packages)

    # The rootfs tarball, made permanent -- build_tarball() leaves it a
    # scratch file __del__ would otherwise unlink; renamed away instead,
    # so there is nothing left there for __del__ to find.
    def _deploy_tarball(self):
        os.rename(self._tarball, self._output)
        self._tarball = None

    # What a build is made of, and what each step waits for -- shared_tasks()
    # and, unless '--packages-only' stops here, own_tasks() after it. The
    # order is derived from the dependencies rather than from the layout of
    # the source; the imager needs the packages, since its own kernel is
    # installed from the repository they land in, so it boots the kernel the
    # specification rebuilt rather than the distribution's.
    def tasks(self):
        if self.spec is None:
            raise ValueError(
                "no 'image:' section in this specification -- nothing to build")
        shared = self.shared_tasks()
        all_tasks = shared if self.options.get("packages_only") \
            else shared + self.own_tasks()
        # Every 'multiconfig:' group's own tasks, namespaced under its
        # declared name -- the same graph a build of that group alone
        # would run, merged in rather than run separately (multiconfig.
        # merged_tasks() is what does that for the CLI's own '--' groups).
        if len(self.subbuilds) > 0:
            from seine import multiconfig
            for name, build in self.subbuilds.items():
                all_tasks += tasks.namespaced(
                    build.image.tasks(), multiconfig._label(build, name=name))
        # '--target' narrows the graph further still, to one task and
        # what it needs -- 'packages_only'/'rootfs_only' already say
        # where the *usual* stopping points are; this names any task at
        # all in whatever this list already trims to. Checked here
        # rather than left to 'ancestors()', which silently drops a name
        # it does not recognise -- built for merging a name list that
        # legitimately does not cover every task, not for catching a
        # typo in the one name a person just gave on the command line.
        target = self.options.get("target")
        if target is not None:
            names = {t.name for t in all_tasks}
            if target not in names:
                raise ValueError(
                    "no task '%s' -- available: %s"
                    % (target, ", ".join(sorted(names))))
            all_tasks = tasks.ancestors(all_tasks, [target])
        return all_tasks

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
        if self.spec is None:
            raise ValueError(
                "no 'image:' section in this specification -- nothing to plan")
        distro = self.spec["distribution"]
        if self.options.get("packages_only"):
            what = "the packages"
        elif self.options.get("rootfs_only"):
            what = "the root file-system"
        else:
            what = "'%s'" % self._output
        # How many at a time only when it is more than one: a build that runs
        # its steps in a row is what a plan describes by default, and saying
        # '1 step at a time' says nothing.
        jobs = self.options.get("jobs", 1)
        print("would build %s for %s/%s%s"
              % (what, distro["release"], distro["architecture"],
                 ", %d steps at a time" % jobs if jobs > 1 else ""))

        builder = packages.Builder(
            distro, self.options, BuilderImage(distro, self.options),
            redactions(self.spec))
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

    # One directory per specification, a run of it per build: what it wrote
    # sits beside the runs before it, not under a name no one can place.
    def _logs(self):
        files = self.options.get("files")
        base = ContainerEngine.logs_root()
        os.makedirs(base, exist_ok=True)
        if not files:
            return tempfile.mkdtemp(dir=base)
        run = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        spec = os.path.join(base, utils.digest(files, 8))
        try:
            path = os.path.join(spec, run)
            os.makedirs(path)
            return path
        except FileExistsError:
            # Two builds of one specification in the same second.
            return tempfile.mkdtemp(dir=spec, prefix="%s-" % run)

    # 'reporter' is a seine.reporter.Reporter -- progress.Display by
    # default, or a caller's own wanting to watch without owning the
    # terminal (the TUI's TextualReporter).
    def build(self, reporter=None):
        if self.options.get("dry_run"):
            return self.plan()
        if self.spec is None:
            raise ValueError(
                "no 'image:' section in this specification -- nothing to build")
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
            # knowing in what is not. A caller's own reporter always gets
            # one too -- it has no terminal to fall back on.
            self.logs = None
            if verbose == False or jobs > 1 or reporter is not None:
                self.logs = self._logs()
                print("output under %s" % self.logs)

            steps = self.tasks()
            # Taken now, before a single task has run -- not in the
            # 'finally' below, after they have. 'disk' (_prepare_disk() ->
            # PartitionHandler.compute_sizes()) writes '_size'/
            # '_start_mib'/'_end_mib' straight onto the same partition/
            # volume dicts this specification holds, the same mistake
            # already found and fixed once for playbooks
            # (ansible_runner.py's _run_playbooks()): a digest taken after
            # a real build's tasks had run never matched what reloading
            # the same files fresh, un-run, would compute -- 'seine plan'/
            # 'seine analyze' on those files could never find the record
            # a real build had just written.
            digest = analyze.spec_digest(self.spec)
            # Only the internally-built Display is entered as a context
            # manager -- a caller's reporter owns its own lifecycle.
            display = reporter
            ticker = contextlib.nullcontext()
            if display is None and verbose == False:
                display = progress.Display(total=len(steps),
                                           environment=os.environ)
                ticker = display
            # What every step cost, kept for 'seine analyze' to read back.
            # Written in a finally because a build that failed is the one
            # worth reading: the steps that did run still say where the
            # time went, and the build that resumes this one is filed
            # with it.
            ok = False
            # 'sampled' is optional on a Reporter -- Display has none, so
            # the machine is still watched and recorded, just not pushed live.
            machine = analyze.watching(callback=getattr(reporter, "sampled", None))
            try:
                with machine, ticker:
                    tasks.run(steps, jobs=jobs, logs=self.logs, verbose=verbose,
                              display=display)
                ok = True
            finally:
                # 'self._tarball' still exists here regardless: it is
                # only ever unlinked from '__del__', well after 'build()'
                # returns. 'None' for a build that never reached the
                # 'tarball' step at all ('--packages-only').
                rootfs_size = None
                if self._tarball is not None and os.path.exists(self._tarball):
                    rootfs_size = os.path.getsize(self._tarball)
                analyze.record(steps, digest, jobs=jobs, ok=ok, machine=machine,
                               rootfs_size=rootfs_size)

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
