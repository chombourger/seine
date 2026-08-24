# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The Test cockpit: mirrors seine/tui/build.py's own BuildState/
# start_build() shape, one App-level worker running seine.testing.runner
# through the same seine.reporter.Reporter TextualReporter already
# implements -- no test-specific reporting code needed here at all.

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
        # A running test's own spec-tree path (spectree.highlight_active_
        # test() reads this), keyed the same as 'rows' -- empty when no
        # spec was given to compute it from.
        self.test_paths = {}
        # Every keyword-level log line seen so far, TestScreen's own
        # #tail pane tails this the way BuildScreen tails a task's log
        # file -- there is no file here, only this growing list.
        self.output_lines = []
        # Bumped on every reset() so TestScreen can tell a fresh run
        # apart from one still going, and clear #tail instead of
        # appending to what a previous run already left there.
        self.run_id = 0

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

    # Reporter sink -- 'task_started'/'task_finished'/'sampled' are
    # TextualReporter's own hardcoded names (seine/tui/reporter.py),
    # not the Reporter protocol's 'started'/'finished' -- BuildState
    # names them this way for the same reason, called on the UI thread.
    def task_started(self, name):
        self.rows.setdefault(name, {"state": "pending"})
        self.rows[name]["state"] = "running"
        if name not in self.order:
            self.order.append(name)

    def task_finished(self, name, failed=False):
        self.rows.setdefault(name, {"state": "pending"})
        self.rows[name]["state"] = "failed" if failed else "done"

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

    def finished_failed(self, text):
        self.done = True
        self.error = True
        self.message = text

    # A failed row's own message goes right under it, the same reason
    # the CLI and the AI chat's own 'run-test' tool both print it beside
    # the test's name rather than only in the summary count -- otherwise
    # this screen is the one place a failure gives no reason at all.
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
            lines.append("")
            lines.append(self.result.summary())
            lines.append("output under %s" % os.path.dirname(self.result.output_xml))
        return "\n".join(lines) + "\n"

# Mirrors start_build()'s own shape: a worker thread, a Reporter crossing
# back through call_from_thread, an outdir under the same logs root a
# multi-group build's own logs land under. 'spec', already parsed, saves
# reloading a spec the active session has open -- see runner.run_spec()'s
# own 'spec' argument.
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
