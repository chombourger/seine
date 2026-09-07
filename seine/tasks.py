# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import concurrent.futures
import os
import signal
import sys
import threading
import time

# Per-thread output stream for the current task. Unset in sequential
# builds (output goes straight to the terminal). Set when tasks run in
# parallel, so each task's output goes to its own file instead of
# interleaving with others.
_local = threading.local()

def output():
    return getattr(_local, "output", None)

# Sends a task's output to 'stream' for as long as this is held. 'stream'
# must be a real file, since its fd is passed to podman for container output.
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

# Replaces sys.stdout while a build runs. Writes go to the current task's
# stream if it has one, else to the real terminal.
class _Stdout:
    def __init__(self, terminal):
        self.terminal = terminal

    def __getattr__(self, name):
        return getattr(output() or self.terminal, name)

    def write(self, text):
        stream = output() or self.terminal
        written = stream.write(text)
        # Flush so a task's log can be tailed while it still runs.
        stream.flush()
        return written

def install():
    if not isinstance(sys.stdout, _Stdout):
        sys.stdout = _Stdout(sys.stdout)

# One step of a build, and what it needs before it can run.
class Task:
    def __init__(self, name, run, needs=None):
        self.name = name
        self.run = run
        self.needs = list(needs or [])
        # Timing and outcome, filled in as the task runs.
        self.started = None
        self.ended = None
        self.failed = False

    def __repr__(self):
        return "Task(%r)" % self.name

# Tasks sorted so every dependency runs first, ties broken by declaration
# order. Stable, so logs stay comparable between runs.
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

# Prefixes every task name with 'prefix', so lists like 'rpi4.yml' and
# 'pc.yml' can run together without colliding on a name like 'rootfs'.
# Only needs that name another task in 'tasks' are prefixed; a need
# naming something outside the list (a shared barrier) is left as-is.
def namespaced(tasks, prefix):
    names = {task.name for task in tasks}
    if len(names) != len(tasks):
        raise ValueError(
            "duplicate task name in the list being prefixed '%s:'" % prefix)
    return [Task("%s:%s" % (prefix, task.name), task.run,
                 needs=["%s:%s" % (prefix, need) if need in names else need
                        for need in task.needs])
            for task in tasks]

# Tasks nothing else in 'tasks' depends on -- the tasks that mark the
# list as done. Call before namespaced(): 'needs' here are unprefixed.
def sinks(tasks):
    needed = {need for task in tasks for need in task.needs}
    return [task.name for task in tasks if task.name not in needed]

# 'names' plus every task in 'tasks' they depend on, directly or
# transitively. A name not found in 'tasks' is skipped, not an error.
def ancestors(tasks, names):
    by_name = {task.name: task for task in tasks}
    seen, pending = set(), list(names)
    while len(pending) > 0:
        name = pending.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        pending += by_name[name].needs
    return [task for task in tasks if task.name in seen]

# True if every one of 'tasks' ran and none failed. For checking a
# subset of a merged run (see ancestors()) rather than the whole run.
def succeeded(tasks):
    return all(task.started is not None and task.failed == False
              for task in tasks)

# Raised when a parallel build has one or more failed tasks. Tasks
# already running are left to finish rather than killed, so each step's
# own cleanup still runs. Reports every failure, not just the first, and
# lists tasks that never ran.
class Failed(Exception):
    def __init__(self, failures, cancelled):
        self.failures = failures
        self.cancelled = cancelled
        names = ", ".join(name for name, _ in failures)
        message = "%s failed" % names
        if len(cancelled) > 0:
            message += " (%s did not run)" % ", ".join(sorted(cancelled))
        for name, error in failures:
            said = str(error) or error.__class__.__name__
            message += "\n  %s: %s" % (name, said)
            # Include the command's own last output line, e.g. what
            # "exit status 255" actually meant, not just the exit code.
            for line in Failed._spoke(error):
                message += "\n    %s" % line
        super().__init__(message)

    @staticmethod
    def _spoke(error):
        said = getattr(error, "output", None) or getattr(error, "stderr", None)
        if isinstance(said, bytes):
            said = said.decode("utf-8", "replace")
        return str(said).splitlines() if said else []

