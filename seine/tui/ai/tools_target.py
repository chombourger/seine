# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# mtda-* tools: the same target-hardware actions '/target' itself
# calls, gated the same way.

import time

from textual.css.query import NoMatches

from . import Tool, _no_args

# Every mtda-* tool calls straight into seine/tui/target.py, same code
# '/target' calls. A gated one is already confirmed by _dispatch()
# before run(); any error comes back as a plain string, not a raise.
def _tool_mtda_status(app, arguments):
    from seine.tui import target
    try:
        state = target.status(app)
    except Exception as e:
        return "target: %s" % e
    return ("power: %s -- uptime: %ss -- storage: %s -- usb: %s"
            % (state["power"], state["uptime"], state["storage"], state["usb"]))

def _tool_mtda_power(app, arguments):
    from seine.tui import target
    state = arguments.get("state")
    if state not in ("on", "off", "toggle"):
        return "'state' must be 'on', 'off', or 'toggle'"
    try:
        target.power(app, state)
    except Exception as e:
        return "target: %s" % e
    return "target %s: done" % state

def _tool_mtda_usb(app, arguments):
    from seine.tui import target
    port, state = arguments.get("port"), arguments.get("state")
    if not port or state not in ("on", "off", "toggle"):
        return "'port' and 'state' ('on'/'off'/'toggle') are both required"
    try:
        target.usb(app, port, state)
    except Exception as e:
        return "target: %s" % e
    return "usb port %s %s: done" % (port, state)

def _tool_mtda_storage(app, arguments):
    from seine.tui import target
    where = arguments.get("where")
    if where not in ("host", "target"):
        return "'where' must be 'host' or 'target'"
    fn = target.storage_to_host if where == "host" else target.storage_to_target
    try:
        fn(app)
    except Exception as e:
        return "target: %s" % e
    return "storage %s: done" % where

def _tool_mtda_write_image(app, arguments):
    from seine.tui import target
    path = arguments.get("path")
    if not path:
        return "'path' (an image file already on this machine) is required"
    try:
        target.write_image(app, path)
    except Exception as e:
        return "target: %s" % e
    return "wrote %s and attached storage to the target" % path

def _tool_mtda_snapshot(app, arguments):
    from seine.tui import target
    try:
        target.snapshot(app)
    except Exception as e:
        return "target: %s" % e
    return "snapshot: done"

def _tool_mtda_rollback(app, arguments):
    from seine.tui import target
    try:
        target.rollback(app)
    except Exception as e:
        return "target: %s" % e
    return "rollback: done"

def _tool_mtda_console_send(app, arguments):
    from seine.tui import target
    data = arguments.get("data")
    if not data:
        return "'data' (the characters to send) is required"
    try:
        target.console_send(app, data)
    except Exception as e:
        return "target: %s" % e
    return "sent"

def _tool_mtda_console_run(app, arguments):
    from seine.tui import target
    cmd = arguments.get("command")
    if not cmd:
        return "'command' is required"
    try:
        return target.console_run(app, cmd) or "(no output)"
    except Exception as e:
        return "target: %s" % e

def _tool_mtda_console_read(app, arguments):
    from seine.tui import target
    which = arguments.get("which", "tail")
    if which not in ("dump", "head", "tail"):
        return "'which' must be 'dump', 'head', or 'tail'"
    try:
        text = getattr(target, "console_" + which)(app)
    except Exception as e:
        return "target: %s" % e
    return text or "(console buffer is empty)"

# Touches ai_state, so it crosses via call_from_thread, same as
# notify_build_finished(). Skipped if the AI is mid-turn already --
# 'waiting' just goes back to idle for a fresh call to pick up.
def _console_wait_finished(app, what, text, error):
    app.target_state.waiting = False
    app.target_state.wait_what = None
    if app.ai_state.busy:
        return
    if error:
        outcome = "failed: %s" % error
    elif text:
        outcome = "matched:\n\n%s" % text
    else:
        outcome = "timed out"
    app.ai_state.busy = True
    app.ai_state.turn_started_at = time.time()
    app.ai_state.messages.append({
        "role": "user",
        "content": "(seine) The console-wait you started for '%s' %s" % (what, outcome)})
    app.ai_state.changed()
    try:
        app.say("AI chat: console-wait for '%s' %s"
                % (what, "matched" if text else "finished"))
    except NoMatches:
        pass
    from seine.tui import ai
    app.run_worker(lambda: ai._run(app), thread=True, exclusive=True, group="ai")

