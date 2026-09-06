# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional AI chat: a LiteLLM tool-calling loop over the same read-only
# render layer every screen uses, plus tools that write (start-build/
# cancel-build/spec-update/spec-create), each gated by ConfirmAction.
# Off unless llm_model is set (configured() below).
#
# litellm/ruamel.yaml are the only new dependencies, scoped to this
# package alone (setup.py's 'ai' extra). ruamel.yaml specifically for
# spec-update: it round-trips comments/formatting that a plain PyYAML
# parse-then-dump would silently drop -- see _spec_update_plan() in
# tools_spec.py.
#
# Split by tool domain: this file holds the shared infrastructure
# (settings/config, Preview/Plan/Tool, the tool registry, ConfirmAction/
# confirm/_dispatch, AIState and the chat loop itself) plus a handful of
# tools with no better home (overview/plan/.../doctor/bash). Every other
# tool lives in tools_<domain>.py or web.py, each contributing its own
# module-level TOOLS list that gets folded into the one below.

import difflib
import json
import os
import re
import threading
import time
from typing import NamedTuple

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static

from seine import settings
from seine.container import ContainerEngine
from seine.utils import redact, redactions

# Same duck-typing as seine.tui.target's own _socket_send(): 'app' here
# is not always a real SeineApp (see MtdaTools's stand-in app in
# tests/tui/ai.py, which has no '_socket_send' at all).
def _socket_send(app, event):
    send = getattr(app, "_socket_send", None)
    if send is not None:
        send(event)

# SEINE_LLM_MODEL/SEINE_LLM_API_BASE override the settings.json values,
# same as SEINE_CACHE_DIR does elsewhere. SEINE_LLM_API_KEY has no
# settings.json field at all -- it is the only source, always.
def _resolved():
    current = settings.load()
    model = os.environ.get("SEINE_LLM_MODEL") or current["llm_model"]
    api_base = os.environ.get("SEINE_LLM_API_BASE") or current["llm_api_base"]
    api_key = os.environ.get("SEINE_LLM_API_KEY")
    return model, api_base, api_key

def configured():
    model, _, _ = _resolved()
    return bool(model)

# Not yet looked up, told apart from "looked up, and it's unknown"
# (None) -- the lookup below is one HTTP round-trip, done once per
# AIState rather than once per question.
_UNSET = object()

# Two sources for the number ChatScreen's context-fill bar needs: the
# server's own /models listing first, then litellm.get_max_tokens()'s
# static database. Neither working is not an error -- None either way,
# read as "show the raw count, no bar against a guessed ceiling".
def _lookup_context_max(model, api_base, api_key):
    if api_base:
        try:
            import urllib.request
            request = urllib.request.Request(api_base.rstrip("/") + "/models")
            if api_key:
                request.add_header("Authorization", "Bearer %s" % api_key)
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read())
            wanted = model.split("/", 1)[-1]  # litellm's 'openai/<name>' prefix, stripped
            for entry in data.get("data", []):
                if entry.get("id") == wanted and entry.get("max_model_len"):
                    return entry["max_model_len"]
        except Exception:
            pass
    try:
        import litellm
        return litellm.get_max_tokens(model)
    except Exception:
        return None

# Plain text, not a Python string -- editing wording (or feeding a
# frontier model both this file and a batch of real transcripts,
# ContainerEngine.chats() below, to suggest a better one) needs no code
# change either way. Read fresh each call ('seine/kernel's own
# 'KERNEL_RULES' follows the same "a path constant, opened by whoever
# needs it" shape), not cached at import -- a person iterating on the
# wording sees the next question pick it up without restarting.
SYSTEM_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "system_prompt.txt")

# Everything above the file's own '\n---\n' marker is editing guidance
# for whoever next changes system_prompt.txt, not the model -- costs
# nothing every turn. No marker (edited without it) falls back to the
# whole file rather than sending nothing.
def _system_prompt():
    with open(SYSTEM_PROMPT_FILE) as f:
        text = f.read()
    _, marker, rest = text.partition("\n---\n")
    return rest.lstrip() if marker else text

def _no_args():
    return {"type": "object", "properties": {}, "required": []}

def _single_group(app):
    if not app.context.active or len(app.context.builds) != 1:
        return None
    return app.context.builds[0]

NO_SINGLE_GROUP = ("no single active specification -- '/use SPEC' first "
                   "(multi-group builds aren't driven from the TUI yet)")

