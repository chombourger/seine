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

class ABuildRunsInTheOrderItAlwaysDid(avocado.Test):
    def test(self):
        build = BuildCmd()
        build.loads(SPEC)
        build.parse()
        names = [t.name for t in ordered(build.image.tasks())]
        # The sequence the steps were written in before they were named,
        # derived here from what each one needs rather than from where it
        # sits in the source.
        self.assertEqual(names, ["bootstrap-host", "bootstrap-target",
                                 "packages", "rootfs", "tarball", "sbom",
                                 "disk", "image"])

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
