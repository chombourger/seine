# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# run-test/test-validate/test-result: seine's own Robot Framework
# suites, driven the same way '/test' is.

import os

from . import Tool, _no_args, _single_group, NO_SINGLE_GROUP

# call_from_thread needs a running app, not true in a bare-App unit
# test. Caught the same way _reload_and_highlight() does: the real
# change already happened, only the UI redraw has nothing to refresh.
def _call_if_running(app, fn, *args):
    try:
        app.call_from_thread(fn, *args)
    except RuntimeError:
        pass

# 'files' defaults to the active spec's own loaded_files, same as
# start-build -- 'test:' is an ordinary section of it, not a separate
# file. An explicit 'files' still works for a spec not currently active.
def _test_files(app, arguments):
    files = arguments.get("files")
    if files:
        return files, None
    build = _single_group(app)
    if build is None:
        return None, NO_SINGLE_GROUP + " (or give 'files' directly)"
    return build.loaded_files, build.spec

# Runs synchronously (a suite's own 'while:'/'retry_until:' already
# carries a limit, nothing to background). Reports via TextualReporter,
# the same bridge '/test' uses, so the Test screen sees the same rows.
def _tool_run_test(app, arguments):
    from seine.testing import available
    if not available():
        return ("robotframework is not installed -- 'seine test' is "
                "disabled (pip install seine[test])")
    if app.test_state.running:
        return "a test run is already running -- test-result once it's done"
    files, spec = _test_files(app, arguments)
    if files is None:
        return spec  # the error message, in that case

    _call_if_running(app, app.test_state.reset, files, spec)
    from seine.testing import runner
    from seine.tui.reporter import TextualReporter
    try:
        result = runner.run_spec(files, spec=spec, tags=arguments.get("tags") or None,
                                 reporter=TextualReporter(app, app.test_state))
    except Exception as e:
        _call_if_running(app, app.test_state.finished_failed, str(e))
        return "could not run: %s" % e
    _call_if_running(app, app.test_state.finished_ok, result)
    lines = [result.summary()]
    for t in result.tests:
        if t.failed:
            lines.append("  FAIL %s: %s" % (t.name, t.message))
    if not result.ok:
        outdir = os.path.dirname(result.output_xml)
        lines.append("see %s/console.log and %s/interactions.json for what "
                     "led up to it (bash/read can open either)" % (outdir, outdir))
    return "\n".join(lines)

# Dry run only -- Robot resolves every keyword and checks its arguments
# without calling any, so this never touches real hardware. Ungated
# unlike run-test: proves a 'test:' section is well-formed before
# spending a real hardware run on it.
def _tool_test_validate(app, arguments):
    from seine.testing import available
    if not available():
        return "robotframework is not installed -- 'seine test' is disabled"
    files, spec = _test_files(app, arguments)
    if files is None:
        return spec
    from seine.testing import runner
    try:
        result = runner.run_spec(files, spec=spec, dryrun=True)
    except (OSError, ValueError) as e:
        return "invalid: %s" % e
    bad = [t for t in result.tests if t.failed]
    if not bad:
        return "valid -- %s" % result.summary()
    lines = ["invalid -- %s" % result.summary()]
    lines += ["  %s: %s" % (t.name, t.message) for t in bad]
    return "\n".join(lines)

def _tool_test_result(app, arguments):
    state = app.test_state
    if len(state.order) == 0:
        return "no test run yet this session"
    if state.running:
        return "still running: " + ", ".join(
            name for name in state.order if state.rows[name]["state"] == "running")
    return state.render()

TOOLS = [
    Tool("test-validate", "Robot Framework's own dry run over the "
        "active spec's own 'test:' section (or 'files', named "
        "directly): every step's keyword is resolved and its arguments "
        "checked, but no keyword body actually runs -- nothing touches "
        "real hardware. Run this after spec-update/spec-create touches "
        "a 'test:' section, before offering run-test at all -- a suite "
        "that fails to validate is worth fixing first, not worth a real "
        "hardware run to discover the same thing more slowly. Ungated, "
        "unlike run-test: nothing here has a real-world effect.",
        {"type": "object",
         "properties": {"files": {"type": "array", "items": {"type": "string"},
                                  "description": "spec file(s) to load instead "
                                                 "of the active one"}},
         "required": []},
        False, _tool_test_validate),
    Tool("run-test", "Run the active spec's own 'test:' section (or "
        "'files', named directly) against the real target -- the same "
        "thing 'seine test'/'/test' does; a specification carries its "
        "tests the same way it carries its packages/playbook/image, so "
        "there is nothing else to point this at. 'tags' (optional) runs "
        "only tests carrying at least one of them. Unlike start-build/"
        "mtda-console-wait, this blocks until the suite finishes -- a "
        "suite's own 'while:'/'retry_until:' steps already carry a "
        "timeout, so there is no open-ended wait to background here; "
        "keep suites CI-sized rather than ones with a very long poll. "
        "Real hardware actions (power, console, keyboard/mouse) run as "
        "the suite dictates, which is why this is gated rather than "
        "ungated like a read tool. This never writes an image to the "
        "target itself -- every test boots whatever is already on its "
        "shared storage, unrelated to whichever build most recently "
        "finished. REQUIRED before calling this after a build or spec "
        "change: confirm the target's storage already holds *this* "
        "build's own image -- this session wrote it with "
        "'mtda-write-image' (image path from 'artifacts'), or the "
        "person says it's already there. Neither true yet? Offer "
        "'mtda-write-image' first and wait for that approval -- do not "
        "call run-test against an image you cannot confirm is current, "
        "and do not report a pass as evidence about content just built "
        "or changed if that write never happened.",
        {"type": "object",
         "properties": {"files": {"type": "array", "items": {"type": "string"},
                                  "description": "spec file(s) to load instead "
                                                 "of the active one"},
                        "tags": {"type": "array", "items": {"type": "string"},
                                "description": "run only tests carrying at "
                                               "least one of these tags"}},
         "required": []},
        True, _tool_run_test),
    Tool("test-result", "Per-test outcome of the test run this TUI "
        "session itself ran (run-test, '/test', or the Test screen) -- "
        "pass/fail/running per test, and the summary line, the same "
        "text the Test screen shows. Says 'no test run yet' if nothing "
        "has run this session.",
        _no_args(), False, _tool_test_result),
]
