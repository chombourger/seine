# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Test cockpit: mirrors build.py's BuildState/start_build() shape, an
# App-level worker running seine.testing.runner through the same
# TextualReporter -- no test-specific reporting code needed.

import os

from seine.testing import runner
from seine.tui.reporter import TextualReporter

class TestState:
    def __init__(self):
        self.files = []
        self.rows = {}
        self.order = []
        self.worker = None
        self.message = None
        self.error = False
        self.done = False
        self.result = None
        # Running test's spec-tree path, keyed like 'rows'; empty when
        # no spec was given to compute it from.
        self.test_paths = {}
        # Every keyword-level log line seen so far; TestScreen's #tail
        # pane tails this list since there's no log file here.
        self.output_lines = []
        # Bumped on every reset() so TestScreen can tell a fresh run
        # apart from one still going, and clear #tail instead of
        # appending to it.
        self.run_id = 0
        # Set by SeineApp.__init__, same shape as BuildState.on_finished.
        self.on_finished = None
        # Same, for the start edge: a fresh run clears the previous
        # per-test states, so whatever is on screen is stale.
        self.on_started = None
        # Same, for every test's own start/finish: one mark flips,
        # so a listing of them is one mark behind.
        self.on_changed = None

    @property
    def running(self):
        return self.worker is not None and self.worker.is_running

    def reset(self, files, spec=None):
        from seine.tui import spectree
        self.files = files
        self.rows = {}
        self.order = []
        self.message = None
        self.error = False
        self.done = False
        self.result = None
        self.test_paths = spectree.test_paths(spec) if spec else {}
        self.output_lines = []
        self.run_id += 1
        if self.on_started:
            self.on_started()

    # Reporter sink: named task_started/task_finished/sampled to match
    # TextualReporter's calls, not the Reporter protocol's own names.
    def task_started(self, name):
        self.rows.setdefault(name, {"state": "pending"})
        self.rows[name]["state"] = "running"
        if name not in self.order:
            self.order.append(name)
        if self.on_changed:
            self.on_changed()

    def task_finished(self, name, failed=False):
        self.rows.setdefault(name, {"state": "pending"})
        self.rows[name]["state"] = "failed" if failed else "done"
        if self.on_changed:
            self.on_changed()

    def say(self, text):
        self.message = text

    def sampled(self, sample):
        pass

    def output(self, name, line):
        self.output_lines.append("%s| %s" % (name, line))

    def finished_ok(self, result):
        self.done = True
        self.result = result
        self.error = not result.ok
        self.message = result.summary()
        if self.on_finished:
            self.on_finished()

    def finished_failed(self, text):
        self.done = True
        self.error = True
        self.message = text
        if self.on_finished:
            self.on_finished()

    # A failed row's message goes right under it, same as the CLI and
    # the AI chat's 'run-test' tool, so a failure always shows a reason.
    def render(self):
        if len(self.order) == 0:
            return "no test run yet -- '/test SPEC...'\n"
        marks = {"pending": "○", "running": "●", "done": "✔", "failed": "✘"}
        by_name = {t.name: t for t in self.result.tests} if self.result else {}
        lines = []
        for name in self.order:
            lines.append("%s %s" % (marks[self.rows[name]["state"]], name))
            outcome = by_name.get(name)
            if outcome is not None and outcome.failed and outcome.message:
                lines.append("    %s" % outcome.message)
        if self.result is not None:
            outdir = os.path.dirname(self.result.output_xml)
            lines.append("")
            lines.append(self.result.summary())
            lines.append("output under %s" % outdir)
            if not self.result.ok:
                lines.append("see %s/console.log (%s/console.cast) and %s/interactions.json "
                            "for what led up to it" % (outdir, outdir, outdir))
        return "\n".join(lines) + "\n"

# Mirrors start_build()'s shape: a worker thread, a Reporter crossing
# back through call_from_thread. 'spec', already parsed, saves reloading
# a spec the active session has open.
def start_test(app, state, files, spec=None, tags=None, outdir=None):
    if state.running:
        raise RuntimeError("a test run is already running")
    state.reset(files, spec=spec)
    reporter = TextualReporter(app, state)

    def run():
        try:
            result = runner.run_spec(files, spec=spec, tags=tags,
                                     outdir=outdir, reporter=reporter)
        except Exception as e:
            app.call_from_thread(state.finished_failed, "%s: %s" % (type(e).__name__, e))
            return
        app.call_from_thread(state.finished_ok, result)

    state.worker = app.run_worker(run, thread=True, exclusive=True, group="test")
