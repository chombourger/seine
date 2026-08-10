# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import time

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
