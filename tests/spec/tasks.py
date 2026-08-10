#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import BuildCmd
from seine.tasks import Task, ordered, run

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
        self.assertEqual(names, ["bootstrap-host", "bootstrap-target",
                                 "packages", "rootfs", "appliance",
                                 "tarball", "sbom", "disk", "image"])

        tasks = {t.name: t for t in build.image.tasks()}
        self.assertEqual(tasks["appliance"].needs,
                         ["bootstrap-target", "packages"])
        self.assertEqual(tasks["image"].needs, ["disk", "appliance"])

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
                         ["fetch:seine-test-unrelated"])

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
            # A download waits on a server, so it waits for nothing but
            # the builder image -- not for the package it will be built
            # against.
            self.assertEqual(tasks["fetch:%s" % name].needs,
                             ["packages-prepare"])
            self.assertIn("fetch:%s" % name,
                          tasks["package:%s" % name].needs)

        # Building still waits for what it is built against, which
        # fetching never did.
        self.assertIn("deploy:seine-test-library",
                      tasks["package:seine-test-application"].needs)
        self.assertNotIn("deploy:seine-test-library",
                         tasks["fetch:seine-test-application"].needs)

        # And the barrier still covers everything, fetches included.
        for name in tasks:
            if name.startswith(("fetch:", "package:", "deploy:")):
                self.assertIn(name, tasks["packages"].needs)

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