# A tool that just wraps one render_*() -- the same text a person
# reading that screen already sees, nothing computed twice. str()'d:
# render_doctor() hands back a styled rich.Text (for the Doctor
# screen's colouring), not a plain str like every other render_*().
def _render_tool(name, with_context=True):
    def run(app, arguments):
        from seine.tui import render
        fn = getattr(render, name)
        return str(fn(app.context) if with_context else fn())
    return run

_tool_overview = _render_tool("render_overview")
_tool_plan = _render_tool("render_plan")
_tool_packages = _render_tool("render_packages")
_tool_analyze = _render_tool("render_analyze")
_tool_artifacts = _render_tool("render_artifacts")
_tool_doctor = _render_tool("render_doctor", with_context=False)

# Installed (seine/data/docs/, generated at build time) is checked
# first; a checkout or editable install (never build_py'd) falls back
# to the repo root's own docs/. Read fresh each call, not cached --
# same spirit as SYSTEM_PROMPT_FILE's own lookup.
def _docs_dir():
    installed = os.path.join(os.path.dirname(SYSTEM_PROMPT_FILE), "docs")
    if os.path.isdir(installed):
        return installed
    checkout = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "docs")
    return checkout if os.path.isdir(checkout) else None

# Sibling of SYSTEM_PROMPT_FILE, not the installed/checkout dance
# _docs_dir() does -- these cluster files ship as ordinary package_data
# (setup.py) the same way system_prompt.txt itself does, so there is
# normally no "not shipped" case the way a repo's own docs/ has; still
# checked defensively below, a broken install being no reason to crash
# the one tool that could otherwise say so.
PROMPT_DOCS_DIR = os.path.join(os.path.dirname(SYSTEM_PROMPT_FILE), "prompt")

# One tool, two directories -- the prompt's own cluster files (always
# there) and the project's written docs/*.md (only with a checkout or a
# build that included it) -- rather than a second tool the model has to
# learn is a near-duplicate of this one. Different extensions keep a
# name from ever meaning two different files at once.
def _doc_sources():
    sources = []
    if os.path.isdir(PROMPT_DOCS_DIR):
        sources.append((PROMPT_DOCS_DIR, ".txt"))
    docs_dir = _docs_dir()
    if docs_dir:
        sources.append((docs_dir, ".md"))
    return sources

# A gated tool's own preview, handed back to _dispatch(): ok=False means
# refuse outright before ConfirmAction ever opens; ok=True means message
# is a redacted diff to show for review.
class Preview(NamedTuple):
    ok: bool
    message: str

# The edit a spec-update call would make, computed once and shared by
# its preview and its run so the two can never drift apart.
class Plan(NamedTuple):
    ok: bool
    message: str        # the error (not ok), or a redacted diff (ok)
    path: str = None    # real on-disk path to write -- only when ok
    new_text: str = None  # the whole file's new content -- only when ok

# A unified diff with every redact: pattern applied line by line -- this
# diff lands in the tool's return text and reaches the remote model, so
# it needs the same redaction dump_file() applies elsewhere. The real
# new/old text (unredacted) is what actually gets written on approval.
# 'spec' is the active build's own spec (for its 'redact:' patterns), or
# None where there isn't one to redact against -- a gist lives outside
# any build.
def _redacted_diff(spec, old_text, new_text, from_path, to_path):
    patterns = redactions(spec)
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                fromfile=from_path, tofile=to_path, lineterm="")
    return "\n".join(redact(line, patterns) for line in diff)

# ruamel.yaml's round trip preserves comments/ordering, but dumps at its
# own fixed default indent regardless of what the source file used --
# without this, a one-line edit to a deeper-indented file reflows the
# whole thing. Detected from the file's own first sequence/mapping
# occurrence and reused for the whole document -- an improvement, not a
# full fix, since YAML().indent() has no per-node granularity.
_SEQUENCE_INDENT_RE = re.compile(r"^([ ]*)\S[^\n]*:[ \t]*\n([ ]*)-[ ]", re.MULTILINE)
_MAPPING_INDENT_RE = re.compile(r"^([ ]*)\S[^\n]*:[ \t]*\n([ ]*)[^-\s][^\n]*:", re.MULTILINE)

