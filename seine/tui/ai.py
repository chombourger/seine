# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional AI chat: a LiteLLM tool-calling loop over the same read-only
# render layer every screen uses, plus tools that write (start-build/
# cancel-build/spec-update/spec-create), each gated by ConfirmAction.
# Off unless llm_model is set (configured() below).
#
# litellm/ruamel.yaml are the only new dependencies, scoped to this
# module alone (setup.py's 'ai' extra). ruamel.yaml specifically for
# spec-update: it round-trips comments/formatting that a plain PyYAML
# parse-then-dump would silently drop -- see _spec_update_plan() below.

import difflib
import glob
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
from seine.utils import redact, redactions

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
# change either way. Read fresh each call ('seine/kernel.py's own
# 'KERNEL_RULES' follows the same "a path constant, opened by whoever
# needs it" shape), not cached at import -- a person iterating on the
# wording sees the next question pick it up without restarting.
SYSTEM_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "system_prompt.txt")

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
# reading that screen already sees, nothing computed twice.
def _render_tool(name, with_context=True):
    def run(app, arguments):
        from seine.tui import render
        fn = getattr(render, name)
        return fn(app.context) if with_context else fn()
    return run

_tool_overview = _render_tool("render_overview")
_tool_plan = _render_tool("render_plan")
_tool_packages = _render_tool("render_packages")
_tool_analyze = _render_tool("render_analyze")
_tool_artifacts = _render_tool("render_artifacts")
# 'matching' (a regex) is the AI-tool equivalent of 'seine cache info
# --entries-matching' -- not run through _render_tool() like the other
# with_context=False tools, since that always calls its render_*()
# with no arguments at all.
def _tool_cache(app, arguments):
    from seine.tui import render
    pattern = arguments.get("matching")
    if pattern:
        try:
            re.compile(pattern)
        except re.error as e:
            return "'%s' is not a usable pattern: %s" % (pattern, e)
    return render.render_cache(matching=pattern)
_tool_doctor = _render_tool("render_doctor", with_context=False)

# The build this TUI session itself started, not any spec's own history
# (analyze covers that) -- the same text BuildScreen's stage list renders.
def _tool_build_status(app, arguments):
    return app.build_state.render()

# One step's log tail, same file BuildScreen's log pane tails. No 'task'
# picks whichever step is failed first, the running one otherwise.
LOG_TAIL_LINES = 200

# 'pattern' filters server-side rather than handing the whole log back.
# 'task' omitted alongside 'pattern' searches every step's log in build
# order, one line each prefixed with which step. Capped like spec-query.
LOG_GREP_MAX_MATCHES = 50

def _tool_task_log(app, arguments):
    state = app.build_state
    if state.logs is None:
        return "no build has written logs yet this session"
    task = arguments.get("task")
    pattern = arguments.get("pattern")
    if pattern:
        import re
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return "'%s' is not a usable pattern: %s" % (pattern, e)
        names = [task] if task else state.order
        lines = []
        truncated = False
        for name in names:
            path = os.path.join(state.logs, "%s.log" % name)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                # A named step with no log yet is worth saying so; one
                # among many during an all-steps search is just skipped.
                if task:
                    return "no log for '%s' yet" % task
                continue
            prefix = "" if task else "%s: " % name
            for line in data.decode("utf-8", "replace").splitlines():
                if regex.search(line):
                    if len(lines) >= LOG_GREP_MAX_MATCHES:
                        truncated = True
                        break
                    lines.append("%s%s" % (prefix, line))
            if truncated:
                break
        if not lines:
            return "no matching lines"
        text = "\n".join(lines)
        if truncated:
            text += ("\n... (more matches -- narrow the pattern, or give "
                     "'task' to search one step)")
        return text
    if not task:
        failed = [name for name, row in state.rows.items() if row["state"] == "failed"]
        task = failed[0] if failed else state.current
    if not task:
        return ("no 'task' given, and none is currently failed or running -- "
                "name one (call build-status first to see what ran), or "
                "give 'pattern' to search every step's log at once")
    path = os.path.join(state.logs, "%s.log" % task)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return "could not read %s: %s" % (path, e)
    lines = data.decode("utf-8", "replace").splitlines()
    return "\n".join(lines[-LOG_TAIL_LINES:])

def _tool_sbom_diff(app, arguments):
    old = arguments.get("old")
    new = arguments.get("new")
    if not old or not new:
        return "sbom-diff needs both 'old' and 'new' SBOM file paths"
    from seine.sbom_diff import diff_files
    try:
        return diff_files(old, new)
    except (OSError, ValueError) as e:
        return "error: %s" % e

