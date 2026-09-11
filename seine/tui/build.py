# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Build cockpit: a live view over a build running in an App-level worker
# (not a Screen-level one), so navigating away doesn't cancel it.

import os
import re
import time

from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import RichLog, Static

from seine import tasks
from seine.progress import elapsed
from seine.tui.base import BaseScreen, StaticPane
from seine.tui.reporter import TextualReporter
from seine.tui.render import render_overview
from seine.tui.spectree import SpecTree, branch_for, _item_label

MARKS = {"pending": "○", "running": "●", "done": "✔", "failed": "✘"}

# Maps each package:/prepare:/deploy: task name to its 'packages: [i]'
# spec tree node, computed once at build start. fetch:/fetch-upstream:
# tasks name a shared source rather than one package, so they fall
# back to the whole 'packages' branch instead.
def _package_paths(build):
    from seine import packages
    from seine.sbuild import BuilderImage
    source_packages = build.image.packages
    if len(source_packages) == 0:
        return {}
    spec_list = build.spec.get("packages") or []
    # Matched by identity, not equality, so look-alike entries aren't confused.
    index_of = {id(item): i for i, item in enumerate(spec_list)}
    distro = build.spec["distribution"]
    builder = packages.Builder(distro, build.options, BuilderImage(distro, build.options))
    paths = {}
    for package in source_packages:
        index = index_of.get(id(package.spec))
        if index is None:
            continue
        label = _item_label(spec_list[index], index)
        path = ["packages", label]
        for architecture in builder.architectures(package):
            paths["package:%s" % builder.label(package, architecture)] = path
        paths["prepare:%s" % package.name] = path
        paths["deploy:%s" % package.name] = path
    return paths

# Which task's log the BUILD OUTPUT pane should tail: oldest running
# package first, then rootfs, then whatever else is running.
def _log_target(state):
    running = {name for name, row in state.rows.items()
              if row["state"] == "running"}
    if len(running) == 0:
        return state.current
    packages = [name for name in running if branch_for(name) == ("packages",)]
    if packages:
        return min(packages, key=lambda name: state.rows[name]["started"] or 0)
    if "rootfs" in running:
        return "rootfs"
    return min(running, key=lambda name: state.rows[name]["started"] or 0)

# ansible_runner.py pins ANSIBLE_STDOUT_CALLBACK=default, so this
# format is guaranteed regardless of ansible.cfg.
PLAY_RE = re.compile(r"^PLAY \[(.+)\] \*+\s*$")
TASK_RE = re.compile(r"^TASK \[(.+)\] \*+\s*$")
# Playbook is done once this prints, though the rootfs task still has
# work left (_save_downloads()/_finalize()).
PLAY_RECAP_RE = re.compile(r"^PLAY RECAP \*+\s*$")

