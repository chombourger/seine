# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional AI chat: a LiteLLM tool-calling loop over the same read-only
# render layer every screen uses, plus write tools (start-build/
# cancel-build/spec-update/spec-create) gated by ConfirmAction. Off
# unless llm_model is set.
#
# This file holds the shared infrastructure (settings, Preview/Plan/
# Tool, the registry, ConfirmAction/confirm/_dispatch, AIState, the chat
# loop) plus a few tools with no better home. Other tools live in
# tools_<domain>.py or web.py.

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

# 'app' is not always a real SeineApp (tests use a stand-in with no
# '_socket_send' at all), so check before calling.
def _socket_send(app, event):
    send = getattr(app, "_socket_send", None)
    if send is not None:
        send(event)

# SEINE_LLM_MODEL/SEINE_LLM_API_BASE override settings.json.
# SEINE_LLM_API_KEY has no settings.json field -- env only.
def _resolved():
    current = settings.load()
    model = os.environ.get("SEINE_LLM_MODEL") or current["llm_model"]
    api_base = os.environ.get("SEINE_LLM_API_BASE") or current["llm_api_base"]
    api_key = os.environ.get("SEINE_LLM_API_KEY")
    return model, api_base, api_key

def configured():
    model, _, _ = _resolved()
    return bool(model)

# Distinguishes "not looked up yet" from "looked up, unknown" (None).
# Looked up once per AIState, not once per question.
_UNSET = object()

# Tries the server's /models listing first, then litellm's static
# database. Neither working is not an error -- None just means "show
# the raw count, no bar against a guessed ceiling".
def _lookup_context_max(model, api_base, api_key):
    if api_base:
        try:
            import urllib.request
            request = urllib.request.Request(api_base.rstrip("/") + "/models")
            if api_key:
                request.add_header("Authorization", "Bearer %s" % api_key)
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read())
            wanted = model.split("/", 1)[-1]  # strip litellm's 'openai/<name>' prefix
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

# Plain text file, read fresh each call -- editing the wording needs no
# code change and no restart.
SYSTEM_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "data", "system_prompt.txt")

# Everything above the '\n---\n' marker is editing guidance for humans,
# not sent to the model. No marker: send the whole file.
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

# Wraps one render_*(), same text its screen shows. str()'d since
# render_doctor() returns a styled rich.Text, not a plain str.
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

# Installed docs (seine/data/docs/) checked first; a checkout without a
# build falls back to the repo root's own docs/.
def _docs_dir():
    installed = os.path.join(os.path.dirname(SYSTEM_PROMPT_FILE), "docs")
    if os.path.isdir(installed):
        return installed
    checkout = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
        "docs")
    return checkout if os.path.isdir(checkout) else None

# Sibling of SYSTEM_PROMPT_FILE, shipped as package_data the same way --
# checked defensively in case of a broken install.
PROMPT_DOCS_DIR = os.path.join(os.path.dirname(SYSTEM_PROMPT_FILE), "prompt")

# One tool, two directories: the prompt's own cluster files (always
# there) and the project's docs/*.md (only with a checkout or full
# build). Different extensions so a name never means two files at once.
def _doc_sources():
    sources = []
    if os.path.isdir(PROMPT_DOCS_DIR):
        sources.append((PROMPT_DOCS_DIR, ".txt"))
    docs_dir = _docs_dir()
    if docs_dir:
        sources.append((docs_dir, ".md"))
    return sources

# A gated tool's own preview: ok=False refuses before ConfirmAction ever
# opens; ok=True carries a redacted diff to show for review.
class Preview(NamedTuple):
    ok: bool
    message: str

# The edit a spec-update call would make, shared by its preview and its
# run so the two can never drift apart.
class Plan(NamedTuple):
    ok: bool
    message: str        # the error (not ok), or a redacted diff (ok)
    path: str = None    # real on-disk path to write -- only when ok
    new_text: str = None  # the whole file's new content -- only when ok

# A unified diff, redacted since it reaches the remote model -- the
# real (unredacted) text is what gets written on approval. 'spec' is
# None when there's nothing to redact against (e.g. a gist).
def _redacted_diff(spec, old_text, new_text, from_path, to_path):
    patterns = redactions(spec)
    diff = difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                fromfile=from_path, tofile=to_path, lineterm="")
    return "\n".join(redact(line, patterns) for line in diff)

# ruamel.yaml dumps at its own fixed indent regardless of the source
# file's -- without this a small edit reflows the whole file. Detected
# from the file's first sequence/mapping and reused for the whole
# document (not perfect: YAML().indent() has no per-node granularity).
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

# How this actually runs (container, HostBootstrap, bind mount) is
# sources.bash()'s business, not a contract the model needs to know.
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
    # (app, arguments: dict) -> Preview; None falls back to confirm()'s
    # default (description plus a dump of 'arguments').
    preview: object = None

# Must come after the names above -- each submodule imports back into
# this still-loading package.
from . import tools_build, tools_gist, tools_source, tools_spec, tools_target, tools_test
from . import web as web_tools