# 'name' filters over the full list from dpkg's own status file -- every
# package actually installed, not just the default's largest-30. The
# reliable way to answer "is package X in my image": a build log names
# the task an apt module ran under, not the packages it installed, and
# the spec only says what's declared, not what a dependency pulled in.
def _tool_installed_sizes(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    tarball = build.image._tarball
    if not tarball:
        return "no tarball for this build yet -- '/build' it first"
    from seine import sbom
    sizes = sbom.installed_sizes(tarball)
    if len(sizes) == 0:
        return ("no package size data available -- the tarball may "
                "already be cleaned up (it is not kept after a build "
                "finishes unless '--keep' was used)")
    name = arguments.get("name")
    if name:
        import re
        try:
            regex = re.compile(name, re.IGNORECASE)
        except re.error as e:
            return "'%s' is not a usable pattern: %s" % (name, e)
        matches = [(pkg, kib) for pkg, kib in sizes if regex.search(pkg)]
        if not matches:
            return "no installed package matching '%s'" % name
        return "\n".join("%-40s %8d KiB" % (pkg, kib) for pkg, kib in matches)
    lines = ["%-40s %8d KiB" % (pkg, kib) for pkg, kib in sizes[:30]]
    return "\n".join(lines)

# The three below read a build's own loaded files back via BuildCmd.
# loaded_files/dump_file() (seine/build.py, general capability, nothing
# AI-specific) -- an arbitrary path is refused there, not here.
#
# Every *.yml/*.yaml in a directory a loaded file lives in, or -- when
# loaded files span two or more directories -- in a sibling directory
# under their common ancestor (e.g. examples/linux-6.18 next to
# examples/common). A single loaded directory never climbs to its
# parent, so a lone spec file doesn't sweep unrelated directories. Non-recursive.
def _sibling_files(build):
    directories = {os.path.dirname(f) for f in build.loaded_files}
    search_dirs = set(directories)
    if len(directories) > 1:
        ancestor = os.path.commonpath(sorted(directories))
        try:
            entries = os.listdir(ancestor)
        except OSError:
            entries = []
        for entry in entries:
            full = os.path.join(ancestor, entry)
            if os.path.isdir(full):
                search_dirs.add(full)
    loaded = set(build.loaded_files)
    found = set()
    for directory in search_dirs:
        for pattern in ("*.yml", "*.yaml"):
            for path in glob.glob(os.path.join(directory, pattern)):
                real = os.path.realpath(path)
                if real not in loaded:
                    found.add(real)
    return sorted(found)

def _tool_spec_files(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    if not build.loaded_files:
        return "no files recorded for this build"
    text = "\n".join(build.loaded_files)
    siblings = _sibling_files(build)
    if siblings:
        # spec-update still only accepts a loaded path; read/spec-query
        # (given an explicit path) accept either section -- but this
        # stays a requires: hint, not a load. A path-less spec-query
        # still only walks loaded_files, so it won't widen to files
        # nothing requires:.
        text += ("\n\nnot loaded (same or a cousin directory, not part "
                "of this build -- a candidate to 'requires:' in rather "
                "than duplicate with spec-create, and readable with "
                "read/spec-query):\n" + "\n".join(siblings))
    return text

def _tool_read(app, arguments):
    path = arguments.get("path")
    if not path:
        return ("read needs a 'path' argument -- a file from spec-files, "
                "or one a spec entry's own content already named (e.g. a "
                "package's 'patches:' entry)")
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    try:
        return build.read(path, extra_allowed=_sibling_files(build))
    except ValueError as e:
        return str(e)

# The merged specification, exactly what 'seine build -D'/'--dump' prints
# -- every loaded file combined, 'extends'/'defaults' resolved, secrets
# redacted -- as opposed to read's one file in isolation. Answers
# "did two entries merge into one, or land side by side" without
# inferring it from 'plan' 's step list.
SPEC_DUMP_CHUNK_LINES = 300

# Shared by spec-dump and docs: 'lines' clamped to a 'start'/'end'
# range no wider than 'chunk_lines', regardless of what was asked for
# -- one reply can't become an unbounded wall of text. 'noun' names
# what's being paged, for the "past the end" message.
def _text_chunk(lines, start, end, chunk_lines, noun):
    total = len(lines)
    start = max(start, 1)
    end = min(end, start + chunk_lines - 1, total)
    if start > total:
        return "%s is %d lines -- 'start' is past the end" % (noun, total)
    header = "lines %d-%d of %d" % (start, end, total)
    if end < total:
        header += " -- call again with 'start': %d for more" % (end + 1)
    return header + "\n\n" + "\n".join(lines[start - 1:end])

def _tool_spec_dump(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    lines = build.dump(build.spec).splitlines()
    try:
        start = int(arguments["start"]) if arguments.get("start") else 1
        end = int(arguments["end"]) if arguments.get("end") else start + SPEC_DUMP_CHUNK_LINES - 1
    except (TypeError, ValueError):
        return "'start'/'end' must be line numbers (1-indexed)"
    return _text_chunk(lines, start, end, SPEC_DUMP_CHUNK_LINES, "the merged spec")

# Installed (seine/data/docs/, generated at build time) is checked
# first; a checkout or editable install (never build_py'd) falls back
# to the repo root's own docs/. Read fresh each call, not cached --
# same spirit as SYSTEM_PROMPT_FILE's own lookup.
def _docs_dir():
    installed = os.path.join(os.path.dirname(SYSTEM_PROMPT_FILE), "docs")
    if os.path.isdir(installed):
        return installed
    checkout = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "docs")
    return checkout if os.path.isdir(checkout) else None

def _tool_docs(app, arguments):
    docs_dir = _docs_dir()
    if docs_dir is None:
        return ("no documentation available in this install of seine -- "
                "docs/ ships only with a source checkout or a build that "
                "included it")
    name = arguments.get("name")
    if not name:
        available = sorted(f for f in os.listdir(docs_dir) if f.endswith(".md"))
        return "give 'name', one of: " + ", ".join(available)
    real = os.path.realpath(os.path.join(docs_dir, name))
    if (os.path.dirname(real) != os.path.realpath(docs_dir)
            or not real.endswith(".md") or not os.path.isfile(real)):
        return "%s is not one of this seine's own docs/*.md files" % name
    with open(real) as f:
        lines = f.read().splitlines()
    try:
        start = int(arguments["start"]) if arguments.get("start") else 1
        end = int(arguments["end"]) if arguments.get("end") else start + SPEC_DUMP_CHUNK_LINES - 1
    except (TypeError, ValueError):
        return "'start'/'end' must be line numbers (1-indexed)"
    return _text_chunk(lines, start, end, SPEC_DUMP_CHUNK_LINES, name)

# Capped the same way task-log caps a log tail -- a wide-open expression
# across every loaded file could otherwise return an unbounded wall of text.
SPEC_QUERY_MAX_MATCHES = 50

# A JSONPath expression evaluated against one loaded file's own parsed,
# redacted tree -- or, 'path' omitted, every loaded file in turn, each
# match prefixed with which one. YAML parses to the same shape JSON
# does, so JSONPath works unmodified.
def _tool_spec_query(app, arguments):
    path = arguments.get("path")
    expression = arguments.get("expression")
    if not expression:
        return ("spec-query needs 'expression' (a JSONPath expression, "
                "e.g. '$..name' finds every 'name:' key anywhere) -- "
                "'path' is optional, a file from spec-files; omit it to "
                "search every loaded file at once")
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    try:
        from jsonpath_ng import parse
        expr = parse(expression)
    except Exception as e:
        return "'%s' is not a usable JSONPath expression: %s" % (expression, e)
    import yaml
    lines = []
    truncated = False
    extra_allowed = _sibling_files(build) if path else ()
    for one in ([path] if path else build.loaded_files):
        try:
            data = yaml.safe_load(build.dump_file(one, extra_allowed=extra_allowed)) or {}
        except ValueError as e:
            # A named file that isn't actually loaded is worth saying
            # so -- one that merely failed to parse on its own during a
            # search across *every* file is skipped instead, the same
            # "don't let one bad fragment abort the whole thing" spirit
            # 'load_all()' 's own probing pass already follows.
            if path:
                return str(e)
            continue
        prefix = "" if path else "%s: " % one
        for m in expr.find(data):
            if len(lines) >= SPEC_QUERY_MAX_MATCHES:
                truncated = True
                break
            lines.append("%s%s = %s" % (prefix, m.full_path, m.value))
        if truncated:
            break
    if not lines:
        return ("no matches -- JSONPath here matches keys/paths, not "
                 "values, so a query built from the value itself (e.g. "
                 "'$..vim') always comes back empty. Searching for where a "
                 "package comes from: query the task modules that install "
                 "it -- '$..apt' or '$..package' -- and scan their 'name' "
                 "lists yourself; '$..packages' is source rebuilds only, "
                 "not ordinary installs")
    text = "\n".join(lines)
    if truncated:
        text += ("\n... (more matches -- narrow the expression, or give "
                 "'path' to search one file)")
    return text

def _tool_reset_conversation(app, arguments):
    app.ai_state.reset()
    return "conversation reset"

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

# Structural, not textual: 'at' is the same JSONPath 'spec-query' hands
# back, so locating a node survives read/query's redacted, re-
# serialized view not matching the real bytes on disk (PyYAML's
# safe_load+dump drops comments, sorts keys, reflows indentation).
# ruamel.yaml's round-trip mode is read and written instead, so a
# one-node edit stays a one-node diff. Refuses on anything ambiguous:
# 'at' must resolve to exactly one node, and a no-op edit is refused.
def _spec_update_plan(app, arguments):
    path = arguments.get("path")
    at = arguments.get("at")
    value = arguments.get("value")
    if not path or not at or value is None:
        return Plan(False, "spec-update needs 'path', 'at' (a JSONPath "
                    "naming exactly one node -- reuse what spec-query "
                    "already returned), and 'value' (YAML for the "
                    "replacement)")
    build = _single_group(app)
    if build is None:
        return Plan(False, NO_SINGLE_GROUP)
    real = os.path.realpath(path)
    if real not in build.loaded_files:
        return Plan(False, "%s is not one of this build's own loaded files" % path)
    try:
        with open(real, "r") as f:
            old_text = f.read()
    except OSError as e:
        return Plan(False, "could not read %s: %s" % (path, e))

    from ruamel.yaml import YAML
    import io
    rt = YAML()
    rt.preserve_quotes = True
    indent = _detect_indent(old_text)
    if indent:
        rt.indent(**indent)
    try:
        data = rt.load(old_text)
    except Exception as e:
        return Plan(False, "%s: %s" % (path, e))
    if data is None:
        return Plan(False, "%s is empty -- nothing for 'at' to address" % path)

    from jsonpath_ng import parse
    try:
        expr = parse(at)
    except Exception as e:
        return Plan(False, "'%s' is not a usable JSONPath expression: %s" % (at, e))
    matches = expr.find(data)
    if not matches:
        return Plan(False, "no node matches '%s' in %s -- check it with "
                    "spec-query first" % (at, path))
    if len(matches) > 1:
        return Plan(False, "'%s' matches %d nodes in %s -- narrow it to "
                    "exactly one" % (at, len(matches), path))

    try:
        new_value = rt.load(value)
    except Exception as e:
        return Plan(False, "'value' is not valid YAML: %s" % e)

    mode = arguments.get("mode", "set")
    if mode == "append":
        target = matches[0].value
        if not isinstance(target, list):
            return Plan(False, "'at' does not address a list -- 'mode': "
                        "'append' needs one to append to")
        target.append(new_value)
    elif mode == "set":
        expr.update(data, new_value)
    else:
        return Plan(False, "'mode' must be 'set' (the default) or "
                    "'append', not '%s'" % mode)

    out = io.StringIO()
    rt.dump(data, out)
    new_text = out.getvalue()

    import yaml
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        return Plan(False, "the edited file would not parse -- not "
                    "written: %s" % e)
    if new_text == old_text:
        return Plan(False, "that change has no effect -- the file would "
                    "be unchanged")

    diff = _redacted_diff(build.spec, old_text, new_text, path, path)
    return Plan(True, diff, real, new_text)

def _tool_spec_update_preview(app, arguments):
    plan = _spec_update_plan(app, arguments)
    return Preview(plan.ok, plan.message)

# Atomic write, temp file then os.replace(). Recomputes the plan rather
# than trusting anything cached from preview above -- the file could
# have changed between the diff being shown and "Yes" being clicked.
def _tool_spec_update(app, arguments):
    plan = _spec_update_plan(app, arguments)
    if not plan.ok:
        return plan.message
    temporary = "%s.new" % plan.path
    with open(temporary, "w") as f:
        f.write(plan.new_text)
    os.replace(temporary, plan.path)
    reload_error = _reload_and_highlight(app)
    if reload_error:
        return ("updated %s, but the active spec failed to reload: %s -- "
                "the file on disk is correct, this session's own view of "
                "it is what's stale" % (arguments.get("path"), reload_error))
    return "updated %s" % arguments.get("path")

# Re-parses the active group's file list and marks what changed on the
# spec tree, the same mechanism /side-load's own highlight uses. Not
# called by spec-create: a brand new file isn't in context.groups[0] yet.
#
# context.use() only reassigns groups/builds after load_all()/parse()
# both succeed, so a reload that fails leaves the active spec untouched
# -- still worth surfacing, since the file on disk is correct either way.
def _reload_and_highlight(app):
    context = app.context
    previous = context.builds[0].spec
    groups = context.groups[0]
    error = {}
    def reload():
        try:
            context.use(groups)
        except (OSError, ValueError) as e:
            error["message"] = str(e)
            return
        context.changed_from = previous
        app.refresh_screens()
    try:
        app.call_from_thread(reload)
    except RuntimeError:
        # Not attached to a running app -- the write already succeeded,
        # only the live highlight has nothing to refresh.
        pass
    return error.get("message")

# A whole new file, not an edit -- the diff is trivially every line
# added. Confined to a directory a loaded file already lives in, the
# same "loaded_files is the only thing this instance can vouch for"
# spirit read/spec-update follow.
def _spec_create_plan(app, arguments):
    path = arguments.get("path")
    content = arguments.get("content")
    if not path or content is None:
        return Plan(False, "spec-create needs 'path' (a new file, "
                    "alongside an already-loaded one) and 'content' (the "
                    "whole file, as YAML)")
    build = _single_group(app)
    if build is None:
        return Plan(False, NO_SINGLE_GROUP)
    real = os.path.realpath(path)
    if real in build.loaded_files:
        return Plan(False, "%s already exists and is loaded -- use "
                    "spec-update to change it" % path)
    if os.path.exists(real):
        return Plan(False, "%s already exists -- spec-create is for a new "
                    "file only" % path)
    allowed = {os.path.dirname(f) for f in build.loaded_files}
    if os.path.dirname(real) not in allowed:
        return Plan(False, "%s is not next to any of this build's own "
                    "loaded files -- refusing to write somewhere "
                    "unrelated" % path)

    import yaml
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        return Plan(False, "'content' is not valid YAML: %s" % e)

    diff = _redacted_diff(build.spec, "", content, "/dev/null", path)
    return Plan(True, diff, real, content)

def _tool_spec_create_preview(app, arguments):
    plan = _spec_create_plan(app, arguments)
    return Preview(plan.ok, plan.message)

def _tool_spec_create(app, arguments):
    plan = _spec_create_plan(app, arguments)
    if not plan.ok:
        return plan.message
    temporary = "%s.new" % plan.path
    with open(temporary, "w") as f:
        f.write(plan.new_text)
    os.replace(temporary, plan.path)
    return "wrote %s" % arguments.get("path")

# 'gist-list' reads no active spec -- a gist lives outside any one
# project, under gists.default_dir() regardless of what (if anything)
# is loaded right now. Each line carries the absolute path too, so a
# side-load call right after doesn't need a follow-up lookup for it.
def _tool_gist_list(app, arguments):
    from seine import gists
    found = gists.list_gists()
    if not found:
        return "no gists yet -- %s" % gists.default_dir()
    lines = []
    for name, description in found:
        path = gists.path_for(name)
        lines.append("%s -- %s\n  %s" % (name, description or "(no description)", path))
    return "\n".join(lines)

def _tool_gist_show(app, arguments):
    from seine import gists
    name = arguments.get("name")
    if not name:
        return "gist-show needs 'name'"
    try:
        return gists.read(name)
    except (OSError, ValueError) as e:
        return "could not read: %s" % e

# gist-create's own plan, same "brand new file, diff against nothing"
# shape as spec-create's -- but writing under gists.default_dir()
# instead of beside a loaded file: a gist lives outside any one
# project, so there is no build to confine it next to, and no
# 'redact:' patterns to apply (spec=None -- see _redacted_diff()).
def _gist_create_plan(app, arguments):
    name = arguments.get("name")
    description = arguments.get("description")
    content = arguments.get("content")
    if not name or not description or content is None:
        return Plan(False, "gist-create needs 'name', 'description', and "
                    "'content' (the fragment, as YAML)")
    from seine import gists
    try:
        path = gists.path_for(name)
    except ValueError as e:
        return Plan(False, str(e))
    if os.path.exists(path):
        return Plan(False, "gist '%s' already exists" % name)

    import yaml
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        return Plan(False, "'content' is not valid YAML: %s" % e)

    body = content if content.endswith("\n") else content + "\n"
    new_text = "# %s\n%s" % (description, body)
    diff = _redacted_diff(None, "", new_text, "/dev/null", path)
    return Plan(True, diff, path, new_text)

def _tool_gist_create_preview(app, arguments):
    plan = _gist_create_plan(app, arguments)
    return Preview(plan.ok, plan.message)

# Recomputes the plan rather than trusting anything cached from preview
# -- another gist of the same name could have appeared since, same
# discipline _tool_spec_update() follows for a file. The real write
# still goes through gists.create() itself, not a copy of its logic.
def _tool_gist_create(app, arguments):
    plan = _gist_create_plan(app, arguments)
    if not plan.ok:
        return plan.message
    from seine import gists
    try:
        gists.create(arguments["name"], arguments["description"], arguments["content"])
    except ValueError as e:
        return "could not create: %s" % e
    return "created %s" % plan.path

def _tool_gist_delete_preview(app, arguments):
    from seine import gists
    name = arguments.get("name")
    if not name:
        return Preview(False, "gist-delete needs 'name'")
    try:
        content = gists.read(name)
    except (OSError, ValueError) as e:
        return Preview(False, str(e))
    path = gists.path_for(name)
    diff = _redacted_diff(None, content, "", path, "/dev/null")
    return Preview(True, diff)

def _tool_gist_delete(app, arguments):
    from seine import gists
    name = arguments.get("name")
    if not name:
        return "gist-delete needs 'name'"
    try:
        gists.delete(name)
    except (OSError, ValueError) as e:
        return "could not delete: %s" % e
    return "deleted %s" % name

# The AI-tool equivalent of /side-load FRAGMENT: loads one more fragment
# on top of the active group, in-session only. Lower stakes than
# spec-update/spec-create (no write, undone by /use again or by
# side-unload) but still gated -- it changes what a /build right after
# would actually build.
#
# The preview is a dry run, not a call to context.side_load() itself: a
# scratch BuildCmd loads the same files side_load() would and is diffed
# against the real active build, so a bad fragment is refused before
# anything touches app.context.
def _side_load_preview(app, arguments):
    fragment = arguments.get("fragment")
    if not fragment:
        return Preview(False, "side-load needs 'fragment' (a spec file to "
                       "load on top of the active one, same as "
                       "'/side-load')")
    context = app.context
    if not context.active:
        return Preview(False, "no active specification -- '/use SPEC' first")
    if len(context.builds) != 1:
        return Preview(False, "side-load needs exactly one active group -- "
                       "multi-group specifications ('/use a -- b') aren't "
                       "supported here yet")
    from seine.build import BuildCmd, diff
    scratch = BuildCmd()
    scratch.options = dict(scratch.options, ansible_library=[])
    try:
        scratch.load_all(context.groups[0] + [fragment])
        scratch.parse()
    except (OSError, ValueError) as e:
        return Preview(False, str(e))
    active = context.builds[0]
    before = active.dump(active.spec)
    after = scratch.dump(scratch.spec)
    changes = diff(before, after, color=False)
    # diff() always returns the whole spec, never blank -- check for an
    # actual +/- mark, not just truthiness of the text.
    if not any(line[:1] in ("+", "-") for line in changes.splitlines()):
        return Preview(False, "loading %s would change nothing" % fragment)
    return Preview(True, changes)

# Textual widgets are only touched from the UI thread -- this tool runs
# from ask()'s worker thread, so it crosses back via call_from_thread,
# same as start-build. The slash command's own _side_load() never needs
# this: it already runs on the UI thread.
def _tool_side_load(app, arguments):
    fragment = arguments.get("fragment")
    if not fragment:
        return "side-load needs 'fragment' (a spec file to load on top of the active one)"
    context = app.context
    before = None
    if context.active and len(context.builds) == 1:
        active = context.builds[0]
        before = active.dump(active.spec)
    try:
        app.call_from_thread(context.side_load, fragment)
    except (OSError, ValueError) as e:
        return "could not side-load: %s" % e
    app.call_from_thread(app.refresh_screens)
    after = context.builds[0]
    from seine.build import diff
    changes = diff(before, after.dump(after.spec), color=False)
    return "side-loaded %s\n\n%s" % (fragment, changes)

# The reverse of side-load: a scratch BuildCmd loads the active group's
# file list *without* 'fragment', diffed the same way, so a name that
# isn't currently loaded (or would leave the group empty) is refused
# before anything touches app.context.
def _side_unload_preview(app, arguments):
    fragment = arguments.get("fragment")
    if not fragment:
        return Preview(False, "side-unload needs 'fragment' (a file "
                       "currently loaded on top of the active one)")
    context = app.context
    if not context.active:
        return Preview(False, "no active specification -- '/use SPEC' first")
    if len(context.builds) != 1:
        return Preview(False, "side-unload needs exactly one active group "
                       "-- multi-group specifications ('/use a -- b') "
                       "aren't supported here yet")
    if fragment not in context.groups[0]:
        return Preview(False, "'%s' isn't currently loaded" % fragment)
    from seine.build import BuildCmd, diff
    scratch = BuildCmd()
    scratch.options = dict(scratch.options, ansible_library=[])
    remaining = [f for f in context.groups[0] if f != fragment]
    try:
        scratch.load_all(remaining)
        scratch.parse()
    except (OSError, ValueError) as e:
        return Preview(False, str(e))
    active = context.builds[0]
    before = active.dump(active.spec)
    after = scratch.dump(scratch.spec)
    changes = diff(before, after, color=False)
    return Preview(True, changes)

def _tool_side_unload(app, arguments):
    fragment = arguments.get("fragment")
    if not fragment:
        return "side-unload needs 'fragment' (a file currently loaded on top of the active one)"
    context = app.context
    before = None
    if context.active and len(context.builds) == 1:
        active = context.builds[0]
        before = active.dump(active.spec)
    try:
        app.call_from_thread(context.side_unload, fragment)
    except (OSError, ValueError) as e:
        return "could not side-unload: %s" % e
    app.call_from_thread(app.refresh_screens)
    after = context.builds[0]
    from seine.build import diff
    changes = diff(before, after.dump(after.spec), color=False)
    return "side-unloaded %s\n\n%s" % (fragment, changes)

# Marks the build as the AI chat's own -- only a build started this way
# gets the unprompted notify_build_finished() turn, never one begun by
# '/build' or the Build screen. Set right after start_build() (whose
# reset() clears it), so a race with an early finished_ok() can't miss it.
def _start_ai_build(app, build, packages_only, target):
    from seine.tui.build import start_build
    start_build(app, app.build_state, build,
                packages_only=packages_only, target=target)
    app.build_state.notify_ai = True

# Both actions below run only after ConfirmAction has already approved
# the call -- neither checks that again here.
def _tool_start_build(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    packages_only = bool(arguments.get("packages_only", False))
    target = arguments.get("target")
    try:
        # start_build() touches Indicators, so it crosses back through
        # call_from_thread, same boundary TextualReporter crosses. An
        # unknown 'target' surfaces here too, as a ValueError -- raised
        # by build.image.tasks() inside start_build()'s own reset(),
        # which runs before the worker thread does.
        app.call_from_thread(_start_ai_build, app, build, packages_only, target)
    except (RuntimeError, ValueError) as e:
        return "could not start: %s" % e
    app.call_from_thread(app.show, "build")
    if target:
        return "build started (target: %s)" % target
    return "build started (packages only)" if packages_only else "build started"

def _tool_cancel_build(app, arguments):
    if not app.build_state.running:
        return "no build is running"
    from seine import tasks
    tasks.interrupt()
    return "cancelling -- waiting for running steps to finish"

class Tool(NamedTuple):
    name: str
    description: str
    parameters: dict
    gated: bool
    run: object  # (app, arguments: dict) -> str
    # (app, arguments: dict) -> Preview, only for a gated tool whose
    # effect depends on 'arguments' -- 'None' (every gated tool before
    # 'spec-update'/'spec-create') keeps '_confirm()' 's own fallback: the
    # tool's static 'description' plus a plain dump of 'arguments'.
    preview: object = None

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
    Tool("cache", "What seine has cached, and how much of it. Each "
        "package entry names its on-disk stamp -- "
        "'<source>_<architecture>_<digest>' -- the same digest 'plan' "
        "names in its \"already built, and not built again\" list; "
        "matching the two confirms a specific build is genuinely "
        "cached rather than trusting either tool's say-so alone. "
        "'matching' (a regex) narrows the listing to entries whose key "
        "matches, and expands a surviving package entry with the "
        "specification content that stamp was actually built from -- "
        "'source'/'patches'/'extends: kernel:'/'extends: module:', the "
        "direct way to check whether a cached build really has a "
        "config option, rather than trusting the live spec still "
        "matches what was built. A file path in it (a patch, a kernel "
        "fragment) is relative to whichever file declared it, same as "
        "spec-files/spec-query show it -- look it up there before "
        "'read'-ing it, don't assume it is relative to anything else.",
        {"type": "object",
         "properties": {"matching": {"type": "string",
                                     "description": "a regex to narrow the "
                                                    "listing to, e.g. a "
                                                    "package name"}},
         "required": []},
        False, _tool_cache),
    Tool("doctor", "Whether this machine has what a build needs.",
        _no_args(), False, _tool_doctor),
    Tool("build-status", "Per-step status of the build this TUI session "
        "started (if any): pending/running/done/failed, elapsed time.",
        _no_args(), False, _tool_build_status),
    Tool("task-log", "One build step's own log. With no 'pattern', the "
        "tail of one step -- 'task' defaults to whichever step failed, or "
        "is running, if not given. With 'pattern' (a regex, case- "
        "insensitive), only matching lines come back, e.g. "
        "'warn|error' for 'were there any warnings' -- omit 'task' "
        "alongside 'pattern' to search every step's whole log at once "
        "instead of fetching each one to eyeball it yourself. Call "
        "build-status first to see the step names.",
        {"type": "object",
         "properties": {"task": {"type": "string",
                                 "description": "a step name from build-status, "
                                                "e.g. 'rootfs' or 'package:linux-image-amd64' "
                                                "-- omit to search every step when 'pattern' is given"},
                        "pattern": {"type": "string",
                                   "description": "a regex (case-insensitive) to filter "
                                                  "lines by, e.g. 'warn|error'"}},
         "required": []},
        False, _tool_task_log),
    Tool("sbom-diff", "Diff two SBOM files by package name/version.",
        {"type": "object",
         "properties": {"old": {"type": "string", "description": "path to the older SPDX file"},
                        "new": {"type": "string", "description": "path to the newer SPDX file"}},
         "required": ["old", "new"]},
        False, _tool_sbom_diff),
    Tool("installed-sizes", "The active build's installed packages -- "
        "the largest 30 by 'Installed-Size' with no 'name', or every "
        "package matching 'name' (a regex, case-insensitive, e.g. "
        "'sudo' or 'sudo|doas' or '^lib') regardless of size. Reads "
        "dpkg's own record of what is actually installed -- the "
        "reliable way to answer 'is package X in my image', unlike a "
        "build log (task names, not package names) or the spec "
        "(declared, not necessarily installed).",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a regex to match package names "
                                                "against, instead of the top 30"}},
         "required": []},
        False, _tool_installed_sizes),
    Tool("spec-files", "Every file the active specification actually "
        "loaded, in load order -- the only paths spec-update will "
        "accept. Also lists, separately, any '*.yml'/'*.yaml' sitting "
        "in the same or a cousin directory that this build does NOT "
        "load -- a candidate to pull in with 'requires:' before "
        "assuming a new file (spec-create) is needed, and also "
        "inspectable first with read or a targeted spec-query; "
        "spec-update still refuses them.",
        _no_args(), False, _tool_spec_files),
    Tool("read", "One file this build trusts, as written (not rendered, "
        "not merged with any other file) -- secrets still redacted. "
        "Two kinds: a spec file spec-files listed (loaded or an "
        "unloaded sibling), returned as YAML; or a local file one of "
        "this build's own 'packages:' entries names -- a patch, a "
        "kernel config fragment, a derived-flavour fragment -- shown "
        "as plain text once its path has actually turned up in a "
        "spec-query/spec-dump result (never guess one). Refused for "
        "anything neither of those two things.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "a file path from spec-files, "
                                                "or one a spec entry's own "
                                                "content already named"}},
         "required": ["path"]},
        False, _tool_read),
    Tool("spec-dump", "The merged specification -- every loaded file "
        "combined into one tree, 'extends'/'defaults' resolved, secrets "
        "redacted -- the same text 'seine build -D' prints. Unlike "
        "read (one file, unmerged), this shows what two entries "
        "for the same thing actually resolved to: merged into one, or "
        "left side by side. Returned in line-numbered chunks, oldest "
        "first -- omit 'start'/'end' for the first chunk, its header "
        "says how many lines there are in total and what 'start' to "
        "give for the next one.",
        {"type": "object",
         "properties": {"start": {"type": "integer",
                                  "description": "first line to return "
                                                 "(1-indexed); omit for 1"},
                        "end": {"type": "integer",
                               "description": "last line to return; omit "
                                              "for 'start' plus a few "
                                              "hundred lines"}},
         "required": []},
        False, _tool_spec_dump),
    Tool("docs", "seine's own written documentation (docs/*.md) -- "
        "'specification.md' for the full YAML schema reference, "
        "'kernels.md' for kernel rebuilds, and others. The facts in "
        "this prompt are the common cases, not the whole schema; this "
        "is where to check something they don't cover, before "
        "guessing. Chunked like spec-dump; omit 'name' to see what's "
        "available. Not present in every install of seine (a packaged "
        "one may not carry it) -- says so plainly rather than "
        "pretending to have read something it hasn't.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a docs/*.md filename; "
                                                "omit to list what's "
                                                "available"},
                        "start": {"type": "integer",
                                 "description": "first line to return "
                                                "(1-indexed); omit for 1"},
                        "end": {"type": "integer",
                               "description": "last line to return; omit "
                                              "for 'start' plus a few "
                                              "hundred lines"}},
         "required": []},
        False, _tool_docs),
    Tool("spec-query", "Search one file spec-files listed -- loaded or "
        "an unloaded sibling -- or, 'path' omitted, every loaded file "
        "at once (siblings are never swept into that blanket search, "
        "only a named 'path' reaches one) -- with a JSONPath "
        "expression. Matches keys/paths, not values -- e.g. '$..name' "
        "finds every 'name:' key anywhere, but a bare value like "
        "'$..vim' always returns no matches. For 'where does package X "
        "come from': query '$..apt' or '$..package' (the install task "
        "modules) and scan their 'name' lists for X; '$..packages' is "
        "source rebuilds only. Use before reading the whole file with "
        "read.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "a file path from spec-files "
                                                "-- omit to search every "
                                                "loaded file"},
                        "expression": {"type": "string",
                                      "description": "a JSONPath expression"}},
         "required": ["expression"]},
        False, _tool_spec_query),
    Tool("reset-conversation", "Forget everything discussed so far in "
        "this conversation.", _no_args(), False, _tool_reset_conversation),
    Tool("start-build", "Start a real build of the active specification "
        "-- the same thing '/build' does. 'target' (a task name, e.g. "
        "'deploy:linux') restricts the build to that one task and "
        "whatever it needs -- the cheap way to prove a single package "
        "(a kernel, say) actually compiles before paying for a full "
        "image build, without also rebuilding every other package. "
        "'packages_only' (default false) stops after the whole "
        "'packages:' section builds instead, without assembling a root "
        "file-system or writing an image -- use it only when several "
        "rebuilt packages need proving at once, or the exact task name "
        "isn't known yet. Give one or the other, not both.",
        {"type": "object",
         "properties": {"target": {"type": "string",
                                   "description": "a task name (e.g. "
                                                  "'deploy:linux') to "
                                                  "build, plus whatever "
                                                  "it needs"},
                        "packages_only": {"type": "boolean",
                                          "description": "stop after the "
                                                         "'packages:' section "
                                                         "builds, before rootfs/"
                                                         "image assembly"}},
         "required": []},
        True, _tool_start_build),
    Tool("cancel-build", "Cancel the running build -- the same thing "
        "'/cancel' does.", _no_args(), True, _tool_cancel_build),
    Tool("spec-update", "Change one node in a loaded spec file. 'at' is a "
        "JSONPath naming exactly one node -- reuse the expression "
        "spec-query already gave you for it, don't guess a fresh one. "
        "'value' is YAML for the replacement ('mode': 'set', the "
        "default) or one new item ('mode': 'append', only if 'at' "
        "addresses a list). A person reviews the exact diff before "
        "anything is written -- show them, in your own reply, what "
        "you're about to change and why before calling this, the same "
        "as before start-build/cancel-build.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "a file path from spec-files"},
                        "at": {"type": "string",
                              "description": "a JSONPath matching exactly one "
                                             "node, from spec-query"},
                        "value": {"type": "string",
                                 "description": "YAML for the replacement, or "
                                                "the one item to append"},
                        "mode": {"type": "string", "enum": ["set", "append"],
                                "description": "'set' (default) replaces the "
                                               "node; 'append' adds 'value' "
                                               "to it as a list"}},
         "required": ["path", "at", "value"]},
        True, _tool_spec_update, _tool_spec_update_preview),
    Tool("spec-create", "Write a brand new spec file alongside a loaded "
        "one (spec-update is for changing an existing file). Refused if "
        "the path already exists, or isn't next to a file spec-files "
        "already lists. A person reviews the whole new file (shown as a "
        "diff against nothing, i.e. every line added) before it's "
        "written -- explain what you're creating and why first, same as "
        "any other consequential action.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "a new file path, in the same "
                                                "directory as an existing "
                                                "loaded file"},
                        "content": {"type": "string",
                                   "description": "the whole file, as YAML"}},
         "required": ["path", "content"]},
        True, _tool_spec_create, _tool_spec_create_preview),
    Tool("side-load", "Load one more spec file on top of the active one, "
        "for this session only -- nothing on disk changes, 'requires:' "
        "is untouched, and it's undone with side-unload (or by picking "
        "the spec again). The natural next step right after spec-create "
        "wrote a new file: this previews what loading it would actually "
        "change in the merged spec, before anyone commits to making that "
        "permanent by adding it to a 'requires:' list. A person reviews "
        "that change before it's applied, same as any other "
        "consequential action. The call's own result already includes "
        "that diff -- no need to follow up with spec-dump/spec-query "
        "just to see what changed; reach for those only for something "
        "the diff itself doesn't answer.",
        {"type": "object",
         "properties": {"fragment": {"type": "string",
                                     "description": "a spec file to load on top "
                                                    "of the active one, e.g. one "
                                                    "spec-create just wrote"}},
         "required": ["fragment"]},
        True, _tool_side_load, _side_load_preview),
    Tool("side-unload", "Drop one side-loaded spec file back out of the "
        "active one -- the reverse of side-load, for the same "
        "session-only fragment. Only works on a file currently in the "
        "active group's own list; refused otherwise. Previews the "
        "reverting diff the same way side-load previews its own.",
        {"type": "object",
         "properties": {"fragment": {"type": "string",
                                     "description": "a file currently "
                                                    "loaded on top of the "
                                                    "active one, to drop "
                                                    "back out"}},
         "required": ["fragment"]},
        True, _tool_side_unload, _side_unload_preview),
    Tool("gist-list", "List reusable spec fragments kept outside any one "
        "project -- 'gist ls' on the command line. Each line is a name, "
        "its description, and the absolute path to hand straight to "
        "side-load. Check this before drafting a fragment from scratch "
        "for something that sounds like a repeat of earlier work.",
        _no_args(), False, _tool_gist_list),
    Tool("gist-show", "Print one gist's raw file content, description "
        "line included -- the same thing 'gist show NAME' prints on the "
        "command line.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a gist name, from gist-list"}},
         "required": ["name"]},
        False, _tool_gist_show),
    Tool("gist-create", "Save a spec fragment as a reusable gist, kept "
        "outside any one project so it can be side-loaded again in a "
        "different one later. Refused if the name is already taken. A "
        "person reviews the whole new file (shown as a diff against "
        "nothing) before it's written, same as spec-create. Offer this "
        "as an alternative to a project's own 'requires:' permanence "
        "once a fragment has proven itself (side-load, and a build if "
        "it rebuilds something) and looks like something worth reusing "
        "elsewhere, not only worth keeping in this one project.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "kebab-case, letters/"
                                                "digits/hyphens only"},
                        "description": {"type": "string",
                                        "description": "one line, shown by "
                                                       "gist-list"},
                        "content": {"type": "string",
                                   "description": "the fragment, as YAML"}},
         "required": ["name", "description", "content"]},
        True, _tool_gist_create, _tool_gist_create_preview),
    Tool("gist-delete", "Permanently remove a gist -- a person reviews "
        "its content (shown as a diff against nothing removed, i.e. "
        "every line dropped) before it's deleted, same as any other "
        "consequential action. Only removes the gist itself, never a "
        "project that side-loaded it before.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a gist name, from gist-list"}},
         "required": ["name"]},
        True, _tool_gist_delete, _tool_gist_delete_preview),
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
# about to ask about.
def _confirm(app, tool, arguments, preview):
    from textual.worker import get_current_worker
    event = threading.Event()
    answer = {}

    def resolved(approved):
        answer["approved"] = approved
        event.set()

    def open_modal():
        app.push_screen(ConfirmAction(tool, arguments, preview, resolved))

    app.call_from_thread(open_modal)
    worker = get_current_worker()
    while not event.wait(timeout=0.2):
        if worker.is_cancelled:
            return False
    return answer.get("approved", False)