# What the Build screen renders, kept apart from the widgets so it's
# testable without a running App.
class BuildState:
    def __init__(self):
        self.build = None
        self.order = []
        self.rows = {}
        self.current = None
        self.worker = None
        self.message = None
        self.error = False
        self.done = False
        # Only meaningful while current is 'rootfs': the play/task name
        # scraped from the log tail.
        self.play = None
        self.ansible_task = None
        # package:/prepare:/deploy: name -> spec tree path, see
        # _package_paths(). Computed once in reset(), not every tick.
        self.package_paths = {}
        # Set once by app.py: "a build just finished, redraw if
        # anyone is looking", fired from finished_ok()/finished_failed().
        self.on_finished = None
        # Lets App follow a build onto the vendor screen for the
        # 'vendor' task Image._vendor_task() adds, then back once it's
        # done. Unset for a build with no such task.
        self.on_task_started = None
        self.on_task_finished = None
        # Set by ai.py's 'start-build' tool, never by '/build': tells
        # App._build_finished() whether to give the AI chat an
        # unprompted turn to report the outcome. Reset each build.
        self.notify_ai = False

    @property
    def running(self):
        return self.worker is not None and self.worker.is_running

    def reset(self, build):
        self.build = build
        ordered = tasks.ordered(build.image.tasks())
        self.order = [t.name for t in ordered]
        self.rows = {t.name: {"needs": t.needs, "state": "pending",
                              "started": None, "elapsed": None}
                    for t in ordered}
        self.current = None
        self.message = None
        self.error = False
        self.done = False
        self.notify_ai = False
        self.play = None
        self.ansible_task = None
        self.package_paths = _package_paths(build)

    # Reporter sink: called on the UI thread (TextualReporter has already
    # crossed back from the worker thread).
    def task_started(self, name):
        row = self.rows.setdefault(
            name, {"needs": [], "state": "pending", "started": None, "elapsed": None})
        row["state"] = "running"
        row["started"] = time.time()
        self.current = name
        # Clear leftovers from a previous rootfs run (retry after failure).
        self.play = None
        self.ansible_task = None
        if self.on_task_started:
            self.on_task_started(name)

    def task_finished(self, name, failed=False):
        row = self.rows.setdefault(
            name, {"needs": [], "state": "pending", "started": None, "elapsed": None})
        row["state"] = "failed" if failed else "done"
        if row["started"] is not None:
            row["elapsed"] = time.time() - row["started"]
        if self.current == name:
            self.current = None
        if self.on_task_finished:
            self.on_task_finished(name, failed)

    def say(self, text):
        self.message = text

    def sampled(self, sample):
        self.message = ("load %.2f, %d%% busy"
                        % (sample.get("load") or 0.0,
                           round((sample.get("cpu") or 0.0) * 100)))

    def finished_ok(self):
        self.done = True
        self.message = "build finished"
        if self.on_finished:
            self.on_finished()

    def finished_failed(self, text):
        self.done = True
        self.error = True
        self.message = text
        if self.on_finished:
            self.on_finished()

    # Read off the Image itself rather than tracked separately here.
    @property
    def logs(self):
        return getattr(self.build.image, "logs", None) if self.build else None

    def render(self):
        if len(self.order) == 0:
            return "no steps -- '/use SPEC' first\n"
        # An empty 'packages' barrier is left out here, same as in the
        # plan: nothing to build, so no row for it. The state machine
        # above still tracks it -- task_started()/task_finished() take
        # whatever name the graph ran, shown or not.
        names = set(self.order)
        lines = []
        for name in self.order:
            if tasks.is_empty_barrier(name, names):
                continue
            row = self.rows[name]
            mark = MARKS[row["state"]]
            if row["state"] == "running" and row["started"] is not None:
                extra = "  %s" % elapsed(time.time() - row["started"])
            elif row["elapsed"] is not None:
                extra = "  %s" % elapsed(row["elapsed"])
            else:
                extra = ""
            lines.append("%s %s%s" % (mark, name, extra))
        return "\n".join(lines) + "\n"

# Starts a build in an App-level thread worker, wired to 'state' through
# a TextualReporter. Raises if one is already running.
#
# 'packages_only' and 'target' are one-shot overrides of
# 'build.options["packages_only"/"target"]', restored after the run so a
# later '/build' never inherits them by accident.
def start_build(app, state, build, packages_only=False, target=None):
    if state.running:
        raise RuntimeError("a build is already running")
    # Set before reset(): reset() calls build.image.tasks() synchronously
    # to compute the step list shown here. An unknown target raises here,
    # before the worker thread starts.
    previous_packages_only = build.options.get("packages_only")
    previous_target = build.options.get("target")
    build.options["packages_only"] = packages_only
    build.options["target"] = target
    # What the next plan/overview compares against (see BuildCmd.main()):
    # taken before the build, which writes computed sizes back into the
    # specification as it goes. Dumped, so internal '_'-keys never reach
    # the baseline file.
    files = list(build.options.get("files") or [])
    recorded = build.dump(build.spec)
    state.reset(build)
    reporter = TextualReporter(app, state)

    def run():
        from seine.diffing import remember
        try:
            build.build(reporter=reporter)
        except (tasks.Failed, tasks.Interrupted) as e:
            app.call_from_thread(state.finished_failed, str(e))
            return
        except Exception as e:
            app.call_from_thread(state.finished_failed, "%s: %s" % (type(e).__name__, e))
            return
        finally:
            build.options["packages_only"] = previous_packages_only
            build.options["target"] = previous_target
        # Only a finished build moves the baseline, same as the CLI.
        remember(files, recorded)
        app.call_from_thread(state.finished_ok)

    state.worker = app.run_worker(run, thread=True, exclusive=True, group="build")
    # Start edge of the status-bar "N build" chip; on_finished covers the finish side.
    app.refresh_indicators()

