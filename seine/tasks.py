# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import os
import sys
import threading
import time

# Where a task's output goes. Unset while a build runs one step at a time,
# so output reaches the terminal in the order it was produced.
#
# Tasks running beside each other cannot share a terminal that way: two
# kernels and a root file-system writing to one stream interleave into
# something no one can follow. So each task may be given a file of its
# own, and everything it writes goes there instead. Per thread rather than
# per call, since the alternative is passing a file handle through every
# function a task reaches.
_local = threading.local()

def output():
    return getattr(_local, "output", None)

# Sends a task's output to 'stream' for as long as this is held. The
# stream has to be a real file: a container's output is redirected by
# handing its file descriptor to podman, which cannot be done with an
# object that only pretends to be one.
class capture:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        self.previous = output()
        _local.output = self.stream
        return self.stream

    def __exit__(self, *args):
        _local.output = self.previous
        return False

# What print() writes to while a build runs. Installed once, and a no-op
# until a task asks for a stream of its own -- so a build that runs its
# steps one at a time writes to the terminal exactly as before.
class _Stdout:
    def __init__(self, terminal):
        self.terminal = terminal

    def __getattr__(self, name):
        return getattr(output() or self.terminal, name)

    def write(self, text):
        stream = output() or self.terminal
        written = stream.write(text)
        # A task's file is read while the build is still running -- by a
        # user tailing it, and by seine itself when the task fails -- so
        # it cannot sit in a buffer until the task ends.
        stream.flush()
        return written

def install():
    if not isinstance(sys.stdout, _Stdout):
        sys.stdout = _Stdout(sys.stdout)

# The steps a build is made of, and what each one needs before it can run.
#
# They were a straight line of statements, which said that every step waits
# for the one written above it -- true of a kernel and the root file-system
# it is installed into, not true of the imager appliance, which waits
# today for a root file-system it has nothing to do with.
#
# How many of them run at once is 'jobs' below; with one, this is the
# same sequence as before, derived rather than assumed. What it buys is
# that the order can be checked, and that each step is timed and named
# when it fails.
class Task:
    def __init__(self, name, run, needs=None):
        self.name = name
        self.run = run
        self.needs = list(needs or [])
        # What running it cost, filled in as it runs. Left on the task
        # rather than handed back, so that whoever built the graph can
        # read it afterwards -- including after a failure, when what
        # comes back is the exception instead.
        self.started = None
        self.ended = None
        self.failed = False

    def __repr__(self):
        return "Task(%r)" % self.name

# The tasks in an order that satisfies every dependency, and among those,
# the order they were declared in. Stable on purpose: a build that
# reorders itself from one run to the next is a build whose logs cannot be
# compared.
def ordered(tasks):
    by_name = {}
    for task in tasks:
        if task.name in by_name:
            raise ValueError("duplicate task '%s'" % task.name)
        by_name[task.name] = task
    for task in tasks:
        for need in task.needs:
            if need not in by_name:
                raise ValueError(
                    "task '%s' needs '%s', which no task provides"
                    % (task.name, need))

    done = []
    seen = set()
    while len(done) < len(tasks):
        ready = [t for t in tasks
                 if t.name not in seen
                 and all(need in seen for need in t.needs)]
        if len(ready) == 0:
            waiting = [t.name for t in tasks if t.name not in seen]
            raise ValueError(
                "tasks wait on each other and none can run: %s"
                % ", ".join(sorted(waiting)))
        for task in ready:
            done.append(task)
            seen.add(task.name)
    return done

