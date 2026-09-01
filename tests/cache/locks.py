#!/usr/bin/env python3

import avocado
import os
import sys
import threading
import time

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.utils import locked

class TwoBuildsDoNotWriteOneCacheAtOnce(avocado.Test):
    def test(self):
        import subprocess
        import sys

        shared = os.path.join(self.workdir, "cache")
        # Two seine runs wanting the same chroot want the same bytes;
        # what they must not do is write it at the same time.
        writer = (
            "import sys, time;"
            "sys.path.insert(0, %r);"
            "from seine.utils import locked;"
            "path = %r;"
            "f = None;"
            "exec('with locked(path):\\n"
            "  open(path, \"a\").write(\"in\\\\n\")\\n"
            "  time.sleep(0.5)\\n"
            "  open(path, \"a\").write(\"out\\\\n\")\\n')"
            % (path_to_sources, shared))
        both = [subprocess.Popen([sys.executable, "-c", writer])
                for _ in range(2)]
        for process in both:
            self.assertEqual(process.wait(), 0)

        with open(shared) as f:
            # Interleaved would be in, in, out, out.
            self.assertEqual(f.read().split(), ["in", "out", "in", "out"])

# What many builds may do at once, and what none may do while one of them
# is: several may add images to a storage together, and a prune may not run
# while any of them has intermediates of its own to keep.
#
# Two threads rather than two processes: flock is per open file, so each
# holder opens its own -- which these do, being separate 'locked' calls.
class SharedHoldersDoNotWaitForEachOther(avocado.Test):
    def setUp(self):
        self.path = os.path.join(self.workdir, "storage")

    def test_two_shared_holders_are_both_let_in(self):
        both = threading.Barrier(2, timeout=5)

        def hold():
            with locked(self.path, shared=True):
                both.wait()

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for thread in threads:
            thread.start()
        # The barrier is what fails if they were let in one at a time: the
        # first would sit in it waiting for a second that cannot start.
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "a shared holder waited")

    def test_exclusive_waits_for_a_shared_holder(self):
        with locked(self.path, shared=True):
            with self.assertRaises(BlockingIOError):
                with locked(self.path, blocking=False):
                    self.fail("the prune ran while a build held the storage")

    def test_shared_waits_for_an_exclusive_holder(self):
        with locked(self.path):
            with self.assertRaises(BlockingIOError):
                with locked(self.path, shared=True, blocking=False):
                    self.fail("a build ran while the prune held the storage")

    # Nothing is asked to wait for a lock nobody holds.
    def test_a_free_lock_is_taken_either_way(self):
        with locked(self.path, shared=True, blocking=False):
            pass
        with locked(self.path, blocking=False):
            pass

# The wiring above the primitive: two builds of different images are inside
# podman together, and two of the same one are not.
class DifferentImagesBuildTogether(avocado.Test):
    def setUp(self):
        from seine.bootstrap import Bootstrap
        from seine.utils import ContainerEngine

        self.environment = dict(os.environ)
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "build")
        os.environ["SEINE_CACHE_DIR"] = os.path.join(self.workdir, "cache")

        self.engine = ContainerEngine
        self.counting = threading.Lock()
        self.inside, self.most, self.commands = 0, 0, []
        self.podman = (ContainerEngine.run, ContainerEngine.imageLabel)
        ContainerEngine.imageLabel = staticmethod(lambda name, label: None)
        ContainerEngine.run = staticmethod(self._run)

        class Image(Bootstrap):
            def __init__(self, name, options):
                super().__init__({"release": "trixie"}, options)
                self.named = name
            def create(self):
                pass
            def defaultName(self):
                return self.named
        self.Image = Image

    def tearDown(self):
        self.engine.run, self.engine.imageLabel = self.podman
        os.environ.clear()
        os.environ.update(self.environment)

    # How many builders were inside podman at the same time, which is the
    # whole question. Long enough that a second one has time to arrive.
    def _run(self, *args, **kwargs):
        cmd = args[0] if len(args) > 0 else kwargs.get("cmd")
        with self.counting:
            self.commands.append(cmd)
        if cmd[0] != "build":
            return None
        with self.counting:
            self.inside += 1
            self.most = max(self.most, self.inside)
        time.sleep(0.3)
        with self.counting:
            self.inside -= 1
        return None

    def build(self, name, failures):
        try:
            self.Image(name, {"verbose": False}).build("FROM debian:trixie\n")
        except Exception as e:
            failures.append("%s: %s" % (name, e))

    def run_both(self, first, second):
        failures = []
        threads = [threading.Thread(target=self.build, args=(name, failures))
                   for name in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive(), "a build never finished")
        self.assertEqual(failures, [])
        return self.most

    def test_two_different_images_are_built_at_once(self):
        self.assertEqual(self.run_both("bootstrap/debian/trixie/amd64",
                                       "builder/debian/trixie"), 2,
                         "the builds were serialised")

    # The same image is still one at a time, or the work is done twice.
    def test_one_image_is_built_once_at_a_time(self):
        self.assertEqual(self.run_both("bootstrap/debian/trixie/amd64",
                                       "bootstrap/debian/trixie/amd64"), 1,
                         "two builds of one image ran together")

    # Making an image sweeps nothing: the prune is machine-wide and waits
    # for the end of the build, where it can be the only thing running.
    def test_making_an_image_prunes_nothing(self):
        self.run_both("bootstrap/debian/trixie/amd64", "builder/debian/trixie")
        self.assertEqual([cmd for cmd in self.commands
                          if cmd[:2] == ["image", "prune"]], [])

# The sweep a build ends with, rather than one after every image it made.
class TheStorageIsSweptWhenABuildIsDone(avocado.Test):
    def setUp(self):
        from seine.build import BuildCmd
        from seine.utils import ContainerEngine

        self.environment = dict(os.environ)
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "build")

        self.engine, self.ran = ContainerEngine, []
        self.podman = ContainerEngine.run
        ContainerEngine.run = staticmethod(
            lambda cmd, check=False: self.ran.append(cmd))
        self.build = BuildCmd()

    def tearDown(self):
        self.engine.run = self.podman
        os.environ.clear()
        os.environ.update(self.environment)

    def pruned(self):
        return [cmd for cmd in self.ran if cmd[:2] == ["image", "prune"]]

    def test_a_free_storage_is_pruned(self):
        self.build._prune()
        self.assertEqual(len(self.pruned()), 1)

    # Another build is holding intermediates it is standing on, and prunes
    # for everyone when it finishes.
    def test_a_storage_another_build_holds_is_left_alone(self):
        with locked(self.engine.storage_lock(), shared=True):
            self.build._prune()
        self.assertEqual(self.pruned(), [])