# Polls a growing log file (stat + seek) rather than watching it. A
# fresh Tail per screen mount so a remounted widget starts from the
# current log's beginning.
class Tail:
    def __init__(self):
        self.path = None
        self.offset = 0

    def switch(self, path):
        if path != self.path:
            self.path = path
            self.offset = 0

    def read_new(self):
        if self.path is None or not os.path.isfile(self.path):
            return ""
        size = os.path.getsize(self.path)
        if size < self.offset:
            self.offset = 0
        if size == self.offset:
            return ""
        with open(self.path, errors="replace") as f:
            f.seek(self.offset)
            text = f.read()
            self.offset = f.tell()
        return text

class BuildScreen(BaseScreen):
    HINT_ADD = [("complete", "cancel", "'/cancel' stops")]

    # Only screen with a BUILD OUTPUT/TASKS row: spec tree + #cmd on top,
    # then a second row split the same 2/3 : 1/3 way.
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            StaticPane(Static(id="body", markup=False), id="cmd"),
            id="main",
        )
        # Focusable, unlike #tasklist: Tab cycles spec tree, build
        # output, prompt.
        tail = RichLog(id="tail", markup=False, wrap=True, max_lines=4000)
        yield Horizontal(
            tail,
            StaticPane(Static(id="tasklist", markup=False), id="tasks"),
            id="buildrow",
        )
        yield from self.footer()

    def on_mount(self):
        self._tail = Tail()
        super().on_mount()
        self._timer = self.set_interval(1.0, self._tick)
        # Empty RichLog crashes textual on click; seed a blank line to avoid it.
        self.query_one("#tail", RichLog).write("")

    def on_unmount(self):
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()

    def update_body(self):
        self.query_one("#body", Static).update(render_overview(self.app.context))

    def refresh_data(self):
        super().refresh_data()
        self._redraw()

    def _tick(self):
        # _follow() updates state.play/ansible_task from the log's
        # growth; _redraw() turns that into #tasklist.
        self._follow()
        self._redraw()

    # Not '_render': that's Widget._render(), an internal Textual hook;
    # shadowing it silently broke rendering. NoMatches: worker callbacks
    # and the 1s _tick can land mid-transition, when #tasklist isn't
    # composed yet -- a missed repaint is fine, a crash is not.
    def _redraw(self):
        try:
            state = self.app.build_state
            self.query_one("#tasklist", Static).update(state.render())
        except NoMatches:
            return
        if state.message:
            self.say(state.message, error=state.error)

    # Tails one file, not every running task's log merged together;
    # _log_target() picks the single most relevant one.
    def _follow(self):
        state = self.app.build_state
        name = _log_target(state)
        if name is None or state.logs is None:
            return
        path = os.path.join(state.logs, "%s.log" % name)
        self._tail.switch(path)
        text = self._tail.read_new()
        if text:
            self.query_one("#tail", RichLog).write(text)
            self._scan_ansible(state, text)

    # Scrapes 'PLAY [name] ***'/'TASK [name] ***' out of the rootfs log
    # as it grows (Ansible's stdout goes straight to that file).
    def _scan_ansible(self, state, text):
        for line in text.splitlines():
            match = PLAY_RE.match(line)
            if match:
                state.play, state.ansible_task = match.group(1), None
                continue
            match = TASK_RE.match(line)
            if match:
                state.ansible_task = match.group(1)
                continue
            if PLAY_RECAP_RE.match(line):
                state.play, state.ansible_task = None, None

    # Highlighting lives in spectree.py's highlight_active(), called by
    # BaseScreen's tick for every screen, so a running build stays
    # highlighted even after navigating away.