def _dispatch(app, call):
    tool = TOOLS.get(call.function.name)
    if tool is None:
        return "unknown tool '%s'" % call.function.name
    try:
        arguments = json.loads(call.function.arguments or "{}")
    except ValueError:
        arguments = {}
    if tool.gated:
        preview = None
        # A bad call is refused here, before anyone is asked to approve
        # anything -- _confirm()'s modal is for reviewing a real, valid
        # change, not for rejecting a broken request.
        if tool.preview:
            pre = tool.preview(app, arguments)
            if not pre.ok:
                return pre.message
            preview = pre.message
        if not _confirm(app, tool, arguments, preview):
            return "denied by user"
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
        from seine.utils import ContainerEngine
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

# Appended to the system prompt fresh every turn (in _run()'s loop, not
# injected into state.messages, so it never appears in the chat pane) --
# a build started or finished mid-conversation is state the model has
# no other way to notice. Overall state only, not a per-step breakdown:
# build-status already covers that on demand.
def _live_status(app):
    build = app.build_state
    if len(build.order) == 0:
        return ""
    if build.running:
        overall = "running"
    elif build.done and build.error:
        overall = "failed"
    elif build.done:
        overall = "finished"
    else:
        overall = "not started yet"
    return ("\n\nThe build this TUI session itself has run is currently: "
            "%s. This is live, not something said earlier in the "
            "conversation -- trust it over your own prior turns, and call "
            "build-status for the per-step picture if that's what's "
            "actually asked." % overall)

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
            stream = litellm.completion(model=model, api_base=api_base, api_key=api_key,
                                        messages=full, tools=TOOL_SCHEMAS,
                                        tool_choice="auto", stream=True,
                                        stream_options={"include_usage": True})
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