def _detect_indent(text):
    kwargs = {}
    match = _SEQUENCE_INDENT_RE.search(text)
    if match:
        parent, dash = len(match.group(1)), len(match.group(2))
        if dash > parent:
            offset = dash - parent
            kwargs["sequence"] = offset + 2  # '- ', the minimal (no extra padding) case
            kwargs["offset"] = offset
    match = _MAPPING_INDENT_RE.search(text)
    if match:
        parent, child = len(match.group(1)), len(match.group(2))
        if child > parent:
            kwargs["mapping"] = child - parent
    return kwargs

# Says nothing about how this runs (a throwaway container, HostBootstrap,
# a bind mount) -- same as start-build's own description never mentions
# the container it starts. All of that is sources.bash()'s business, not
# a contract the model needs.
def _tool_bash(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    command = arguments.get("command")
    if not command:
        return "bash needs 'command'"
    from seine import sources
    try:
        return sources.bash(command, build.spec["distribution"],
                            cwd=arguments.get("cwd"))
    except ValueError as e:
        return "could not run: %s" % e

class Tool(NamedTuple):
    name: str
    description: str
    parameters: dict
    gated: bool
    run: object  # (app, arguments: dict) -> str
    # (app, arguments: dict) -> Preview, only for a gated tool whose
    # effect depends on 'arguments' -- 'None' (every gated tool before
    # 'spec-update'/'spec-create') keeps 'confirm()' 's own fallback: the
    # tool's static 'description' plus a plain dump of 'arguments'.
    preview: object = None

# Imported here, after Tool/_no_args/_single_group/NO_SINGLE_GROUP/
# Preview/Plan/_redacted_diff/_detect_indent/_doc_sources/_socket_send
# above, before TOOLS below -- each submodule's own 'from . import ...'
# reaches back into this still-loading package, so those names must
# already exist when it runs.
from . import tools_build, tools_gist, tools_source, tools_spec, tools_target, tools_test
from . import web as web_tools

# Re-exported so 'self.ai.AUDIT_LOG_MAX_ROWS' etc keeps working the same
# way it did when everything lived in one file -- these are read-only,
# never monkeypatched by a test, so a plain import is enough.
from .tools_build import AUDIT_LOG_MAX_ROWS, LOG_TAIL_LINES
from .tools_spec import SPEC_DUMP_CHUNK_LINES

TOOLS = {t.name: t for t in [
    Tool("overview", "Build status, last-build timing, whether the "
        "specification changed since the last build.", _no_args(), False, _tool_overview),
    Tool("plan", "The merged specification, diffed against the last "
        "real build of it.", _no_args(), False, _tool_plan),
    Tool("packages", "Packages rebuilt from source, and what the last "
        "SBOM found installed.", _no_args(), False, _tool_packages),
    Tool("analyze", "Where the time went in the last recorded build, "
        "and rootfs size over recorded runs.", _no_args(), False, _tool_analyze),
    Tool("artifacts", "What the last build wrote under the deploy "
        "directory.", _no_args(), False, _tool_artifacts),
    Tool("doctor", "Whether this machine has what a build needs.",
        _no_args(), False, _tool_doctor),
    Tool("bash", "Run a shell command. Its working directory is the "
        "workbench (source-list, source-pull) or, with 'cwd', a "
        "directory under it -- nothing outside that is reachable, and "
        "output is capped, so narrow the command rather than ask for "
        "everything at once. Use this to scan a pulled source ('grep -r', "
        "'find', './configure --help') or answer anything read alone "
        "can't.",
        {"type": "object",
         "properties": {"command": {"type": "string"},
                        "cwd": {"type": "string",
                               "description": "a subdirectory of the "
                                              "workbench, default its root"}},
         "required": ["command"]},
        False, _tool_bash),

    *tools_build.TOOLS,
    *tools_spec.TOOLS,
    *tools_gist.TOOLS,
    *tools_source.TOOLS,
    *tools_target.TOOLS,
    *tools_test.TOOLS,
    *web_tools.TOOLS,
]}

TOOL_SCHEMAS = [{"type": "function",
                 "function": {"name": t.name, "description": t.description,
                             "parameters": t.parameters}}
                for t in TOOLS.values()]

# A pending action's own modal -- "Yes"/"No" as a two-row OptionList,
# same shape as SettingsScreen/HelpScreen. on_result is called with a
# bool from the UI thread; the worker thread that opened this is
# blocked on a threading.Event until then -- pushing a modal is
# fire-and-forget from here, the wait happens on the caller's thread.
#
# A unified diff line by line, into a Text built with .append(literal,
# style=...) rather than a markup string, so a package name containing
# a literal '[' cannot spoof or break the review dialog's formatting.
# +++/--- checked before a bare +/- line, since a header starts with one too.
def _diff_text(diff):
    text = Text()
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            text.append(line + "\n", style="bold")
        elif line.startswith("@@"):
            text.append(line + "\n", style="bold cyan")
        elif line.startswith("+"):
            text.append(line + "\n", style="white on #266100")  # rgb(38,97,0)
        elif line.startswith("-"):
            text.append(line + "\n", style="white on #590000")  # rgb(89,0,0)
        else:
            text.append(line + "\n")
    return text

class ConfirmAction(ModalScreen):
    BINDINGS = [Binding("escape", "deny", show=False)]

    DEFAULT_CSS = """
    ConfirmAction { align: center middle; }
    #confirmpane {
        width: 70%; height: auto; max-height: 90%;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #confirmtitle { color: blue; text-style: bold; }
    #confirmdesc { padding: 1 0; }
    #confirmargs { padding: 0 0 1 0; color: $text-muted; }
    #confirmfile { text-style: bold; padding: 1 0 0 0; }
    #confirmdiffscroll { max-height: 20; margin: 0 0 1 0; }
    """

    def __init__(self, tool, arguments, preview, on_result):
        super().__init__()
        self.tool = tool
        self.arguments = arguments
        # A redacted diff when tool.preview produced one, None for a
        # gated tool that doesn't (start-build/cancel-build) -- decides
        # which review layout compose() below shows.
        self.preview = preview
        self.on_result = on_result

    # No preview: plain 'key: value' lines, not markup (an argument
    # value is arbitrary model/tool-supplied text). With preview: a
    # 'file:' line plus the diff, coloured, in its own scrollable region
    # -- a long diff must not push Yes/No off screen.
    def compose(self):
        with Vertical(id="confirmpane"):
            yield Static("seine wants to run: %s" % self.tool.name, id="confirmtitle")
            yield Static(self.tool.description, id="confirmdesc")
            if self.preview is not None:
                # 'path' (spec-update/spec-create): a file about to be
                # written. 'fragment' (side-load/side-unload): a file
                # about to be loaded into (or dropped out of) the
                # session, nothing written -- same "what does this
                # touch" clarity, worded for which one it actually is.
                path = self.arguments.get("path")
                fragment = self.arguments.get("fragment")
                if path:
                    yield Static("file: %s" % path, id="confirmfile", markup=False)
                elif fragment:
                    verb = "unloading" if self.tool.name == "side-unload" else "loading"
                    yield Static("%s: %s" % (verb, fragment), id="confirmfile", markup=False)
                with VerticalScroll(id="confirmdiffscroll"):
                    yield Static(_diff_text(self.preview), id="confirmdiff")
            elif self.arguments:
                lines = "\n".join("%s: %s" % (k, v) for k, v in self.arguments.items())
                yield Static(lines, id="confirmargs", markup=False)
            options = OptionList("Yes", "No", id="confirmoptions")
            yield options

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        self._resolve(event.option_index == 0)

    def action_deny(self):
        self._resolve(False)

    def _resolve(self, approved):
        self.on_result(approved)
        self.app.pop_screen()

# Blocks the worker thread, not the UI thread, until a person answers.
# Polled rather than a bare event.wait(): quitting while a modal sits
# open would otherwise block forever, since nothing would ever call
# resolved() once the app is gone. Cancellation is treated as a denial
# -- quitting mid-approval must never quietly do the thing it was
# about to ask about. Public (no leading '_'): commands.py's '/target'
# calls this too, from its own thread worker -- same requirement, same
# modal, not a separate confirm system.
def confirm(app, tool, arguments, preview):
    from textual.worker import get_current_worker
    event = threading.Event()
    answer = {}

    def resolved(approved):
        answer["approved"] = approved
        event.set()
        _socket_send(app, {"type": "confirm_resolved", "tool": tool.name,
                           "approved": approved})

    def open_modal():
        app.push_screen(ConfirmAction(tool, arguments, preview, resolved))

    app.call_from_thread(open_modal)
    _socket_send(app, {"type": "confirm_shown", "tool": tool.name,
                       "description": tool.description, "arguments": arguments,
                       "preview": preview})
    worker = get_current_worker()
    while not event.wait(timeout=0.2):
        if worker.is_cancelled:
            return False
    return answer.get("approved", False)

# Append-only audit trail of gated tool calls -- what the AI actually
# did (or was refused), not the chat transcript's own record of what was
# said. 'result' is capped: it's already what was sent back to the
# model, not a place to duplicate a multi-KB task-log for its own sake.
AUDIT_RESULT_CAP = 2000

def _audit(tool, arguments, approved, result, started):
    entry = {"ts": started, "tool": tool.name, "approved": approved,
             "arguments": arguments, "result": str(result)[:AUDIT_RESULT_CAP]}
    path = ContainerEngine.audit()
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    os.chmod(path, 0o600)

def _dispatch(app, call):
    tool = TOOLS.get(call.function.name)
    if tool is None:
        return "unknown tool '%s'" % call.function.name
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except ValueError:
        arguments = {}
    if tool.gated:
        started = time.time()
        preview = None
        # A bad call is refused here, before anyone is asked to approve
        # anything -- confirm()'s modal is for reviewing a real, valid
        # change, not for rejecting a broken request. Not audited: no
        # real action was ever on the table to approve or deny.
        if tool.preview:
            pre = tool.preview(app, arguments)
            if not pre.ok:
                return pre.message
            preview = pre.message
        if not confirm(app, tool, arguments, preview):
            _audit(tool, arguments, False, "denied by user", started)
            return "denied by user"
        result = tool.run(app, arguments)
        _audit(tool, arguments, True, result, started)
        return result
    return tool.run(app, arguments)

# RichLog.write() is one full row per call, not an append-in-place
# stream -- a reply still arriving lands in a small Static (#draft)
# that Static.update() redraws each token into. A finished line never
# goes into #chatlog directly though: ChatScreen rebuilds the whole
# thing from messages on every on_change, since a tool call's own
# summary row can flip between collapsed/expanded well after it was
# first written, and RichLog can't redraw one row in place.
class AIState:
    def __init__(self):
        self.messages = []
        self.errors = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.busy = False
        self.context_max = _UNSET
        # Set by 'ask()', read by 'ChatScreen' 's own ticking "working"
        # indicator to say how long the current turn has been running --
        # not reset when the turn ends, so it just stops being read.
        self.turn_started_at = None
        # The file this conversation is written to (below) -- 'None'
        # until the first message actually needs one, assigned once and
        # kept for the rest of this conversation. Cleared by 'reset()',
        # so the next one starts a file of its own rather than
        # continuing to overwrite what came before it.
        self.chat_file = None
        self.chat_started = None
        # Set by 'ChatScreen.on_mount()'/cleared by 'on_unmount()' --
        # the same "redraw, if anyone is looking" indirection
        # 'FilesystemState.on_change' already uses, so this module
        # never imports a screen that would import it back.
        self.on_change = None      # () -> None: 'messages'/'errors' changed
        self.on_delta = None       # (text) -> None: one more fragment
        self.on_delta_done = None  # () -> None: the streaming reply is over
        self.on_stats = None       # () -> None: token counters/context changed
        # --- Interaction-socket integration ---
        self._last_sent_index = -1  # index of last assistant message sent via socket
        self.app = None  # back-reference to SeineApp, set by SeineApp.__init__

    def reset(self):
        self.messages = []
        self.errors = []
        self.chat_file = None
        self.chat_started = None
        self.prompt_tokens = 0
        self.completion_tokens = 0
        # 'context_max' is not reset -- it is a property of the server
        # this conversation is talking to, not of the conversation
        # itself, and re-asking it on every 'reset-conversation' would
        # be a network round-trip nothing needs.
        self._notify(self.on_stats)
        self.changed()

    # 'on_*' callbacks are bound ChatScreen methods, normally paired
    # with mount/unmount. notify_build_finished() can fire mid-transition,
    # hitting a torn-down widget -- caught as "nothing to redraw", since
    # on_mount() always rebuilds fresh on the next real mount.
    def _notify(self, callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except NoMatches:
            pass

    # The conversation's own size right now, were it sent as the next
    # request -- local, no network call ('litellm.token_counter()'),
    # read by '#stats' 's context-fill bar against 'context_max' once
    # that lookup (below) has actually run.
    def used_tokens(self, model):
        import litellm
        try:
            return litellm.token_counter(model=model, messages=self.messages)
        except Exception:
            return None

    def set_context_max(self, value):
        self.context_max = value
        self._notify(self.on_stats)

    # Fired after messages or errors changed. ChatScreen rebuilds
    # #chatlog whole every time rather than tracking deltas -- cheap at
    # this size, and the only way a tool call's row can flip between
    # collapsed/expanded after the fact.
    def changed(self):
        self._persist()
        self._notify(self.on_change)
        # Notify the UI to emit any new assistant messages over the socket.
        if getattr(self, "app", None) is not None:
            self.app._socket_send_ai_messages()

    # Finished assistant replies added since the last _mark_sent() --
    # read by SeineApp._socket_send_ai_messages() after every changed().
    def _new_assistant_messages(self):
        start = self._last_sent_index + 1
        return [m for m in self.messages[start:] if m.get("role") == "assistant"]

    def _mark_sent(self):
        self._last_sent_index = len(self.messages) - 1


    # One JSON file per conversation, rewritten whole on every change,
    # same atomic-write shape as settings.save(), under
    # ContainerEngine.chats(). Nothing written with no question asked
    # yet. Kept purely local -- read back by a person, never sent anywhere.
    def _persist(self):
        if not self.messages:
            return
        import datetime
        import json
        import time
        from seine.container import ContainerEngine
        if self.chat_file is None:
            chats = ContainerEngine.chats()
            os.makedirs(chats, exist_ok=True)
            # Microseconds, not just seconds -- two conversations
            # started the same second (readily hit by a fast test, or
            # scripted use) must not collide and silently overwrite
            # each other.
            stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f")
            self.chat_file = os.path.join(chats, "%s.json" % stamp)
            self.chat_started = time.time()
        record = {"started": self.chat_started, "model": _resolved()[0],
                 "messages": self.messages, "errors": self.errors}
        temporary = "%s.new" % self.chat_file
        with open(temporary, "w") as f:
            json.dump(record, f, indent=1)
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.chat_file)

    def delta(self, text):
        self._notify(self.on_delta, text)

    def delta_done(self):
        self._notify(self.on_delta_done)

    def add_usage(self, prompt, completion):
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self._notify(self.on_stats)

# Asked from any screen's prompt once configured() is true -- switches
# to ChatScreen right away, then runs the call in a worker thread
# (exclusive/group, same shape as build.py's, so a second question
# mid-answer replaces the first rather than running both at once).
def ask(app, question):
    if app.ai_state.busy:
        app.say("still waiting on the last answer", error=True)
        return
    app.ai_state.busy = True
    app.ai_state.turn_started_at = time.time()
    app.ai_state.messages.append({"role": "user", "content": question})
    app.ai_state.changed()
    app.show("chat")
    app.run_worker(lambda: _run(app), thread=True, exclusive=True, group="ai")

# Fired from App._build_finished() when the finished build is the one
# 'start-build' itself started (BuildState.notify_ai) -- same shape as
# ask(), triggered by the build's outcome. Screen switch is already
# decided by the caller; this only appends the turn. Skipped if a turn
# is already in flight (rare); _live_status() covers it passively next turn.
def notify_build_finished(app):
    if app.ai_state.busy:
        return
    outcome = "failed" if app.build_state.error else "finished successfully"
    app.ai_state.busy = True
    app.ai_state.turn_started_at = time.time()
    app.ai_state.messages.append({
        "role": "user",
        "content": "(seine) The build you started with start-build has "
                   "%s. Check it and report back." % outcome})
    app.ai_state.changed()
    # app.say() has no mount/unmount pairing (unlike _notify() above) --
    # it just queries whatever screen is current, which can be mid-
    # transition here. A lost status line is fine; it's a courtesy, not
    # something the turn below depends on.
    try:
        app.say("AI chat: build %s -- checking in" % outcome)
    except NoMatches:
        pass
    app.run_worker(lambda: _run(app), thread=True, exclusive=True, group="ai")

# 'vendor_state''s own twin of notify_build_finished() above -- same
# one-shot, "only a run start-vendor itself started" wiring, fired from
# App._vendor_finished() the way notify_build_finished() is from
# _build_finished().
def notify_vendor_finished(app):
    if app.ai_state.busy:
        return
    outcome = "failed" if app.vendor_state.error else "finished successfully"
    app.ai_state.busy = True
    app.ai_state.turn_started_at = time.time()
    app.ai_state.messages.append({
        "role": "user",
        "content": "(seine) The vendor run you started with start-vendor "
                   "has %s. Check it and report back." % outcome})
    app.ai_state.changed()
    try:
        app.say("AI chat: vendor %s -- checking in" % outcome)
    except NoMatches:
        pass
    app.run_worker(lambda: _run(app), thread=True, exclusive=True, group="ai")

# Appended to the system prompt fresh every turn (in _run()'s loop, not
# injected into state.messages, so it never appears in the chat pane) --
# a build/vendor started or finished mid-conversation is state the model
# has no other way to notice. Overall state only, not a per-step
# breakdown: build-status/'vendor' already cover that on demand.
def _state_overall(state):
    if state.running:
        return "running"
    if state.done and state.error:
        return "failed"
    if state.done:
        return "finished"
    return "not started yet"

def _live_status(app):
    parts = []
    build = app.build_state
    if len(build.order) > 0:
        parts.append(
            "The build this TUI session itself has run is currently: "
            "%s. This is live, not something said earlier in the "
            "conversation -- trust it over your own prior turns, and call "
            "build-status for the per-step picture if that's what's "
            "actually asked." % _state_overall(build))
    vendor = app.vendor_state
    if len(vendor.order) > 0:
        parts.append(
            "The vendor run this TUI session itself has started is "
            "currently: %s. Live, the same as the build status above -- "
            "call 'vendor' for what it actually resolved/fetched once "
            "it's done." % _state_overall(vendor))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""

# Reasoning models (seen live: Qwen3) stream 'delta.reasoning_content'
# separately from 'delta.content' -- only the latter is ever shown,
# same as a person reading the TUI would only ever see the final
# answer, not a model's own internal monologue on the way there.
def _run(app):
    import litellm
    # An unrecognised model name makes litellm print a warning straight
    # to stderr -- inside the TUI's alternate screen buffer that's
    # corruption, not a readable log line.
    litellm.suppress_debug_info = True
    model, api_base, api_key = _resolved()
    state = app.ai_state
    if state.context_max is _UNSET:
        app.call_from_thread(state.set_context_max, _lookup_context_max(model, api_base, api_key))
    try:
        while True:
            full = [{"role": "system", "content": _system_prompt() + _live_status(app)}] + state.messages
            # 'timeout' is a per-read stall bound (httpx measures it from
            # each socket read, not from request start), not a total-
            # reply budget -- a live stream keeps resetting it. Without
            # it, a backend that stops sending mid-stream (seen live:
            # Ollama Cloud dropping a request server-side without
            # closing the connection) hangs this whole thread, and with
            # it #draft/#chatcol's spinner, forever -- caught below as
            # just another 'except Exception', same as any other
            # request failure.
            stream = litellm.completion(model=model, api_base=api_base, api_key=api_key,
                                        messages=full, tools=TOOL_SCHEMAS,
                                        tool_choice="auto", stream=True,
                                        stream_options={"include_usage": True},
                                        timeout=120)
            chunks = []
            streaming = False
            for chunk in stream:
                chunks.append(chunk)
                delta = chunk.choices[0].delta
                if delta.content:
                    streaming = True
                    app.call_from_thread(state.delta, delta.content)
            if streaming:
                app.call_from_thread(state.delta_done)
            response = litellm.stream_chunk_builder(chunks, messages=full)
            message = response.choices[0].message
            if response.usage is not None:
                app.call_from_thread(state.add_usage, response.usage.prompt_tokens,
                                     response.usage.completion_tokens)
            state.messages.append(message.model_dump(exclude_none=True))
            app.call_from_thread(state.changed)
            if not message.tool_calls:
                break
            # A tool call's row in #chatlog starts collapsed; recorded in
            # state.messages either way, so the model gets the real
            # result regardless of what a person has expanded.
            for call in message.tool_calls:
                result = _dispatch(app, call)
                state.messages.append({"role": "tool", "tool_call_id": call.id,
                                       "content": result})
                app.call_from_thread(state.changed)
    except Exception as e:
        state.errors.append(str(e))
        app.call_from_thread(state.changed)
    finally:
        state.busy = False
        # The one true "turn is done" signal -- every message/tool-call
        # round trip within the turn has already run by the time this
        # fires, gated tool calls included.
        app._socket_send({"type": "ai_turn_finished"})
