# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

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
# Nothing here runs anything in parallel: this is the same sequence as
# before, derived rather than assumed. What it buys now is that the order
# can be checked, and that each step can be timed and named when it fails.
class Task:
    def __init__(self, name, run, needs=None):
        self.name = name
        self.run = run
        self.needs = list(needs or [])

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

# Runs them, in that order. 'verbose' prints what each one cost, which is
# what tells a user where a build's time actually goes -- and so which
# steps are worth running beside each other.
def run(tasks, verbose=False):
    for task in ordered(tasks):
        started = time.time()
        task.run()
        if verbose:
            print("  %s: %.1fs" % (task.name, time.time() - started))