# Runs the wait in its own worker group (not "ai") so a slow mtda
# timeout never blocks the model -- this call returns right away and
# _console_wait_finished() delivers the outcome as an unprompted turn.
def _tool_mtda_console_wait(app, arguments):
    from seine.tui import target
    what = arguments.get("what")
    if not what:
        return "'what' (the text to wait for) is required"
    if app.target_state.waiting:
        return "already waiting in the background for '%s'" % app.target_state.wait_what
    timeout = arguments.get("timeout")
    timeout = float(timeout) if timeout else None
    app.target_state.waiting = True
    app.target_state.wait_what = what

    def run():
        try:
            text = target.console_wait(app, what, timeout=timeout)
        except Exception as e:
            app.call_from_thread(_console_wait_finished, app, what, None, str(e))
        else:
            app.call_from_thread(_console_wait_finished, app, what, text, None)

    app.run_worker(run, thread=True, exclusive=True, group="target-wait")
    return ("waiting in the background for '%s' (timeout: %s) -- you'll get "
            "an unprompted message when it matches or times out; go do "
            "something else meanwhile instead of stalling on this."
            % (what, timeout if timeout else "mtda's default"))

TOOLS = [
    Tool("mtda-status", "Power/uptime/storage/USB status of the real "
        "target -- the same thing '/target status' prints. This "
        "'target' is real hardware driven over mtda (github.com/"
        "siemens/mtda), unrelated to start-build's own 'target' "
        "parameter (a build task name).",
        _no_args(), False, _tool_mtda_status),
    Tool("mtda-power", "Turn the target on/off, or toggle it -- the same "
        "thing '/target on'/'off'/'toggle' does.",
        {"type": "object",
         "properties": {"state": {"type": "string", "enum": ["on", "off", "toggle"]}},
         "required": ["state"]},
        True, _tool_mtda_power),
    Tool("mtda-usb", "Turn one USB port on/off, or toggle it -- the same "
        "thing '/target usb PORT on|off|toggle' does. 'port' is a number "
        "or name, from mtda-status.",
        {"type": "object",
         "properties": {"port": {"type": "string"},
                        "state": {"type": "string", "enum": ["on", "off", "toggle"]}},
         "required": ["port", "state"]},
        True, _tool_mtda_usb),
    Tool("mtda-storage", "Attach the target's shared storage to this "
        "host, or back to the target -- the same thing '/target storage "
        "host|target' does. Storage must be on the host before "
        "mtda-write-image can write to it; mtda-write-image already "
        "re-attaches it to the target afterwards, so this is only "
        "needed to get storage onto the host in the first place, or to "
        "hand it back to the target without writing anything.",
        {"type": "object",
         "properties": {"where": {"type": "string", "enum": ["host", "target"]}},
         "required": ["where"]},
        True, _tool_mtda_storage),
    Tool("mtda-write-image", "Write an image already on this machine to "
        "the target's shared storage, then attach that storage to the "
        "target -- the same thing '/target write IMAGE' does.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "path to an image file on this machine"}},
         "required": ["path"]},
        True, _tool_mtda_write_image),
    Tool("mtda-snapshot", "Snapshot the target's shared storage -- the "
        "same thing '/target snapshot' does.", _no_args(), True, _tool_mtda_snapshot),
    Tool("mtda-rollback", "Roll the target's shared storage back to its "
        "last snapshot -- the same thing '/target rollback' does.",
        _no_args(), True, _tool_mtda_rollback),
    Tool("mtda-console-send", "Send raw characters to the target's "
        "console, unbuffered -- no newline added automatically. The same "
        "thing '/target console send STRING' does.",
        {"type": "object",
         "properties": {"data": {"type": "string"}},
         "required": ["data"]},
        True, _tool_mtda_console_send),
    Tool("mtda-console-run", "Run a command on the target's console and "
        "return its output -- the same thing '/target console run "
        "COMMAND' does.",
        {"type": "object",
         "properties": {"command": {"type": "string"}},
         "required": ["command"]},
        True, _tool_mtda_console_run),
    Tool("mtda-console-read", "Read from the target's console output -- "
        "'tail' (default) for just the last line, 'head' for the first, "
        "or 'dump' for the whole buffer. Prefer 'tail'/'head' over 'dump' "
        "for something like \"did it boot\": far cheaper than paying for "
        "the whole buffer every time. Read-only, no confirmation needed.",
        {"type": "object",
         "properties": {"which": {"type": "string", "enum": ["dump", "head", "tail"],
                                  "description": "default 'tail'"}},
         "required": []},
        False, _tool_mtda_console_read),
    Tool("mtda-console-wait", "Start waiting, in the background, for "
        "'what' to appear in the target's console output, or 'timeout' "
        "seconds to pass -- same match as '/target console wait STRING "
        "[TIMEOUT]', but this call returns immediately instead of "
        "blocking; you'll get an unprompted follow-up message with the "
        "outcome once it matches or times out. Only one wait runs at a "
        "time. Read-only, no confirmation needed.",
        {"type": "object",
         "properties": {"what": {"type": "string"},
                        "timeout": {"type": "number",
                                   "description": "seconds to wait before giving "
                                                  "up; omit for mtda's own default"}},
         "required": ["what"]},
        False, _tool_mtda_console_wait),
]
