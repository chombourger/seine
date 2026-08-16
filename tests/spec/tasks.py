#!/usr/bin/env python3

import atexit
import avocado
import os
import re
import shutil
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.bootstrap import HostBootstrap
from seine.build import BuildCmd
from seine.tasks import Task, namespaced, ordered, run

# Nothing under here may write into the machine's own cache. These build
# Builder objects directly, and asking one for a stamp or an index makes
# the directory it would live in -- so a plain unit test run leaves
# directories in ~/.cache/seine, and a run at an older commit leaves them
# in a layout the current code no longer uses.
#
# One cache per test process, thrown away with it. Set rather than
# defaulted so it holds however the suite was invoked; the tests that
# build images for real pass their own to the seine they run.
os.environ["SEINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seine-tests-")
# And no key either: what a build signs with is read from the environment,
# so a developer who signs their own builds would otherwise run a
# different suite than everyone else.
os.environ.pop("SEINE_SIGN_KEY", None)
atexit.register(shutil.rmtree, os.environ["SEINE_CACHE_DIR"],
                ignore_errors=True)

SPEC = """
distribution:
    release: trixie
    architecture: amd64
image:
    filename: tasks-test.img
    partitions:
        - label: rootfs
          where: /
"""

# What a terminal is left showing: the display takes lines back with
# 'move up N, clear', so what survived is not what was written.
def screen(written):
    lines = []
    for chunk in re.split(r"(\x1b\[\d+A\x1b\[J)", written):
        erased = re.fullmatch(r"\x1b\[(\d+)A\x1b\[J", chunk)
        if erased is None:
            lines.extend(chunk.splitlines())
            continue
        del lines[len(lines) - int(erased.group(1)):]
    return lines

def task(name, needs=None, ran=None):
    ran = [] if ran is None else ran
    return Task(name, lambda: ran.append(name), needs=needs)

class DependenciesDecideTheOrder(avocado.Test):
    def test(self):
        ran = []
        # Declared back to front, so only the dependencies can be what
        # puts them right.
        tasks = [task("image", ["disk"], ran),
                 task("disk", ["rootfs"], ran),
                 task("rootfs", None, ran)]
        run(tasks)
        self.assertEqual(ran, ["rootfs", "disk", "image"])

class WhatIsDeclaredFirstRunsFirst(avocado.Test):
    def test(self):
        # Two tasks that could run in either order run in the order they
        # were written: a build that shuffles itself between runs is a
        # build whose logs cannot be compared.
        ran = []
        tasks = [task("root", None, ran),
                 task("one", ["root"], ran),
                 task("two", ["root"], ran)]
        run(tasks)
        self.assertEqual(ran, ["root", "one", "two"])

class TasksWaitingOnEachOtherAreRejected(avocado.Test):
    def test(self):
        try:
            ordered([task("one", ["two"]), task("two", ["one"])])
            self.fail("a cycle was accepted!")
        except ValueError as e:
            self.assertIn("wait on each other", str(e))

class ADependencyNothingProvidesIsRejected(avocado.Test):
    def test(self):
        try:
            ordered([task("image", ["nosuchtask"])])
            self.fail("a dependency on nothing was accepted!")
        except ValueError as e:
            self.assertIn("nosuchtask", str(e))

class TasksAreNamedOnlyOnce(avocado.Test):
    def test(self):
        try:
            ordered([task("image"), task("image")])
            self.fail("two tasks of the same name were accepted!")
        except ValueError:
            pass