# Raised on Ctrl-C: like Failed, running tasks finish instead of being
# killed. A second Ctrl-C falls through to the default KeyboardInterrupt.
class Interrupted(Exception):
    def __init__(self, cancelled):
        self.cancelled = cancelled
        message = "interrupted"
        if len(cancelled) > 0:
            message += " (%s did not run)" % ", ".join(sorted(cancelled))
        super().__init__(message)

_interrupted = threading.Event()
_running = set()
_running_lock = threading.Lock()
# The active display, if any, set by run() for interrupt() to write to.
_display = None

class _interruptible:
    def __enter__(self):
        _interrupted.clear()
        try:
            self.previous = signal.signal(signal.SIGINT, self._asked)
        except ValueError:
            # Not the main thread; no signal handler can be installed here.
            self.previous = None
        return self

    def __exit__(self, *args):
        if self.previous is not None:
            signal.signal(signal.SIGINT, self.previous)
        return False

    def _asked(self, signum, frame):
        signal.signal(signal.SIGINT, self.previous or signal.SIG_DFL)
        interrupt()

# Same as pressing Ctrl-C, but callable from code (also used by tests,
# which run off the main thread where SIGINT isn't delivered).
def interrupt():
    _interrupted.set()
    with _running_lock:
        waiting = len(_running)
    said = ("interrupted: waiting for %d task(s) to finish, "
            "starting no more" % waiting)
    if _display is not None:
        _display.say(said)
        return
    # Not print(): a task's output goes to its own file, not the terminal.
    sys.stderr.write("\n%s\n" % said)

# Prints the run order run() would take, without running anything.
def describe(tasks):
    for task in ordered(tasks):
        if len(task.needs) == 0:
            print("  %s" % task.name)
        else:
            print("  %-24s after %s" % (task.name, ", ".join(task.needs)))

# Runs the tasks. 'jobs' caps how many run at once (1 = sequential,
# the default). 'verbose' prints each task's duration.
def run(tasks, jobs=1, verbose=False, logs=None, display=None):
    global _display
    tasks = ordered(tasks)
    if logs is not None:
        install()
    _display = display
    try:
        with _interruptible():
            if jobs <= 1:
                _sequential(tasks, verbose, logs, display)
                return

            install()
            with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
                _parallel(tasks, pool, jobs, verbose, logs, display)
    finally:
        _display = None

def _sequential(tasks, verbose, logs, display):
    for index, task in enumerate(tasks):
        if _interrupted.is_set():
            raise Interrupted([t.name for t in tasks[index:]])
        _run_one(task, verbose, logs, display)
    if _interrupted.is_set():
        raise Interrupted([])

def _parallel(tasks, pool, jobs, verbose, logs, display):
    done = set()
    failures = []
    running = {}
    waiting = list(tasks)

    while len(waiting) > 0 or len(running) > 0:
        # Start nothing new after a failure or interrupt; let running tasks finish.
        if len(failures) == 0 and _interrupted.is_set() == False:
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
    if _interrupted.is_set():
        raise Interrupted([t.name for t in waiting])

def _run_one(task, verbose, logs, display=None):
    started = time.time()
    task.started = started
    if display is not None:
        display.started(task.name)
    with _running_lock:
        _running.add(task.name)
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
        with _running_lock:
            _running.discard(task.name)
        if display is not None:
            display.finished(task.name, failed=failed)
    if verbose:
        print("  %s: %.1fs" % (task.name, time.time() - started))

# Prints a failed task's output, which went to its own log file instead
# of the terminal.
def _report(task, logs):
    print("task '%s' failed" % task.name)
    if logs is None:
        return
    path = os.path.join(logs, "%s.log" % task.name)
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                print("  %s| %s" % (task.name, line.rstrip()))
