# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Vendor cockpit: a live view over 'seine vendor' running in an
# App-level worker, mirroring build.py's BuildState/BuildScreen shape.
# VendorCmd._run() drives the same three waves either way; this just
# adds a display.

import os
import time

from textual.containers import Horizontal
from textual.widgets import RichLog, Static

from seine import tasks
from seine.cache import human, size_of
from seine.progress import elapsed
from seine.tui.base import BaseScreen, StaticPane
from seine.tui.build import MARKS, Tail
from seine.tui.reporter import TextualReporter
from seine.tui.spectree import SpecTree

# What the Vendor screen renders, kept apart from the widgets the same
# way BuildState is. Unlike BuildState, the task list isn't known up
# front: which suites resolve and which sources/binaries fetch is only
# known wave by wave, so 'order'/'rows' grow as tasks start.
class VendorState:
    def __init__(self):
        self.wanted = []
        self.order = []
        self.rows = {}
        self.current = None
        self.worker = None
        self.message = None
        self.error = False
        self.done = False
        self.retries = 0
        # Log directory of the wave currently running; changes per wave.
        self.logs = None
        # 'repo_size' at reset() is the baseline every sample() subtracts
        # back out, since a suite's repository persists across runs --
        # this tracks what the current run itself added.
        self._baseline = 0
        self.repo_size = 0
        self.bytes_downloaded_session = 0
        # Set once by App, same role as BuildState.on_finished.
        self.on_finished = None
        # Set by ai.py's 'start-vendor' tool, never by '/vendor'. Same
        # role as BuildState's own; reset() clears it each run.
        self.notify_ai = False

    @property
    def running(self):
        return self.worker is not None and self.worker.is_running

    def reset(self, wanted):
        self.wanted = list(wanted)
        self.order = []
        self.rows = {}
        self.current = None
        self.message = None
        self.error = False
        self.done = False
        self.notify_ai = False
        self.retries = 0
        self.logs = None
        self._baseline = self._repo_bytes()
        self.repo_size = self._baseline
        self.bytes_downloaded_session = 0

    def _repo_bytes(self):
        from seine.vendor import repository
        return sum(size_of(repository(suite)) for suite in self.wanted)

    # Polled by the screen's tick, not pushed: apt gives no progress
    # stream, so the repository directory size is sampled instead.
    def sample_repo(self):
        self.repo_size = self._repo_bytes()
        self.bytes_downloaded_session = max(0, self.repo_size - self._baseline)

    # Reporter sink, called on the UI thread. Same three methods
    # BuildState implements; wave_logs is the one addition.
    def task_started(self, name):
        if name not in self.rows:
            self.order.append(name)
        row = self.rows.setdefault(
            name, {"state": "pending", "started": None, "elapsed": None})
        row["state"] = "running"
        row["started"] = time.time()
        self.current = name
        # A retried task is a new Task named '<base>#<attempt>'; counted
        # here rather than derived from 'rows' at render time.
        if "#" in name:
            self.retries += 1

    def task_finished(self, name, failed=False):
        row = self.rows.setdefault(
            name, {"state": "pending", "started": None, "elapsed": None})
        row["state"] = "failed" if failed else "done"
        if row["started"] is not None:
            row["elapsed"] = time.time() - row["started"]
        if self.current == name:
            self.current = None

    def say(self, text):
        self.message = text

    def wave_logs(self, path):
        self.logs = path

    # Message not overwritten here: _run()'s final say() already set the
    # per-suite summary a successful run wants shown.
    def finished_ok(self):
        self.done = True
        if self.on_finished:
            self.on_finished()

    def finished_failed(self, text):
        self.done = True
        self.error = True
        self.message = text
        if self.on_finished:
            self.on_finished()

    def render(self):
        if len(self.order) == 0:
            return "no steps yet\n"
        lines = []
        for name in self.order:
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

    def render_stats(self):
        done = sum(1 for r in self.rows.values() if r["state"] == "done")
        failed = sum(1 for r in self.rows.values() if r["state"] == "failed")
        running = sum(1 for r in self.rows.values() if r["state"] == "running")
        lines = [
            "suite%s: %s" % ("" if len(self.wanted) == 1 else "s",
                             ", ".join(self.wanted)),
            "tasks: %d done, %d running, %d failed, %d total"
            % (done, running, failed, len(self.rows)),
        ]
        if self.retries:
            lines.append("retries: %d" % self.retries)
        lines.append("")
        lines.append("downloaded this session: %s" % human(self.bytes_downloaded_session))
        lines.append("repository size: %s" % human(self.repo_size))
        return "\n".join(lines) + "\n"