class NamespacedTasksAreRenamedAndReordered(avocado.Test):
    def test(self):
        ran = []
        tasks = namespaced(
            [task("rootfs", ["bootstrap-target"], ran),
             task("bootstrap-target", None, ran)], "rpi4")
        self.assertEqual(sorted(t.name for t in tasks),
                         ["rpi4:bootstrap-target", "rpi4:rootfs"])
        run(tasks)
        self.assertEqual(ran, ["bootstrap-target", "rootfs"])

    def test_a_need_outside_the_list_is_left_alone(self):
        # A barrier several namespaced lists share -- 'packages', say --
        # is not itself in 'tasks', so prefixing it again would point it
        # at a task that does not exist.
        tasks = namespaced([task("rootfs", ["packages"])], "rpi4")
        self.assertEqual(tasks[0].needs, ["packages"])

    def test_two_specifications_do_not_collide(self):
        # The whole point: 'rootfs' from two boards, run together.
        one = namespaced([task("rootfs")], "rpi4")
        two = namespaced([task("rootfs")], "pc")
        ordered(one + two)

    def test_a_duplicate_name_is_rejected_before_running_anything(self):
        try:
            namespaced([task("rootfs"), task("rootfs")], "rpi4")
            self.fail("two tasks of the same name were accepted!")
        except ValueError:
            pass

class AncestorsWalksNeedsBackwards(avocado.Test):
    def test(self):
        from seine.tasks import ancestors
        tasks = [task("root"), task("middle", ["root"]),
                 task("leaf", ["middle"]), task("unrelated")]
        got = ancestors(tasks, ["leaf"])
        self.assertEqual(sorted(t.name for t in got), ["leaf", "middle", "root"])

    def test_the_names_asked_for_are_included(self):
        from seine.tasks import ancestors
        tasks = [task("root"), task("alone")]
        got = ancestors(tasks, ["root", "alone"])
        self.assertEqual(sorted(t.name for t in got), ["alone", "root"])

    def test_a_name_naming_nothing_is_skipped(self):
        from seine.tasks import ancestors
        tasks = [task("root")]
        got = ancestors(tasks, ["root", "does-not-exist"])
        self.assertEqual([t.name for t in got], ["root"])

class SucceededIsFalseForAnythingThatDidNotRunOrDid(avocado.Test):
    def test_true_when_everything_ran(self):
        from seine.tasks import run, succeeded
        tasks = [task("root"), task("leaf", ["root"])]
        run(tasks)
        self.assertTrue(succeeded(tasks))

    def test_false_for_a_task_that_failed(self):
        # jobs=2: a single sequential task's own exception propagates
        # unwrapped, wrapping in Failed is what jobs>1 does.
        from seine.tasks import Failed, run, succeeded

        def fails():
            raise ValueError("no")

        tasks = [Task("fails", fails)]
        try:
            run(tasks, jobs=2)
        except Failed:
            pass
        self.assertFalse(succeeded(tasks))

    def test_false_for_a_task_that_never_ran(self):
        # A task cancelled by a failure elsewhere: never started, never
        # marked failed either -- succeeded() has to ask both.
        from seine.tasks import Failed, run, succeeded

        def fails():
            raise ValueError("no")

        tasks = [Task("fails", fails), task("unrelated", ["fails"])]
        try:
            run(tasks, jobs=2)
        except Failed:
            pass
        cancelled = [t for t in tasks if t.name == "unrelated"][0]
        self.assertIsNone(cancelled.started)
        self.assertFalse(succeeded([cancelled]))

