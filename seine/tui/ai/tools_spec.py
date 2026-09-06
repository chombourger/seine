# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Specification-reading and -editing tools: spec-files/read/spec-dump/
# docs/spec-query/reset-conversation, plus the gated spec-update/
# spec-create/side-load/side-unload actions.

import glob
import os

from seine.container import ContainerEngine

from . import (Plan, Preview, Tool, _detect_indent, _doc_sources, _no_args,
               _redacted_diff, _single_group, _socket_send, NO_SINGLE_GROUP)

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

# A source-pull tool call, or 'seine source pull' run by hand, lives
# entirely under the workbench -- no active spec needed to read it back,
# same as source-list/gist-show need none. Checked before _single_group()
# for that reason, not after.
def _under_workbench(real):
    workbench = os.path.realpath(ContainerEngine.workbench())
    return real == workbench or real.startswith(workbench + os.sep)

# The active build's own SBOM output (seine build --sbom), if its
# options still say one would be produced and it is actually there --
# not any SBOM (sbom-diff takes an explicit path for that), only the
# one naming what this build itself installs, so the model can look up
# an exact version before calling source-pull instead of guessing one.
def _build_sbom_path(build):
    from seine.sbom import SBOM
    output = SBOM(build.spec["distribution"], build.options)._output_file(
        build.image._output)
    if output is None:
        return None
    path = output + ".spdx.json"
    return path if os.path.isfile(path) else None

def _tool_read(app, arguments):
    path = arguments.get("path")
    if not path:
        return ("read needs a 'path' argument -- a file from spec-files, "
                "one a spec entry's own content already named (e.g. a "
                "package's 'patches:' entry), a file under the workbench "
                "(source-list), or the active build's own SBOM")
    real = os.path.realpath(path)
    if _under_workbench(real):
        try:
            with open(real) as f:
                text = f.read()
        except OSError as e:
            return "could not read %s: %s" % (path, e)
        from seine import sources
        sources.touch_path(real)
        return text

    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP

    sbom_path = _build_sbom_path(build)
    if sbom_path is not None and real == os.path.realpath(sbom_path):
        with open(real) as f:
            return f.read()

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

def _tool_docs(app, arguments):
    sources = _doc_sources()
    if not sources:
        return ("no documentation available in this install of seine -- "
                "neither docs/ nor the prompt's own cluster files were "
                "found")
    name = arguments.get("name")
    if not name:
        available = []
        for dir_path, ext in sources:
            available += sorted(f for f in os.listdir(dir_path) if f.endswith(ext))
        return "give 'name', one of: " + ", ".join(available)
    for dir_path, ext in sources:
        real = os.path.realpath(os.path.join(dir_path, name))
        if (os.path.dirname(real) == os.path.realpath(dir_path)
                and real.endswith(ext) and os.path.isfile(real)):
            with open(real) as f:
                lines = f.read().splitlines()
            try:
                start = int(arguments["start"]) if arguments.get("start") else 1
                end = int(arguments["end"]) if arguments.get("end") else start + SPEC_DUMP_CHUNK_LINES - 1
            except (TypeError, ValueError):
                return "'start'/'end' must be line numbers (1-indexed)"
            return _text_chunk(lines, start, end, SPEC_DUMP_CHUNK_LINES, name)
    return ("%s is not one of this seine's own docs/*.md or prompt "
            "cluster *.txt files" % name)

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
    _socket_send(app, {"type": "spec_written", "path": plan.path})
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
    _socket_send(app, {"type": "spec_written", "path": plan.path})
    return "wrote %s" % arguments.get("path")

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
    from seine.build import BuildCmd
    from seine.diffing import diff
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
    from seine.diffing import diff
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
    from seine.build import BuildCmd
    from seine.diffing import diff
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
    from seine.diffing import diff
    changes = diff(before, after.dump(after.spec), color=False)
    return "side-unloaded %s\n\n%s" % (fragment, changes)

TOOLS = [
    Tool("spec-files", "Every file the active specification actually "
        "loaded, in load order -- the only paths spec-update will "
        "accept. Also lists, separately, any '*.yml'/'*.yaml' sitting "
        "in the same or a cousin directory that this build does NOT "
        "load -- a candidate to pull in with 'requires:' before "
        "assuming a new file (spec-create) is needed, and also "
        "inspectable first with read or a targeted spec-query; "
        "spec-update still refuses them.",
        _no_args(), False, _tool_spec_files),
    Tool("read", "A file this build trusts, as written (not rendered, "
        "not merged with any other file) -- secrets still redacted. "
        "Four kinds: a spec file spec-files listed (loaded or an "
        "unloaded sibling), returned as YAML; a local file one of "
        "this build's own 'packages:' entries names -- a patch, a "
        "kernel config fragment, a derived-flavour fragment -- shown "
        "as plain text once its path has actually turned up in a "
        "spec-query/spec-dump result (never guess one); anything under "
        "the workbench (source-list, source-pull) -- no active "
        "specification needed for this one; or the active build's own "
        "SBOM (seine build --sbom), to look up a package's exact "
        "installed version before calling source-pull. Refused for "
        "anything else.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "a file path from spec-files, "
                                                "source-list, or one a spec "
                                                "entry's own content already "
                                                "named"}},
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
    Tool("docs", "Two kinds of written reference, chunked the same way, "
        "one 'name' namespace since they never share an extension: a "
        "cluster file ([PROMPT-DOCS] names them and what each covers) "
        "-- detail behind this system prompt's own tags, always "
        "present; and seine's own docs/*.md ('specification.md' for "
        "the full YAML schema, 'kernels.md' for kernel rebuilds, and "
        "others) -- not present in every install (a packaged one may "
        "not carry it). Omit 'name' to see what's actually available "
        "right now in *this* install, from both, rather than assuming "
        "last turn's list still holds -- a cluster file can change "
        "between turns the same way this prompt's own text can. Fetch "
        "the matching cluster before acting on something it covers, "
        "not only when asked to explain it, and prefer what it says "
        "this call over a memory of what an earlier turn's fetch said.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a cluster or docs/*.md "
                                                "filename, e.g. "
                                                "'gists.txt' or "
                                                "'specification.md'; "
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
]