# Shared by '/vendor' and the AI chat's 'start-vendor' tool: validates
# a build's 'vendor:' section the same way VendorCmd.main() does,
# narrowed to 'suite' when given. Raises ValueError with a message fit
# to show straight to whoever asked.
def prepare(build, suite=None):
    from seine import vendor, utils
    entries = vendor.parse(build.spec)
    if len(entries) == 0:
        raise ValueError("this specification has no 'vendor:' section")
    exclude = vendor.exclusions(build.spec)
    extra_archs = vendor.extra_architectures(build.spec)
    distro = utils.distribution(build.spec)
    available = vendor.named_suites(entries, distro)
    if suite is not None:
        if suite not in available:
            raise ValueError(
                "'%s' is not a suite this specification's 'vendor:' asks "
                "for -- expected one of %s" % (suite, ", ".join(available)))
        wanted = [suite]
    else:
        wanted = available
    unknown = vendor.unconfigured_suites(wanted, distro)
    if len(unknown) > 0:
        raise ValueError(
            "'vendor:' asks for %s, which %s no configured feed -- add it "
            "under 'distribution: feeds:' first"
            % (", ".join(unknown), "has" if len(unknown) == 1 else "have"))
    return distro, entries, exclude, wanted, extra_archs

# Starts 'seine vendor' in an App-level thread worker, mirroring
# start_build(). Refuses to start beside a real build: both drive
# seine.tasks.run(), which isn't built for two job graphs at once.
def start_vendor(app, state, distro, entries, exclude, wanted, refresh=False,
                 extra_archs=()):
    if state.running:
        raise RuntimeError("a vendor is already running")
    if app.build_state.running:
        raise RuntimeError("a build is running -- wait for it to finish first")
    state.reset(wanted)
    reporter = TextualReporter(app, state)

    from seine.vendor import VendorCmd
    cmd = VendorCmd()
    # fetch_source()/fetch_binary() progress is gated behind 'verbose';
    # without it the log tail has nothing to show during the fetch wave.
    cmd.options["verbose"] = True

    def run():
        try:
            cmd._run(distro, entries, exclude, wanted, refresh,
                    extra_archs=extra_archs, display=reporter)
        except (tasks.Failed, tasks.Interrupted) as e:
            app.call_from_thread(state.finished_failed, str(e))
            return
        except Exception as e:
            app.call_from_thread(state.finished_failed, "%s: %s" % (type(e).__name__, e))
            return
        app.call_from_thread(state.finished_ok)

    state.worker = app.run_worker(run, thread=True, exclusive=True, group="vendor")
    app.refresh_indicators()

class VendorScreen(BaseScreen):
    HINT_ADD = [("complete", "cancel", "'/cancel' stops")]

    # Same split as BuildScreen but 50/50 (own ids/CSS, not '#spectree'
    # etc which are 2:1): the stats panel and task list matter here as
    # much as the tree/log.
    def compose(self):
        yield Horizontal(
            SpecTree(id="vendorspectree"),
            StaticPane(Static(id="vendorstats", markup=False), id="vendorstatspane"),
            id="vendormain",
        )
        tail = RichLog(id="vendortail", markup=False, wrap=True, max_lines=4000)
        yield Horizontal(
            tail,
            StaticPane(Static(id="vendortasks", markup=False), id="vendortaskspane"),
            id="vendorrow",
        )
        yield from self.footer()

    def on_mount(self):
        self._tail = Tail()
        super().on_mount()
        self._timer = self.set_interval(1.0, self._tick)
        # See BuildScreen.on_mount: dodges textual's empty-RichLog click crash.
        self.query_one("#vendortail", RichLog).write("")

    def on_unmount(self):
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()

    def update_body(self):
        self.query_one("#vendorstats", Static).update(self.app.vendor_state.render_stats())

    def refresh_data(self):
        super().refresh_data()
        self._redraw()

    def _tick(self):
        state = self.app.vendor_state
        if state.running:
            state.sample_repo()
        self._follow()
        self._redraw()

    def _redraw(self):
        state = self.app.vendor_state
        self.query_one("#vendortasks", Static).update(state.render())
        self.query_one("#vendorstats", Static).update(state.render_stats())
        if state.message:
            self.say(state.message, error=state.error)

    # Tails whichever task is 'current' in the current wave's log
    # directory -- same "one file, not several merged" choice as
    # BuildScreen._follow().
    def _follow(self):
        state = self.app.vendor_state
        name = state.current
        if name is None or state.logs is None:
            return
        path = os.path.join(state.logs, "%s.log" % name)
        self._tail.switch(path)
        text = self._tail.read_new()
        if text:
            self.query_one("#vendortail", RichLog).write(text)