# Re-exported so 'self.ai.AUDIT_LOG_MAX_ROWS' etc still works as before
# the split into submodules.
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

# Built with .append(literal, style=...) rather than a markup string,
# so a literal '[' in a diff can't spoof or break the dialog. +++/---
# checked before bare +/-, since a header line starts with one too.
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
        # A redacted diff, or None for a gated tool with no preview
        # (start-build/cancel-build) -- picks the layout compose() uses.
        self.preview = preview
        self.on_result = on_result

    # No preview: plain 'key: value' lines (not markup -- values are
    # arbitrary model text). With preview: a 'file:' line plus the
    # coloured diff, in its own scrollable region.
    def compose(self):
        with Vertical(id="confirmpane"):
            yield Static("seine wants to run: %s" % self.tool.name, id="confirmtitle")
            yield Static(self.tool.description, id="confirmdesc")
            if self.preview is not None:
                # 'path': a file about to be written. 'fragment': a
                # file about to be loaded/unloaded, nothing written.
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

# Blocks the worker thread until a person answers. Polled instead of a
# bare event.wait() so quitting mid-approval doesn't block forever --
# cancellation counts as a denial. Public: '/target' calls this too.
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
# did or was refused, separate from the chat transcript. Capped since
# this shouldn't duplicate a multi-KB task log.
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
        # A bad call is refused here, before confirm()'s modal opens --
        # that's for reviewing a real change, not rejecting a broken
        # request. Not audited: nothing was ever on the table.
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

# ChatScreen rebuilds #chatlog whole from messages on every on_change
# (a tool-call row can flip collapsed/expanded well after being
# written, and RichLog can't redraw one row in place) -- only a
# still-streaming reply lands directly, in the #draft Static.
class AIState:
    def __init__(self):
        self.messages = []
        self.errors = []
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.busy = False
        self.context_max = _UNSET
        # Set by ask(); read by ChatScreen's ticking "working" indicator.
        # Not reset when the turn ends -- it just stops being read.
        self.turn_started_at = None
        # None until the first message needs a chat file, then kept for
        # the rest of the conversation. Cleared by reset().
        self.chat_file = None
        self.chat_started = None
        # Set/cleared by ChatScreen's mount/unmount -- "redraw if anyone
        # is looking", so this module never imports a screen back.
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
        # Not reset -- a property of the server, not the conversation;
        # re-asking on every reset would be a needless round trip.
        self._notify(self.on_stats)
        self.changed()

    # 'on_*' callbacks are bound ChatScreen methods. notify_build_finished()
    # can fire mid-transition, hitting a torn-down widget -- caught here
    # as nothing to redraw.
    def _notify(self, callback, *args):
        if callback is None:
            return
        try:
            callback(*args)
        except NoMatches:
            pass

    # Local count, no network call -- read by #stats' context-fill bar
    # against context_max once that lookup has run.
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
    # under ContainerEngine.chats(). Nothing written until a question is
    # asked. Kept purely local, never sent anywhere.
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
            # Microseconds so two conversations started the same second
            # don't collide and overwrite each other.
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

# Asked from any screen's prompt once configured() is true. Runs on an
# exclusive worker so a second question mid-answer replaces the first.
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

# Fired from App._build_finished() when start-build (BuildState.notify_ai)
# started the finished build. Skipped if a turn is already in flight --
# _live_status() covers it passively next turn.
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
    # A lost status line here is fine -- a courtesy, not load-bearing.
    try:
        app.say("AI chat: build %s -- checking in" % outcome)
    except NoMatches:
        pass
    app.run_worker(lambda: _run(app), thread=True, exclusive=True, group="ai")

# notify_build_finished()'s twin for vendor runs.
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

# Appended to the system prompt each turn, never into state.messages
# (so it never appears in the chat pane) -- the model has no other way
# to notice a build/vendor starting or finishing mid-conversation.
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

# Reasoning models stream 'delta.reasoning_content' separately from
# 'delta.content' -- only the latter is shown, never the model's
# internal monologue.
def _run(app):
    import litellm
    # An unrecognised model name makes litellm print a warning to stderr
    # -- inside the TUI's alternate screen that's corruption.
    litellm.suppress_debug_info = True
    model, api_base, api_key = _resolved()
    state = app.ai_state
    if state.context_max is _UNSET:
        app.call_from_thread(state.set_context_max, _lookup_context_max(model, api_base, api_key))
    try:
        while True:
            full = [{"role": "system", "content": _system_prompt() + _live_status(app)}] + state.messages
            # 'timeout' is a per-read stall bound, reset by each chunk of
            # a live stream -- without it a backend that stops sending
            # mid-stream hangs this thread (and the spinner) forever.
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
            # A tool call's #chatlog row starts collapsed, but the real
            # result always goes into state.messages regardless.
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
        # The one true "turn is done" signal, fired after every
        # message/tool-call round trip in the turn has run.
        app._socket_send({"type": "ai_turn_finished"})