class ABuildRunsInTheOrderItsStepsRequire(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(SPEC)
        build.parse()
        names = [t.name for t in ordered(build.image.tasks())]
        # Derived from what each step needs rather than from where it
        # sits in the source, which is why the appliance now comes before
        # the root file-system it has nothing to do with: it is ready to
        # start as soon as the packages are.
        #
        # 'packages' before 'bootstrap-target': shared_tasks() is declared
        # ahead of own_tasks() -- neither needs the other, so this is only
        # tie-breaking order, not a dependency.
        self.assertEqual(names, ["bootstrap-host", "packages",
                                 "bootstrap-target", "rootfs", "appliance",
                                 "tarball", "sbom", "disk", "image"])

        tasks = {t.name: t for t in build.image.tasks()}
        self.assertEqual(tasks["appliance"].needs,
                         ["bootstrap-target", "packages"])
        self.assertEqual(tasks["image"].needs, ["disk", "appliance"])

class SharedAndOwnTasksAreTheWholeBuild(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(SPEC)
        build.parse()
        image = build.image
        self.assertEqual([t.name for t in image.shared_tasks()]
                         + [t.name for t in image.own_tasks()],
                         [t.name for t in image.tasks()])

    def test_packages_only_is_shared_tasks_alone(self):
        build = BuildCmd()
        build.options["packages_only"] = True
        build.loads(SPEC)
        build.parse()
        image = build.image
        self.assertEqual([t.name for t in image.shared_tasks()],
                         [t.name for t in image.tasks()])

class OwnTasksTakeTheSharedBarrierByName(avocado.Test):
    def test(self):
        # What a caller running several images together points 'rootfs'
        # at instead of a bare 'packages' -- a barrier several of them
        # share, named for the release rather than for one image.
        build = BuildCmd()
        build.loads(SPEC)
        build.parse()
        image = build.image
        image.hostBootstrap = HostBootstrap(
            image.spec["distribution"], image.options)
        own = {t.name: t for t in image.own_tasks(needs_packages="trixie:packages")}
        self.assertEqual(own["rootfs"].needs,
                         ["bootstrap-target", "trixie:packages"])
        # imager.py's own 'appliance' task needs the packages too, for its
        # kernel -- threaded the same way, not left pointing at a bare
        # 'packages' nothing here builds.
        self.assertEqual(own["appliance"].needs,
                         ["bootstrap-target", "trixie:packages"])

class OwnTasksTakeAGivenHostBootstrap(avocado.Test):
    def test(self):
        # A caller merging several images shares one HostBootstrap object
        # rather than letting each image build its own -- own_tasks()
        # stands its target bootstrap on whichever it is handed.
        build = BuildCmd()
        build.loads(SPEC)
        build.parse()
        image = build.image
        given = HostBootstrap(image.spec["distribution"], image.options)
        image.own_tasks(hostBootstrap=given)
        self.assertIs(image.hostBootstrap, given)

class OutputGoesToTheTerminalByDefault(avocado.Test):
    def test(self):
        from seine import tasks
        # Nothing capturing: a build running one step at a time writes
        # where it always did, in the order it produced it.
        self.assertEqual(tasks.output(), None)

class ATaskCanBeGivenAFileOfItsOwn(avocado.Test):
    def test(self):
        from seine import tasks
        tasks.install()
        path = os.path.join(self.workdir, "task.log")
        with open(path, "w") as f:
            with tasks.capture(f):
                self.assertEqual(tasks.output(), f)
                print("inside")
            print("outside")
        with open(path) as f:
            self.assertEqual(f.read(), "inside\n")
        # And the sink is put back, whatever was there before.
        self.assertEqual(tasks.output(), None)

class ATaskFileIsReadableWhileItIsWritten(avocado.Test):
    def test(self):
        from seine import tasks
        tasks.install()
        path = os.path.join(self.workdir, "task.log")
        with open(path, "w") as f:
            with tasks.capture(f):
                print("first")
                # A long build is watched with tail while it runs, which
                # a buffer holding the line would defeat.
                with open(path) as reader:
                    self.assertEqual(reader.read(), "first\n")

class IndependentTasksRunAtOnce(avocado.Test):
    def test(self):
        import threading
        from seine.tasks import run

        # Each waits for the other to have started. With one at a time
        # this cannot finish, which is the point: it is what proves they
        # really are running beside each other rather than in a row.
        started = threading.Barrier(2, timeout=30)
        tasks = [Task("one", started.wait), Task("two", started.wait)]
        run(tasks, jobs=2)

class DependenciesStillWaitWhenRunningInParallel(avocado.Test):
    def test(self):
        from seine.tasks import run
        ran = []
        tasks = [task("first", None, ran),
                 task("second", ["first"], ran),
                 task("third", ["second"], ran)]
        run(tasks, jobs=4)
        self.assertEqual(ran, ["first", "second", "third"])

class AFailureStartsNothingNew(avocado.Test):
    def test(self):
        import time
        from seine.tasks import Failed, run

        ran = []
        def fails():
            ran.append("fails")
            raise ValueError("no")

        def slow():
            time.sleep(0.2)
            ran.append("slow")

        tasks = [Task("fails", fails),
                 Task("slow", slow),
                 task("after", ["fails"], ran),
                 task("unrelated", ["slow"], ran)]
        try:
            run(tasks, jobs=2)
            self.fail("a failing task was not reported!")
        except Failed as e:
            # What needed the failure never runs, and neither does
            # anything still waiting: the build is over, it is only
            # finishing what it had already started.
            self.assertNotIn("after", ran)
            self.assertNotIn("unrelated", ran)
            self.assertEqual([name for name, _ in e.failures], ["fails"])
            self.assertEqual(sorted(e.cancelled), ["after", "unrelated"])
            # What was already running was left alone rather than killed:
            # a step interrupted halfway leaves half-written caches.
            self.assertIn("slow", ran)

class AnInterruptStartsNothingNew(avocado.Test):
    def test(self):
        import time
        from seine.tasks import Interrupted, interrupt, run

        ran = []
        def asks():
            ran.append("asks")
            interrupt()

        def slow():
            time.sleep(0.3)
            ran.append("slow")

        tasks = [Task("asks", asks),
                 Task("slow", slow),
                 task("after", ["asks"], ran),
                 task("unrelated", ["slow"], ran)]
        try:
            run(tasks, jobs=2)
            self.fail("an interrupted build said it had finished!")
        except Interrupted as e:
            self.assertNotIn("after", ran)
            self.assertNotIn("unrelated", ran)
            self.assertEqual(sorted(e.cancelled), ["after", "unrelated"])
            # As a failure does: what was running finishes, so what it was
            # writing is not left half-done.
            self.assertIn("slow", ran)

# The display erases the lines it wrote every tenth of a second, so a
# message written past it does not stay said.
class AnInterruptSaysSoWhereItCanBeRead(avocado.Test):
    def test(self):
        import io
        from seine.progress import Display
        from seine.tasks import Interrupted, interrupt, run

        class Terminal(io.StringIO):
            encoding = "utf-8"
            def isatty(self):
                return True

        stream = Terminal()
        shown = Display(stream=stream, total=2, environment={"TERM": "xterm"})

        def asks():
            interrupt()

        tasks = [Task("asks", asks), task("after", ["asks"], [])]
        try:
            with shown:
                run(tasks, display=shown)
            self.fail("an interrupted build said it had finished!")
        except Interrupted:
            pass
        # On the screen at the end, not merely written to it once.
        self.assertIn("interrupted: waiting for",
                      "\n".join(screen(stream.getvalue())))

    # Without a display -- a verbose build, or a caller that has none --
    # nothing redraws the terminal, and stderr is where the key was pressed.
    def test_with_no_display(self):
        import io
        from seine.tasks import Interrupted, interrupt, run

        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            tasks = [Task("asks", interrupt), task("after", ["asks"], [])]
            try:
                run(tasks)
                self.fail("an interrupted build said it had finished!")
            except Interrupted:
                pass
            self.assertIn("interrupted: waiting for", sys.stderr.getvalue())
        finally:
            sys.stderr = stderr

class AnInterruptStopsABuildRunningOneStepAtATime(avocado.Test):
    def test(self):
        from seine.tasks import Interrupted, interrupt, run

        ran = []
        def asks():
            ran.append("asks")
            interrupt()

        tasks = [Task("asks", asks), task("after", ["asks"], ran)]
        try:
            run(tasks)
            self.fail("an interrupted build said it had finished!")
        except Interrupted as e:
            self.assertEqual(ran, ["asks"])
            self.assertEqual(e.cancelled, ["after"])

class AFailingTaskSaysWhatItWrote(avocado.Test):
    def test(self):
        from seine.tasks import Failed, run

        def fails():
            print("the interesting line")
            raise ValueError("no")

        try:
            run([Task("fails", fails)], jobs=2, logs=self.workdir)
            self.fail("a failing task was not reported!")
        except Failed:
            pass
        # Its output went to a file so it would not interleave with the
        # tasks beside it, so the build has to hand it back.
        with open(os.path.join(self.workdir, "fails.log")) as f:
            self.assertIn("the interesting line", f.read())

PACKAGES = """
distribution:
    release: trixie
    architecture: amd64
packages:
    - source: apt://seine-test-application
      after:
          - seine-test-library
    - source: apt://seine-test-library
    - source: apt://seine-test-unrelated
image:
    filename: tasks-test.img
    partitions:
        - label: rootfs
          where: /
"""

class EachPackageIsATaskOfItsOwn(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(PACKAGES)
        build.parse()
        tasks = {t.name: t for t in build.image.tasks()}

        for name in ["seine-test-application", "seine-test-library",
                     "seine-test-unrelated"]:
            self.assertIn("package:%s" % name, tasks)

        # A package built against another waits for that one to be
        # published rather than merely built: what sbuild installs comes
        # out of the repository, which is a later moment than the build.
        self.assertIn("deploy:seine-test-library",
                      tasks["package:seine-test-application"].needs)
        self.assertEqual(tasks["deploy:seine-test-library"].needs,
                         ["package:seine-test-library"])
        self.assertEqual(tasks["package:seine-test-unrelated"].needs,
                         ["prepare:seine-test-unrelated"])

        # And the rest of the build waits on the barrier rather than on
        # whichever packages a specification happens to have.
        self.assertEqual(tasks["rootfs"].needs, ["bootstrap-target", "packages"])
        for name in tasks:
            if name.startswith(("package:", "deploy:")):
                self.assertIn(name, tasks["packages"].needs)

class ABuildWithoutPackagesStillHasTheBarrier(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(SPEC)
        build.parse()
        tasks = {t.name: t for t in build.image.tasks()}
        # Nothing to build, and the steps that wait for packages still
        # have something to wait for.
        self.assertEqual(tasks["packages"].needs, ["bootstrap-host"])
        self.assertNotIn("packages-prepare", tasks)

class FetchingIsItsOwnStep(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(PACKAGES)
        build.parse()
        tasks = {t.name: t for t in build.image.tasks()}

        for name in ["seine-test-application", "seine-test-library",
                     "seine-test-unrelated"]:
            self.assertIn("fetch:%s" % name, tasks)
            self.assertIn("prepare:%s" % name, tasks)
            # A download waits on a server, so it waits for nothing but
            # the builder image -- not for the package it will be built
            # against, nor for the preparing that follows it.
            self.assertEqual(tasks["fetch:%s" % name].needs,
                             ["packages-prepare"])
            self.assertEqual(tasks["prepare:%s" % name].needs,
                             ["fetch:%s" % name])
            self.assertIn("prepare:%s" % name,
                          tasks["package:%s" % name].needs)

        # Building still waits for what it is built against, which
        # neither fetching nor preparing did.
        self.assertIn("deploy:seine-test-library",
                      tasks["package:seine-test-application"].needs)
        self.assertNotIn("deploy:seine-test-library",
                         tasks["fetch:seine-test-application"].needs)
        self.assertNotIn("deploy:seine-test-library",
                         tasks["prepare:seine-test-application"].needs)

        # And the barrier still covers everything, fetches and prepares
        # included.
        for name in tasks:
            if name.startswith(("fetch:", "prepare:", "package:", "deploy:")):
                self.assertIn(name, tasks["packages"].needs)

# A module's packaging is written as its source is prepared, decided by
# the ABI of the kernel it names -- which this build's own kernel has
# none of until built. Only preparing waits for that; the fetch (a plain
# git clone here) needs none of it and starts as early as any other
# package's.
class AModulesFetchDoesNotWaitOnItsKernelButPreparingDoes(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads("""
distribution:
    release: trixie
    architecture: amd64
packages:
    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=deadbeef
      name: nvidia-open
      version: "580.95.05"
      extends:
          module:
              build: kernel-open
              modules:
                  - nvidia
              amd64-kernels:
                  - linux
    - source: apt://linux
      extends:
          kernel:
              flavour: amd64
image:
    filename: tasks-test.img
    partitions:
        - label: rootfs
          where: /
        """)
        build.parse()
        tasks = {t.name: t for t in build.image.tasks()}

        self.assertEqual(tasks["fetch:open-gpu-kernel-modules"].needs,
                         ["packages-prepare"])
        self.assertIn("deploy:linux", tasks["prepare:nvidia-open"].needs)

class PublishingIsItsOwnStep(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(PACKAGES)
        build.parse()
        tasks = {t.name: t for t in build.image.tasks()}

        for name in ["seine-test-application", "seine-test-library",
                     "seine-test-unrelated"]:
            # Built, then put where the rest of the build installs from.
            self.assertIn("deploy:%s" % name, tasks)
            self.assertEqual(tasks["deploy:%s" % name].needs,
                             ["package:%s" % name])

# A machine filling a cache for others to import builds the packages and
# has no use for the image at the end of it.
class PackagesOnlyStopsAtThePackages(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.options["packages_only"] = True
        build.loads(PACKAGES)
        build.parse()
        names = [t.name for t in build.image.tasks()]

        self.assertIn("bootstrap-host", names)
        self.assertIn("package:seine-test-library", names)
        self.assertIn("packages", names)
        # Nothing that belongs to the image, the target bootstrap included:
        # packages are built in a chroot of the build architecture and
        # never touch it.
        for step in ["bootstrap-target", "rootfs", "tarball", "disk",
                     "appliance", "image"]:
            self.assertNotIn(step, names)

    # Every step still waits for something that is in the graph.
    def test_the_graph_is_whole(self):
        build = BuildCmd()
        build.options["packages_only"] = True
        build.loads(PACKAGES)
        build.parse()
        steps = build.image.tasks()
        names = set(t.name for t in steps)
        for step in steps:
            for needed in step.needs:
                self.assertIn(needed, names,
                              "%s waits for %s, which is not in the graph"
                              % (step.name, needed))

# Somebody who wants to look inside a build has no use for the disk it
# would be written to, or the appliance that writes it.
class RootfsOnlyStopsAtTheTarball(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.options["rootfs_only"] = True
        build.loads(PACKAGES)
        build.parse()
        names = [t.name for t in build.image.tasks()]

        self.assertIn("bootstrap-host", names)
        self.assertIn("bootstrap-target", names)
        self.assertIn("package:seine-test-library", names)
        self.assertIn("rootfs", names)
        self.assertIn("tarball", names)
        for step in ["disk", "appliance", "image"]:
            self.assertNotIn(step, names)

    # Every step still waits for something that is in the graph.
    def test_the_graph_is_whole(self):
        build = BuildCmd()
        build.options["rootfs_only"] = True
        build.loads(PACKAGES)
        build.parse()
        steps = build.image.tasks()
        names = set(t.name for t in steps)
        for step in steps:
            for needed in step.needs:
                self.assertIn(needed, names,
                              "%s waits for %s, which is not in the graph"
                              % (step.name, needed))

class ADryRunSaysWhatItWouldDo(avocado.Test):
    def spec(self):
        return """
                distribution:
                    release: trixie
                    architecture: amd64
                packages:
                    - source: apt://seine-test-dry
                image:
                    filename: dry.img
                    partitions:
                        - label: rootfs
                          where: /
        """

    def build(self, **options):
        build = BuildCmd()
        build.options.update(options)
        build.loads(self.spec())
        build.parse()
        return build

    def test(self):
        import contextlib
        import io

        build = self.build(dry_run=True, jobs=4)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.assertEqual(build.build(), 0)
        said = said.getvalue()

        # Every step it would run, and what each waits for.
        for step in ["bootstrap-host", "packages-prepare",
                     "fetch:seine-test-dry", "package:seine-test-dry",
                     "deploy:seine-test-dry", "rootfs", "appliance", "image"]:
            self.assertIn(step, said)
        self.assertIn("after bootstrap-host", said)
        self.assertIn("4 steps at a time", said)

    def test_rootfs_only_says_so(self):
        import contextlib
        import io

        build = self.build(dry_run=True, rootfs_only=True)
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            self.assertEqual(build.build(), 0)
        said = said.getvalue()

        self.assertIn("would build the root file-system for", said)
        for step in ["rootfs", "tarball"]:
            self.assertIn(step, said)
        for step in ["disk", "appliance", "image"]:
            self.assertNotIn(step, said)

class ADryRunSaysWhatItWouldNotRedo(avocado.Test):
    def test(self):
        import contextlib
        import io

        from seine.packages import Builder
        from seine.sbuild import BuilderImage

        build = BuildCmd()
        build.options["dry_run"] = True
        build.loads("""
                distribution:
                    release: trixie
                    architecture: amd64
                packages:
                    - source: apt://seine-test-done
                image:
                    filename: dry.img
                    partitions:
                        - label: rootfs
                          where: /
        """)
        build.parse()

        # A stamp is what says a package was built from exactly these
        # inputs, so a dry run reads them rather than guessing.
        distro = build.spec["distribution"]
        builder = Builder(distro, {}, BuilderImage(distro, {}))
        stamp = builder.stamp(build.image.packages[0])
        with open(stamp, "w") as f:
            f.write("")
        try:
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                build.build()
            said = said.getvalue()

            self.assertIn("already built", said)
            self.assertIn("seine-test-done", said)
            self.assertIn(os.path.basename(stamp), said)
            # And it says so instead of listing steps that will not run.
            self.assertNotIn("fetch:seine-test-done", said)
        finally:
            os.unlink(stamp)

# A machine that was handed a repository has the .debs and nothing to read
# them with: the index is made from what the directory holds, so a build
# with nothing to rebuild still has that to do.
class ARepositoryWithNoIndexIsIndexed(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def builder(self):
        from seine.packages import Builder
        return Builder({"source": "debian", "release": "trixie",
                        "architecture": "amd64",
                        "uri": "http://example.com/debian",
                        "feeds": [{"suite": "trixie"}]}, {}, None)

    def deb(self, builder, name="linux-image-amd64_6.1_amd64.deb"):
        path = os.path.join(builder.repository(), name)
        with open(path, "wb") as f:
            f.write(b"deb")
        return path

    def steps(self, builder):
        return [t.name for t in builder.tasks([], None)]

    def test_nothing_cached_is_nothing_to_index(self):
        self.assertNotIn("packages-prepare", self.steps(self.builder()))

    def test_debs_without_an_index(self):
        builder = self.builder()
        self.deb(builder)
        steps = self.steps(builder)
        self.assertIn("packages-prepare", steps)
        # And the barrier still waits for it.
        self.assertIn("packages-prepare",
                      [t.needs for t in builder.tasks([], None)
                       if t.name == "packages"][0])

    def test_an_index_that_describes_them_is_left_alone(self):
        builder = self.builder()
        self.deb(builder)
        index = os.path.join(builder.repository(), "Packages")
        with open(index, "w") as f:
            f.write("Package: linux-image-amd64\n")
        self.assertNotIn("packages-prepare", self.steps(builder))

    def test_an_index_older_than_the_debs_is_written_again(self):
        builder = self.builder()
        index = os.path.join(builder.repository(), "Packages")
        with open(index, "w") as f:
            f.write("Package: something-else\n")
        os.utime(index, (1000, 1000))
        self.deb(builder)
        self.assertIn("packages-prepare", self.steps(builder))

# A step that raised rather than exited has to say what it raised: the
# imager talks to libguestfs through python, so what goes wrong there is an
# exception rather than a command's exit status, and the name of the step is
# not an explanation.
class AFailingStepSaysWhatWentWrong(avocado.Test):
    def test(self):
        from seine.tasks import Failed, run

        def fails():
            raise RuntimeError("guestfs: appliance closed the connection")

        try:
            # Two at a time: with one, a step's exception is what a build
            # sees, and it is the parallel path that has to hand it back.
            run([Task("image", fails)], jobs=2, logs=self.workdir)
            self.fail("a failing task was not reported!")
        except Failed as e:
            self.assertIn("image failed", str(e))
            self.assertIn("appliance closed the connection", str(e))

    # One with nothing to say is named by its type rather than by an empty
    # line: 'image failed\n  image: ' explains less than 'KeyError'.
    def test_an_exception_with_no_message(self):
        from seine.tasks import Failed, run

        def fails():
            raise KeyError()

        try:
            run([Task("image", fails)], jobs=2, logs=self.workdir)
            self.fail("a failing task was not reported!")
        except Failed as e:
            self.assertIn("KeyError", str(e))

# 'seine plan' is 'seine build --dry-run' with the option decided for it: the
# same graph, since a plan is only worth what a build would really do.
class ThePlanIsTheBuildUnwalked(avocado.Test):
    def plan(self, command):
        import contextlib, io
        said = io.StringIO()
        command.loads(SPEC)
        command.parse()
        with contextlib.redirect_stdout(said):
            self.assertEqual(command.build(), 0)
        return said.getvalue()

    def test(self):
        from seine.build import PlanCmd
        said = self.plan(PlanCmd())
        self.assertIn("would build", said)
        self.assertIn("bootstrap-host", said)
        # Nothing said about how many at a time: it takes no such option.
        self.assertNotIn("at a time", said)

    def test_the_same_as_a_dry_run(self):
        from seine.build import BuildCmd, PlanCmd
        dry = BuildCmd()
        dry.options["dry_run"] = True
        self.assertEqual(self.plan(PlanCmd()), self.plan(dry))

    # A plan asks for nothing to be built, whatever else it is given.
    def test_it_does_not_build(self):
        from seine.build import PlanCmd
        command = PlanCmd()
        self.assertEqual(command.options["dry_run"], True)
        self.assertEqual(command.options["build"], True)

# What two 'scope' roles do to the graph: an architecture per build, one
# fetch between them, and one step publishing what they made.
SCOPED = """
distribution:
    release: trixie
    architecture: %s
packages:
    - source: apt://seine-test-application
      scope: [host, target]
      after:
          - seine-test-library
    - source: apt://seine-test-library
      scope: [host, target]
image:
    filename: tasks-test.img
    partitions:
        - label: rootfs
          where: /
"""

class TwoRolesAreTwoBuildsOfOneSource(avocado.Test):
    def graph(self, architecture):
        build = BuildCmd()
        build.loads(SCOPED % architecture)
        build.parse()
        return {t.name: t for t in build.image.tasks()}

    def test(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        tasks = self.graph(other)

        # One build per architecture, named for the one it is for, and one
        # fetch feeding both: the same source package is what each is
        # handed.
        for architecture in sorted([HOST_ARCH, other]):
            self.assertIn("package:seine-test-library:%s" % architecture, tasks)
        self.assertEqual(
            len([n for n in tasks if n.startswith("fetch:seine-test-library")]), 1)

    # One step publishes every architecture, since an arch-all binary
    # belongs in all their repositories and is built by one of them.
    def test_publishing_is_one_step_for_every_architecture(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        tasks = self.graph(other)

        deploys = [n for n in tasks if n.startswith("deploy:seine-test-library")]
        self.assertEqual(deploys, ["deploy:seine-test-library"])
        for architecture in sorted([HOST_ARCH, other]):
            self.assertIn("package:seine-test-library:%s" % architecture,
                          tasks["deploy:seine-test-library"].needs)

    # A package built against another waits for it to be published, which
    # is one step however many architectures it was built for.
    def test_dependents_wait_for_what_they_are_built_against(self):
        from seine.utils import HOST_ARCH
        other = "arm64" if HOST_ARCH != "arm64" else "amd64"
        tasks = self.graph(other)

        for architecture in sorted([HOST_ARCH, other]):
            needs = tasks["package:seine-test-application:%s" % architecture].needs
            self.assertIn("deploy:seine-test-library", needs)

    # Same architecture on both sides is one build, not the same work done
    # twice into the same repository.
    def test_a_native_build_collapses_to_one(self):
        from seine.utils import HOST_ARCH
        tasks = self.graph(HOST_ARCH)
        self.assertIn("package:seine-test-library", tasks)
        for name in tasks:
            self.assertNotIn(":%s" % HOST_ARCH, name)

# A specification that says nothing about 'scope' has the graph it always
# had: no architecture in a step's name.
class OneScopeKeepsThePlainNames(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(PACKAGES)
        build.parse()
        names = [t.name for t in build.image.tasks()]

        self.assertIn("package:seine-test-library", names)
        self.assertIn("deploy:seine-test-library", names)
        self.assertNotIn("package:seine-test-library:amd64", names)