# What a build does when a step fails, which parallel builds have to
# answer and a sequential one never had to.
#
# Nothing new is started. What is already running is left to finish
# rather than killed: seine's house-keeping runs at the end of each step
# -- a stamp is written only once a package built, a half-made chroot
# deletes itself -- and killing a task skips all of that for a build
# that has already failed.
#
# So the build waits, then reports the failure that came first, says what
# never ran, and raises it. Later failures are reported too, since the
# first one is the one that has any chance of being the cause.
class Failed(Exception):
    def __init__(self, failures, cancelled):
        self.failures = failures
        self.cancelled = cancelled
        names = ", ".join(name for name, _ in failures)
        message = "%s failed" % names
        if len(cancelled) > 0:
            message += " (%s did not run)" % ", ".join(sorted(cancelled))
        # And what each of them said. Without this a step that raised
        # rather than exited -- the imager talking to libguestfs is the one
        # that does -- reported only its own name, and what actually went
        # wrong was nowhere: not in the step's output, which had ended, and
        # not in the traceback, which starts here.
        for name, error in failures:
            said = str(error) or error.__class__.__name__
            message += "\n  %s: %s" % (name, said)
            # And the last of what the command itself wrote, where it was
            # kept. 'returned non-zero exit status 255' is a number: what
            # says whether podman failed or what it ran did is the line
            # above it, which was in the step's log and nowhere a person
            # reading this would look.
            for line in Failed._spoke(error):
                message += "\n    %s" % line
        super().__init__(message)

    @staticmethod
    def _spoke(error):
        said = getattr(error, "output", None) or getattr(error, "stderr", None)
        if isinstance(said, bytes):
            said = said.decode("utf-8", "replace")
        return str(said).splitlines() if said else []

# What a build would do, without doing any of it: every step in the order
# they would run, and what each waits for. The order is the same one run()
# would take, so a plan is not a description of the build but the build
# itself, unexecuted.
def describe(tasks):
    for task in ordered(tasks):
        if len(task.needs) == 0:
            print("  %s" % task.name)
        else:
            print("  %-24s after %s" % (task.name, ", ".join(task.needs)))

# Runs them. 'jobs' is how many may run at once; one, by default, which
# is the order and the output a build has always had. 'verbose' prints
# what each step cost, which is what tells a user where the time goes --
# and so what is worth running beside what.
def run(tasks, jobs=1, verbose=False, logs=None, display=None):
    tasks = ordered(tasks)
    if logs is not None:
        install()
    if jobs <= 1:
        _sequential(tasks, verbose, logs, display)
        return

    install()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        _parallel(tasks, pool, jobs, verbose, logs, display)

def _sequential(tasks, verbose, logs, display):
    for task in tasks:
        _run_one(task, verbose, logs, display)

def _parallel(tasks, pool, jobs, verbose, logs, display):
    done = set()
    failures = []
    running = {}
    waiting = list(tasks)

    while len(waiting) > 0 or len(running) > 0:
        # Nothing new once something has failed. What is running stays
        # running: see above.
        if len(failures) == 0:
            ready = [t for t in waiting if all(n in done for n in t.needs)]
            for task in ready[:jobs - len(running)]:
                waiting.remove(task)
                running[pool.submit(_run_one, task, verbose, logs, display)] = task

        if len(running) == 0:
            break
        finished, _ = concurrent.futures.wait(
            running, return_when=concurrent.futures.FIRST_COMPLETED)
        for future in finished:
            task = running.pop(future)
            error = future.exception()
            if error is None:
                done.add(task.name)
            else:
                failures.append((task.name, error))
                _report(task, logs)

    if len(failures) > 0:
        raise Failed(failures, [t.name for t in waiting])

def _run_one(task, verbose, logs, display=None):
    started = time.time()
    task.started = started
    if display is not None:
        display.started(task.name)
    failed = True
    try:
        if logs is None:
            task.run()
        else:
            path = os.path.join(logs, "%s.log" % task.name)
            with open(path, "w") as f, capture(f):
                task.run()
        failed = False
    finally:
        task.ended = time.time()
        task.failed = failed
        if display is not None:
            display.finished(task.name, failed=failed)
    if verbose:
        print("  %s: %.1fs" % (task.name, time.time() - started))

# A failing task's output, which nobody saw: it went to that task's file
# so it would not interleave with the tasks it ran beside.
def _report(task, logs):
    print("task '%s' failed" % task.name)
    if logs is None:
        return
    path = os.path.join(logs, "%s.log" % task.name)
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                print("  %s| %s" % (task.name, line.rstrip()))
