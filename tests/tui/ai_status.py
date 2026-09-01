#!/usr/bin/env python3

import avocado
import contextlib
import os
import sys
import tempfile
import time
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-ai-tests-"))
os.chdir(tempfile.mkdtemp(prefix="seine-ai-tests-cwd-"))

# The three env vars ('SEINE_LLM_MODEL' etc.) are process-global state,
# same as any other 'SEINE_*' override elsewhere in this suite -- popped
# in every 'setUp()' below, not just where a test sets its own, so a
# leftover from one test can never leak into the next one run in the
# same process.
LLM_ENV = ["SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"]

def _clear_llm_env():
    for name in LLM_ENV:
        os.environ.pop(name, None)

# Duplicated rather than imported from tests/tui/ai.py -- test files
# stay self-contained here, none of them import each other.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# One theme out of what was originally a single 'ToolTable' class in
# tests/tui/ai.py -- split apart (2026-09-01) once it had grown to
# cover every AI tool at once. This file: the build-status,
# live-status and task-log tools.
class ToolTable(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
            from seine.tui.app import SeineApp
        self.ai = ai
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_GISTS_DIR"] = os.path.join(self.workdir, "gists")
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "workbench")
        _clear_llm_env()

    def test_build_status_matches_the_build_screens_own_stage_list(self):
        app = self.SeineApp()
        self.assertEqual(self.ai.TOOLS["build-status"].run(app, {}),
                         app.build_state.render())

    # '_live_status()' folds the *overall* state into the system prompt
    # every turn (see '_run()') -- pinned here on its own, one case per
    # 'BuildState' shape, rather than only indirectly through a live
    # chat loop.
    def test_live_status_is_empty_with_no_spec_selected(self):
        app = self.SeineApp()
        self.assertEqual(self.ai._live_status(app), "")

    def test_live_status_not_started_yet(self):
        app = self.SeineApp()
        app.build_state.order = ["rootfs"]
        app.build_state.rows = {"rootfs": {"needs": [], "state": "pending",
                                           "started": None, "elapsed": None}}
        self.assertIn("not started yet", self.ai._live_status(app))

    def test_live_status_running(self):
        app = self.SeineApp()
        app.build_state.order = ["rootfs"]
        app.build_state.worker = types.SimpleNamespace(is_running=True)
        self.assertIn("running", self.ai._live_status(app))

    def test_live_status_finished(self):
        app = self.SeineApp()
        app.build_state.order = ["rootfs"]
        app.build_state.done = True
        app.build_state.error = False
        self.assertIn("finished", self.ai._live_status(app))

    def test_live_status_failed(self):
        app = self.SeineApp()
        app.build_state.order = ["rootfs"]
        app.build_state.done = True
        app.build_state.error = True
        self.assertIn("failed", self.ai._live_status(app))

    # "Why did my build fail" without naming a step: 'task-log' picks
    # whichever one is marked 'failed' in 'BuildState.rows', the same
    # dict 'BuildScreen' 's own stage list already reads.
    def test_task_log_defaults_to_the_failed_step(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["bootstrap-host", "rootfs"]
        state.rows = {
            "bootstrap-host": {"needs": [], "state": "done", "started": None, "elapsed": 1.0},
            "rootfs": {"needs": ["bootstrap-host"], "state": "failed",
                      "started": None, "elapsed": 2.0},
        }
        logdir = self.workdir
        with open(os.path.join(logdir, "rootfs.log"), "w") as f:
            f.write("line one\nERROR: it broke\n")
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {})
        self.assertIn("ERROR: it broke", text)

    def test_task_log_by_explicit_name(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["rootfs"]
        state.rows = {"rootfs": {"needs": [], "state": "running",
                                 "started": None, "elapsed": None}}
        logdir = self.workdir
        with open(os.path.join(logdir, "rootfs.log"), "w") as f:
            f.write("real content\n")
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        self.assertIn("real content", self.ai.TOOLS["task-log"].run(app, {"task": "rootfs"}))

    def test_task_log_only_tails_the_most_recent_lines(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["rootfs"]
        state.rows = {"rootfs": {"needs": [], "state": "running",
                                 "started": None, "elapsed": None}}
        logdir = self.workdir
        with open(os.path.join(logdir, "rootfs.log"), "w") as f:
            f.write("\n".join("line %d" % i for i in range(500)) + "\n")
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"task": "rootfs"})
        self.assertEqual(len(text.splitlines()), self.ai.LOG_TAIL_LINES)
        self.assertIn("line 499", text)
        self.assertNotIn("line 0\n", text)

    def test_task_log_with_no_task_and_nothing_failed_or_running(self):
        app = self.SeineApp()
        app.build_state.build = type("B", (), {"image": type("I", (), {"logs": self.workdir})()})()
        self.assertIn("no 'task' given", self.ai.TOOLS["task-log"].run(app, {}))

    def test_task_log_missing_file_is_an_error_not_a_crash(self):
        app = self.SeineApp()
        state = app.build_state
        state.build = type("B", (), {"image": type("I", (), {"logs": self.workdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"task": "does-not-exist"})
        self.assertIn("could not read", text)

    # 'pattern' filters server-side, one task named -- only matching
    # lines come back, not the whole tail.
    def test_task_log_pattern_filters_one_tasks_log(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["rootfs"]
        state.rows = {"rootfs": {"needs": [], "state": "done",
                                 "started": None, "elapsed": None}}
        logdir = self.workdir
        with open(os.path.join(logdir, "rootfs.log"), "w") as f:
            f.write("ok: install vim\nWARNING: something\nok: install ssh\n")
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"task": "rootfs", "pattern": "warn"})
        self.assertEqual(text, "WARNING: something")

    # 'task' left out alongside 'pattern' -- every step's own log in
    # 'state.order', each match prefixed with which one, the "any
    # warnings anywhere" case this exists for.
    def test_task_log_pattern_with_no_task_searches_every_step(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["bootstrap-host", "rootfs", "tarball"]
        state.rows = {name: {"needs": [], "state": "done", "started": None, "elapsed": None}
                     for name in state.order}
        logdir = self.workdir
        with open(os.path.join(logdir, "bootstrap-host.log"), "w") as f:
            f.write("clean\n")
        with open(os.path.join(logdir, "rootfs.log"), "w") as f:
            f.write("clean\n")
        with open(os.path.join(logdir, "tarball.log"), "w") as f:
            f.write('level=warning msg="teardown"\n')
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"pattern": "warn"})
        self.assertIn("tarball: ", text)
        self.assertIn("teardown", text)
        self.assertNotIn("bootstrap-host:", text)

    def test_task_log_pattern_with_bad_regex_reports_it_not_a_crash(self):
        app = self.SeineApp()
        state = app.build_state
        state.build = type("B", (), {"image": type("I", (), {"logs": self.workdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"task": "rootfs", "pattern": "("})
        self.assertIn("not a usable pattern", text)

    def test_task_log_pattern_with_no_matches_says_so(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["rootfs"]
        state.rows = {"rootfs": {"needs": [], "state": "done",
                                 "started": None, "elapsed": None}}
        logdir = self.workdir
        with open(os.path.join(logdir, "rootfs.log"), "w") as f:
            f.write("all clean\n")
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"task": "rootfs", "pattern": "warn|error"})
        self.assertEqual(text, "no matching lines")

    # Same "no matching lines" outcome, but across every step -- the
    # "is package X in my image" shape this exists for: a clean build,
    # searched whole rather than one step at a time.
    def test_task_log_pattern_with_no_task_and_no_matches_says_so(self):
        app = self.SeineApp()
        state = app.build_state
        state.order = ["bootstrap-host", "rootfs", "tarball"]
        state.rows = {name: {"needs": [], "state": "done", "started": None, "elapsed": None}
                     for name in state.order}
        logdir = self.workdir
        for name in state.order:
            with open(os.path.join(logdir, "%s.log" % name), "w") as f:
                f.write("nothing interesting here\n")
        state.build = type("B", (), {"image": type("I", (), {"logs": logdir})()})()
        text = self.ai.TOOLS["task-log"].run(app, {"pattern": "sudo"})
        self.assertEqual(text, "no matching lines")

