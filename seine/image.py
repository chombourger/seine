# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import contextlib
import functools
import os
import shutil
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
from seine.container import ContainerEngine

class Image:
    def __init__(self, partitionHandler, options=None):
        self.partitionHandler = partitionHandler
        self.options = options if options is not None else {}
        self.hostBootstrap = None
        self._cid = None
        self.targetBootstrap = None
        self._from = None
        self._image = None
        self._initrd_output = None
        self._keep = options["keep"]
        self._output = None
        self._tarball = None
        self._verbose = options["verbose"]
        # Defaulted here so a spec with no 'image:' section still has
        # something safe to read, instead of parse()/tasks() hitting
        # an AttributeError on an unparsed spec.
        self.packages = []
        self.spec = None
        # 'multiconfig:' groups: name -> the BuildCmd that parsed it.
        self.subbuilds = {}
        # Each group's resolved 'after' set: name -> names it must
        # build after. Used by tasks() to wire group dependencies.
        self.multiconfig_after = {}

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

        if "image" in spec and "initrd" in spec:
            raise ValueError("'image' and 'initrd' sections are mutually exclusive!")

        if "image" in spec:
            image = spec["image"]
            if "filename" not in image:
                raise ValueError("output 'filename' not specified in 'image' section!")
            filename = image["filename"]
            # Relative path goes under deploy/<release>, same as other
            # output, so two releases don't overwrite each other's image.
            if os.path.isabs(filename) == False:
                deploy = os.path.join(ContainerEngine.deploy_root(), distro["release"])
                os.makedirs(deploy, exist_ok=True)
                filename = os.path.join(deploy, filename)
            self._output = filename
        elif "initrd" in spec:
            initrd = spec["initrd"]
            if "filename" not in initrd:
                raise ValueError("output 'filename' not specified in 'initrd' section!")
            filename = initrd["filename"]
            if os.path.isabs(filename) == False:
                deploy = os.path.join(ContainerEngine.deploy_root(), distro["release"])
                os.makedirs(deploy, exist_ok=True)
                filename = os.path.join(deploy, filename)
            self._initrd_output = filename
        else:
            # No 'image:' section: the rootfs tarball is this build's
            # real output instead (own_tasks() below).
            self._output = self._rootfs_output(distro)

        # Validated at parse time, not build time. 'defer_uki_check' skips
        # the 'extends: uki: initrd:' check for a multiconfig group whose
        # predecessor (not yet built) will deploy it.
        self.packages = packages.parse(
            spec, check_uki=not self.options.get("defer_uki_check"))

        # Validated here too, so a typo doesn't wait for 'seine vendor' to
        # catch it.
        from seine import vendor
        vendor.suites(vendor.parse(spec), distro)
        vendor.exclusions(spec)

        if self.options.get("require_hashes"):
            self._require_hashes()

        spec = self._parse_playbooks(spec)

        # Make selected 'baseline' visible in the parsed spec (for our test-suite)
        if self._from:
            spec["baseline"] = self._from

        self.spec = spec
        return self.spec

    # Named from the spec file's basename ('main.yaml' -> 'main.tar') so
    # two image-less specs sharing a release don't collide. Falls back
    # to the release name when no file is known (e.g. tests).
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

    # 'check=True' only catches podman failing, not an export that exits
    # zero with an empty tar. That case would otherwise only surface much
    # later, as a confusing libguestfs error. So confirm the tar actually
    # has a root file-system in it (has '/etc') before trusting it.
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
            # Scratch space, not the working directory: it's large and
            # unwanted by default, and a failed build leaves it where
            # 'seine cache clear scratch' will find it.
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
                # Container is still running ('sleep infinity'), so it
                # needs a forceful removal, not a plain 'rm'.
                ContainerEngine.discard(self._cid, force=True, failed=failed)
                self._cid = None
            # No prune here: it's machine-wide and would catch images the
            # appliance build beside this one still needs.

    # Every 'source:' a partition/volume names, once each -- the extra
    # tarballs (besides this spec's own) that a build needs.
    def _referenced_sources(self):
        return sorted({m["source"] for m in self.partitionHandler.mounts
                       if m.get("source") is not None})

    # The tarball a mount's 'source' points at. 'None' is this spec's
    # own. A group's is read from '_output' once 'deploy-rootfs' has
    # moved it there, or from '_tarball' if the group has its own
    # 'image:' and never runs that step.
    def _tarball_for(self, source):
        if source is None:
            return self._tarball
        build = self.subbuilds[source]
        if "image" in build.spec:
            return build.image._tarball
        return build.image._output

    def _size_partitions(self):
        for source in [None] + self._referenced_sources():
            tar = tarfile.open(self._tarball_for(source), "r")
            for f in tar.getmembers():
                self.partitionHandler.distribute(f, source=source)
            tar.close()
        self.partitionHandler.compute_sizes()
        self.partitionHandler.print_stats()

    # Names the task that finishes writing a referenced group's tarball,
    # so 'disk' can wait for it and never read it mid-write.
    def _source_task_names(self):
        from seine import multiconfig
        names = []
        for source in self._referenced_sources():
            build = self.subbuilds[source]
            label = multiconfig._label(build, name=source)
            terminal = "tarball" if "image" in build.spec else "deploy-rootfs"
            names.append("%s:%s" % (label, terminal))
        return names

    # Created beside the final output path, not in scratch: the imager
    # finishes by renaming this into place, and rename only works within
    # one filesystem.
    def _empty_disk(self):
        size = self.partitionHandler.disk_size()
        image = tempfile.NamedTemporaryFile(
            mode="wb", delete=False,
            dir=os.path.dirname(os.path.abspath(self._output)))
        image.truncate(size)
        image.close()
        self._image = image.name

    # Resolves/fetches/indexes 'vendor:' before 'packages:' needs it.
    # Skipped when there's nothing to do, or a vendor repo already
    # exists for this release. Narrowed to this build's own release
    # and architecture, and reuses VendorCmd._run() to keep a no-op
    # rerun cheap.
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
        # Resolving still covers every 'distribution: architectures:'
        # entry; only fetching is narrowed to this build's own one.
        extra_archs = vendor.extra_architectures(self.spec)
        cmd = vendor.VendorCmd()
        cmd.options = dict(cmd.options, jobs=self.options.get("jobs", 1),
                           verbose=self.options.get("verbose", False))
        # No 'needs=["bootstrap-host"]': it bootstraps its own
        # always-online HostBootstrap. 'bootstrap-host' can depend on
        # this task instead (shared_tasks() below) without a cycle.
        return Task("vendor",
                    functools.partial(cmd._run, distro, entries, exclude,
                                      wanted, False,
                                      archs=[distro["architecture"]],
                                      extra_archs=extra_archs))

    # The host bootstrap and chroot-built packages -- the half of a build
    # several specs sharing a release can reuse. A caller building several
    # together passes its own 'hostBootstrap' and combined 'requested'.
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
        # 'bootstrap-host' waits on 'vendor' only when there is one:
        # its apt-get would otherwise look for a repo not built yet.
        shared = [self.hostBootstrap.task(
            needs=["vendor"] if vendor_task is not None else None)]
        if vendor_task is not None:
            shared.append(vendor_task)
        return shared + builder.tasks(
            requested if requested is not None else self.packages,
            self.hostBootstrap,
            vendor_task=vendor_task.name if vendor_task is not None else None)

    # The rest: the target bootstrap and everything built on top of it --
    # never shared between specs. 'needs_packages' names the barrier
    # shared_tasks() ends with.
    def own_tasks(self, hostBootstrap=None, needs_packages="packages"):
        distro = self.spec["distribution"]
        if hostBootstrap is not None:
            self.hostBootstrap = hostBootstrap
        self.targetBootstrap = TargetBootstrap(distro, self.options)

        common = [
            self.targetBootstrap.task(self.hostBootstrap),
            Task("rootfs", self.rootfs,
                needs=["bootstrap-target", needs_packages], resource="io"),
            Task("tarball", self.build_tarball, needs=["rootfs"], resource="io"),
            SBOM(distro, self.options).task(self),
        ]

        # 'initrd:' section: just pull the initrd out of the tarball and
        # stop, there's no image to build.
        if "initrd" in self.spec:
            return common + [
                Task("deploy-initrd", self._deploy_initrd, needs=["tarball"]),
            ]

        # No 'image:' section: the tarball is the real output, so move
        # it to its deploy path. Needs 'sbom' too, since it also reads
        # '_tarball' before this renames it away.
        if "image" not in self.spec:
            return common + [
                Task("deploy-rootfs", self._deploy_tarball,
                    needs=["tarball", "sbom"]),
            ]

        # '--rootfs-only' stops here: no disk or appliance needed just
        # to look inside the tarball.
        if self.options.get("rootfs_only"):
            return common

        return common + [
            Task("disk", self._prepare_disk,
                needs=["tarball"] + self._source_task_names(), resource="io"),
        ] + Imager(self).tasks(needs_packages)

    # The rootfs tarball, made permanent -- build_tarball() leaves it a
    # scratch file __del__ would otherwise unlink; renamed away instead,
    # so there is nothing left there for __del__ to find.
    def _deploy_tarball(self):
        os.rename(self._tarball, self._output)
        self._tarball = None

    # Pulled out of the built tarball rather than the tarball itself:
    # more than one match is the same build-time error the imager's own
    # kernel/initrd pairing check makes of it (imager.py's _boot_files()).
    def _deploy_initrd(self):
        with tarfile.open(self._tarball) as tar:
            matches = [m for m in tar.getmembers()
                       if m.name.lstrip("./").startswith("boot/initrd.img-")]
            if len(matches) != 1:
                raise RuntimeError(
                    "expected exactly one 'initrd.img-*' under '/boot' in "
                    "the built root file-system, found %d (%s)"
                    % (len(matches), ", ".join(m.name for m in matches)))
            with tar.extractfile(matches[0]) as src, \
                 open(self._initrd_output, "wb") as dst:
                shutil.copyfileobj(src, dst)
        self._unlink(self._tarball, "root file-system as a tarball")
        self._tarball = None

    # The full task graph: shared_tasks(), plus own_tasks() unless
    # '--packages-only' stops here.
    def tasks(self):
        if self.spec is None:
            raise ValueError(
                "no 'image:' section in this specification -- nothing to build")
        shared = self.shared_tasks()
        all_tasks = shared if self.options.get("packages_only") \
            else shared + self.own_tasks()
        # Merge in each 'multiconfig:' group's tasks, namespaced under
        # its name, with each group's root tasks wired to need every
        # sink task of its predecessor group(s) ('after' set).
        if len(self.subbuilds) > 0:
            from seine import multiconfig
            raw = {name: build.image.tasks()
                   for name, build in self.subbuilds.items()}
            labels = {name: multiconfig._label(build, name=name)
                      for name, build in self.subbuilds.items()}
            sinks = {name: tasks.sinks(group) for name, group in raw.items()}
            for name, deps in self.multiconfig_after.items():
                if len(deps) == 0:
                    continue
                predecessors = ["%s:%s" % (labels[dep], sink)
                                for dep in sorted(deps) for sink in sinks[dep]]
                for task in raw[name]:
                    if len(task.needs) == 0:
                        task.needs = predecessors
            for name, group in raw.items():
                all_tasks += tasks.namespaced(group, labels[name])
        # '--target' narrows to one task and what it needs. Checked here
        # rather than left to ancestors(), which silently drops an
        # unrecognised name instead of catching a command-line typo.
        target = self.options.get("target")
        if target is not None:
            names = {t.name for t in all_tasks}
            if target not in names:
                raise ValueError(
                    "no task '%s' -- available: %s"
                    % (target, ", ".join(sorted(names))))
            all_tasks = tasks.ancestors(all_tasks, [target])
        return all_tasks

    # Every container image a build would use, named without building any
    # of them. Asked of the same classes the build instantiates, rather
    # than reimplementing their naming here.
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

    # Prints the same task graph build() would run, instead of running it.
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
        # Only mention parallelism when it's more than 1: "1 at a time" is
        # the default and says nothing.
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
    # default, or a caller's own (e.g. the TUI's TextualReporter).
    def build(self, reporter=None):
        if self.options.get("dry_run"):
            return self.plan()
        if self.spec is None:
            raise ValueError(
                "no 'image:' section in this specification -- nothing to build")
        try:
            jobs = self.options.get("jobs", 1)
            resources = self.options.get("resources")
            verbose = self.options.get("verbose", False)

            # Tracked per release, not per .deb: apt decides which cached
            # .debs it actually uses, so release is the smallest unit
            # seine can honestly attribute the cache hit to.
            release = self.spec["distribution"]["release"]
            cache_index.Index().hit(cache_index.DOWNLOADS, release)

            # Each step's output goes to its own log file unless verbose
            # and single-job (one terminal, one step at a time). A
            # caller's own reporter always gets log files too.
            self.logs = None
            if verbose == False or jobs > 1 or reporter is not None:
                self.logs = self._logs()
                print("output under %s" % self.logs)

            steps = self.tasks()
            # Digest taken before any task runs: 'disk' mutates the
            # partition/volume dicts in self.spec, so a digest taken
            # after running would never match a fresh, un-run reload.
            digest = analyze.spec_digest(self.spec)
            # Only the internally-built Display is entered as a context
            # manager -- a caller's reporter owns its own lifecycle.
            display = reporter
            ticker = contextlib.nullcontext()
            if display is None and verbose == False:
                display = progress.Display(total=len(steps),
                                           environment=os.environ)
                ticker = display
            ok = False
            # 'sampled' is optional on a Reporter -- Display has none, so
            # the machine is still watched and recorded, just not pushed live.
            machine = analyze.watching(callback=getattr(reporter, "sampled", None))
            try:
                with machine, ticker:
                    tasks.run(steps, jobs=jobs, resources=resources,
                              logs=self.logs, verbose=verbose, display=display)
                ok = True
            finally:
                # Recorded even on failure: which steps ran and how long
                # they took still matters for the build that resumes this one.
                rootfs_size = None
                if self._tarball is not None and os.path.exists(self._tarball):
                    rootfs_size = os.path.getsize(self._tarball)
                analyze.record(steps, digest, jobs=jobs, ok=ok, machine=machine,
                               rootfs_size=rootfs_size)

            # Printed once at the end, as the answer to "is the cache working".
            said = cache_index.summary()
            if said is not None:
                print(said)
        except:
            if self._image is not None:
                os.unlink(self._image)
            raise
