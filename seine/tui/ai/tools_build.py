# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Build/vendor/cache/log/audit/sbom tools: read-only status plus the
# gated start-build/cancel-build/start-vendor/cancel-vendor actions.

import json
import os
import re

from seine.container import ContainerEngine

from . import Preview, Tool, _no_args, _single_group, NO_SINGLE_GROUP
from .tools_spec import SPEC_DUMP_CHUNK_LINES, _text_chunk

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

# render_vendor()/render_vendor_why() are text already, same as every
# other render_*() -- chunked here (SPEC_DUMP_CHUNK_LINES/_text_chunk,
# defined further down, beside spec-dump) the same way spec-dump/docs
# are, rather than handed back whole: a large closure's own summary can
# run to one line per suite plus per-package reasons, no different in
# kind from a merged spec dump.
def _tool_vendor(app, arguments):
    from seine.tui import render
    text = render.render_vendor(app.context, suite=arguments.get("suite"))
    lines = text.splitlines()
    try:
        start = int(arguments["start"]) if arguments.get("start") else 1
        end = int(arguments["end"]) if arguments.get("end") else start + SPEC_DUMP_CHUNK_LINES - 1
    except (TypeError, ValueError):
        return "'start'/'end' must be line numbers (1-indexed)"
    return _text_chunk(lines, start, end, SPEC_DUMP_CHUNK_LINES, "the vendor status")

def _tool_vendor_why(app, arguments):
    package = arguments.get("package")
    if not package:
        return "give 'package', a source package name from 'vendor'"
    from seine.tui import render
    text = render.render_vendor_why(app.context, package, suite=arguments.get("suite"))
    lines = text.splitlines()
    try:
        start = int(arguments["start"]) if arguments.get("start") else 1
        end = int(arguments["end"]) if arguments.get("end") else start + SPEC_DUMP_CHUNK_LINES - 1
    except (TypeError, ValueError):
        return "'start'/'end' must be line numbers (1-indexed)"
    return _text_chunk(lines, start, end, SPEC_DUMP_CHUNK_LINES, "the answer")

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

# Tail of today's gated-tool audit trail (ContainerEngine.audit(),
# written by the package's own '_audit()') -- capped the same way
# task-log's own tail is, newest activity kept when there's more than
# fits.
AUDIT_LOG_MAX_ROWS = 50

def _tool_audit_log(app, arguments):
    import datetime
    path = ContainerEngine.audit()
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return "no gated tool calls recorded yet today"
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    if not entries:
        return "no gated tool calls recorded yet today"
    if len(entries) > AUDIT_LOG_MAX_ROWS:
        entries = entries[-AUDIT_LOG_MAX_ROWS:]
    rows = []
    for e in entries:
        when = datetime.datetime.fromtimestamp(e["ts"]).strftime("%H:%M:%S")
        verdict = "approved" if e["approved"] else "denied"
        args = ", ".join("%s=%s" % (k, v) for k, v in e.get("arguments", {}).items())
        rows.append("%s %-16s %-8s %s -- %s" % (when, e["tool"], verdict, args, e["result"]))
    return "\n".join(rows)

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
def _tool_installed_packages(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    tarball = build.image._tarball
    if not tarball:
        return "no tarball for this build yet -- '/build' it first"
    from seine import sbom
    packages = sbom.installed_packages(tarball)
    if len(packages) == 0:
        return ("no package data available -- the tarball may "
                "already be cleaned up (it is not kept after a build "
                "finishes unless '--keep' was used)")
    name = arguments.get("name")
    if name:
        import re
        try:
            regex = re.compile(name, re.IGNORECASE)
        except re.error as e:
            return "'%s' is not a usable pattern: %s" % (name, e)
        matches = [(pkg, version, kib) for pkg, version, kib in packages if regex.search(pkg)]
        if not matches:
            return "no installed package matching '%s'" % name
        return "\n".join("%-40s %-20s %8d KiB" % (pkg, version, kib) for pkg, version, kib in matches)
    lines = ["%-40s %-20s %8d KiB" % (pkg, version, kib) for pkg, version, kib in packages[:30]]
    return "\n".join(lines)

# Capped the same way task-log/spec-query are -- a real scan can easily
# run into four figures of findings (examples/pc-image's own SBOM had
# 1664, seen live while secscan.py was written).
ISSUES_MAX_ROWS = 50

# Never scans itself: seine/secscan.py's own scan() would, on a missing
# or stale cache, run a container (or an external program) with
# '--update-db' -- a real network fetch, exactly the kind of
# consequential action [GATED] reserves for a confirmed call, not an
# always-available read tool. read_cache() only ever reads back what
# '/issues' (or 'seine issues') already scanned and cached -- 'name'/
# 'min_urgency' then narrow that cached list the same way
# installed-packages' own 'name' does.
def _tool_issues(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    from seine import sbom, secscan
    path = sbom.output_path(build.image._output)
    findings = secscan.read_cache(path) if os.path.isfile(path) else None
    if findings is None:
        return ("no CVE scan cached yet for this build's own SBOM -- "
                "call 'issues-scan' to run one")
    try:
        findings = secscan.filter_findings(
            findings, package=arguments.get("name"), min_urgency=arguments.get("min_urgency"))
    except ValueError as e:
        return str(e)
    if not findings:
        return "no cached findings match"
    lines = ["%-16s %-24s %-18s %s" % (f.cve, f.package, f.urgency, f.status)
             for f in findings[:ISSUES_MAX_ROWS]]
    text = "\n".join(lines)
    if len(findings) > ISSUES_MAX_ROWS:
        text += ("\n... (%d more -- narrow with 'name' or 'min_urgency')"
                 % (len(findings) - ISSUES_MAX_ROWS))
    return text

# The scan 'issues' above can only read back -- gated like 'source-pull',
# for the same reason 'issues' stays read-only (a container run, or the
# configured external program, downloading a security-tracker database:
# real network activity a person should confirm).
def _issues_scan_preview(app, arguments):
    build = _single_group(app)
    if build is None:
        return Preview(False, NO_SINGLE_GROUP)
    from seine import sbom
    path = sbom.output_path(build.image._output)
    if not os.path.isfile(path):
        return Preview(False, "no SBOM for this build yet -- a "
                       "'packages_only' build never writes one either "
                       "([SBOM-NEEDS-ROOTFS]); 'start-build' without it "
                       "does")
    return Preview(True, "would run a real CVE scan (a container, or "
                   "the configured external program) against this "
                   "build's own SBOM")

def _tool_issues_scan(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    import subprocess
    from seine import sbom, secscan
    path = sbom.output_path(build.image._output)
    if not os.path.isfile(path):
        return "no SBOM for this build yet"
    release = build.spec["distribution"]["release"]
    rescan = bool(arguments.get("rescan", False))
    try:
        findings = secscan.scan(path, distro=release, rescan=rescan)
    except (OSError, subprocess.CalledProcessError) as e:
        return "scan failed: %s" % e
    return ("scan complete -- %d finding(s) now cached, 'issues' to "
            "read them" % len(findings))

# Marks the build as the AI chat's own -- only a build started this way
# gets the unprompted notify_build_finished() turn, never one begun by
# '/build' or the Build screen. Set right after start_build() (whose
# reset() clears it), so a race with an early finished_ok() can't miss it.
def _start_ai_build(app, build, packages_only, target):
    from seine.tui.build import start_build
    # Same as '/build' (seine/tui/commands.py) -- installed-packages and
    # read's own SBOM lookup both need one to have been produced.
    build.options["sbom"] = True
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
    # A model sending "" rather than omitting 'target' would otherwise
    # reach build.image.tasks() as a real, invalid task name.
    target = arguments.get("target") or None
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

# Refuses before ConfirmAction ever opens when there is nothing to
# cancel -- without this, confirming "cancel the build?" against an
# already-finished build only then reveals there was nothing running.
def _cancel_build_preview(app, arguments):
    if not app.build_state.running:
        return Preview(False, "no build is running")
    return Preview(True, "would cancel the running build")

# Marks the run as the AI chat's own -- only one started this way gets
# the unprompted notify_vendor_finished() turn, never one begun by
# '/vendor' or the Vendor screen. Set right after start_vendor() (whose
# reset() clears it), the same race-avoidance _start_ai_build() needs.
def _start_ai_vendor(app, distro, entries, exclude, wanted, extra_archs):
    from seine.tui.vendor import start_vendor
    start_vendor(app, app.vendor_state, distro, entries, exclude, wanted,
                 extra_archs=extra_archs)
    app.vendor_state.notify_ai = True

def _tool_start_vendor(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    from seine.tui.vendor import prepare
    try:
        distro, entries, exclude, wanted, extra_archs = prepare(
            build, arguments.get("suite") or None)
    except ValueError as e:
        return "could not start: %s" % e
    try:
        # Crosses back through call_from_thread the same boundary
        # TextualReporter does -- start_vendor() touches Indicators.
        app.call_from_thread(_start_ai_vendor, app, distro, entries, exclude,
                             wanted, extra_archs)
    except RuntimeError as e:
        return "could not start: %s" % e
    app.call_from_thread(app.show, "vendor")
    return "vendor started (%s)" % ", ".join(wanted)

# Refuses before ConfirmAction ever opens for the same reasons
# start_vendor() itself would raise or prepare() would error -- a bad
# 'suite', a specification with no 'vendor:' section, one already
# running, or a build in the way. Without this, approving "start a
# vendor run?" only then reveals it was never going to work.
def _start_vendor_preview(app, arguments):
    build = _single_group(app)
    if build is None:
        return Preview(False, NO_SINGLE_GROUP)
    if app.vendor_state.running:
        return Preview(False, "a vendor is already running")
    if app.build_state.running:
        return Preview(False, "a build is running -- wait for it to finish first")
    from seine.tui.vendor import prepare
    try:
        prepare(build, arguments.get("suite") or None)
    except ValueError as e:
        return Preview(False, str(e))
    return Preview(True, "would start a vendor run")

def _tool_cancel_vendor(app, arguments):
    if not app.vendor_state.running:
        return "no vendor is running"
    from seine import tasks
    tasks.interrupt()
    return "cancelling -- waiting for running steps to finish"

def _cancel_vendor_preview(app, arguments):
    if not app.vendor_state.running:
        return Preview(False, "no vendor is running")
    return Preview(True, "would cancel the running vendor")

TOOLS = [
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
    Tool("audit-log", "Every gated tool call this session's AI has made "
        "today, oldest first: when, which tool, approved or denied, its "
        "arguments, and what it returned. Ungated -- reading the trail "
        "is not itself an action worth confirming.",
        _no_args(), False, _tool_audit_log),
    Tool("vendor", "Every suite the active specification's 'vendor:' "
        "section names (or just one, with 'suite'): source/binary "
        "package counts, which are 'direct' (an explicit 'vendor:' "
        "entry) versus pulled in as a build dependency, and the "
        "dependency graph's own size if 'seine vendor' has resolved it. "
        "Says 'no vendor graph yet' for a suite nobody has vendored, "
        "and works on a specification with no 'image:' section at all "
        "-- vendoring is independent of building. Chunked like "
        "spec-dump -- give 'start'/'end' for more once the first "
        "chunk's header says how many lines there are.",
        {"type": "object",
         "properties": {"suite": {"type": "string",
                                  "description": "narrow to one suite "
                                                 "instead of every one "
                                                 "'vendor:' names"},
                        "start": {"type": "integer",
                                 "description": "1-indexed line to start "
                                                "from, for a chunk past "
                                                "the first"},
                        "end": {"type": "integer",
                               "description": "1-indexed line to end at "
                                              "-- omit for a default-size "
                                              "chunk from 'start'"}},
         "required": []},
        False, _tool_vendor),
    Tool("vendor-why", "Why a source package ended up in the vendor -- "
        "read straight off the resolver's own recorded dependency "
        "graph (edges/pruned it wrote when 'seine vendor' last resolved "
        "this suite), never re-derived by guessing at Build-Depends. "
        "Says whether 'package' is a root (an explicit 'vendor:' entry) "
        "or an extra, then every recorded (parent, via binary, field, "
        "arch, depth) reason it was pulled in, shallowest first. A "
        "package the vendor never reached at all says so plainly, "
        "rather than an empty answer that reads like 'no reason "
        "found'. Chunked like 'vendor' above.",
        {"type": "object",
         "properties": {"package": {"type": "string",
                                    "description": "a source package "
                                                   "name, e.g. from "
                                                   "'vendor' above"},
                        "suite": {"type": "string",
                                 "description": "narrow to one suite "
                                                "instead of searching "
                                                "every one 'vendor:' "
                                                "names"},
                        "start": {"type": "integer",
                                 "description": "1-indexed line to start "
                                                "from, for a chunk past "
                                                "the first"},
                        "end": {"type": "integer",
                               "description": "1-indexed line to end at "
                                              "-- omit for a default-size "
                                              "chunk from 'start'"}},
         "required": ["package"]},
        False, _tool_vendor_why),
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
    Tool("installed-packages", "The active build's installed packages -- "
        "name, version, and size -- the largest 30 by 'Installed-Size' "
        "with no 'name', or every package matching 'name' (a regex, "
        "case-insensitive, e.g. 'sudo' or 'sudo|doas' or '^lib') "
        "regardless of size. Reads dpkg's own record of what is "
        "actually installed -- the reliable way to answer 'is package "
        "X in my image' and 'which version of X got installed', unlike "
        "a build log (task names, not package names) or the spec "
        "(declared, not necessarily installed).",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a regex to match package names "
                                                "against, instead of the top 30"}},
         "required": []},
        False, _tool_installed_packages),
    Tool("issues", "Known CVEs against the active build's own SBOM, "
        "read from whatever '/issues' (the TUI screen), 'seine issues' "
        "(the command line), or 'issues-scan' (below) already scanned "
        "and cached -- never scans on its own, since that means a "
        "container run or an external program downloading a fresh "
        "security-tracker database, both real network activity. Answers "
        "'nothing cached yet' if none of those has run for this build -- "
        "call 'issues-scan' to fix that. 'name' "
        "narrows to a package (a regex, case-insensitive, same as "
        "installed-packages' own 'name'); 'min_urgency' drops anything "
        "less severe than it -- one of high, medium, low, unimportant, "
        "end-of-life, not-yet-assigned (the default: everything "
        "cached). The urgency shown is Debian's own triage label, not "
        "a CVSS score -- the scan itself carries no numeric severity "
        "at all; ask about a specific CVE's CVSS/EPSS with 'web-fetch' "
        "against opencve.io instead ([CVE-SEVERITY]). A 'packages_only' "
        "build never populates this cache at all ([SBOM-NEEDS-ROOTFS]).",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a regex to match package names "
                                                "against, instead of every cached finding"},
                        "min_urgency": {"type": "string",
                                       "description": "drop anything less severe "
                                                      "than this -- high, medium, "
                                                      "low, unimportant, end-of-life, "
                                                      "or not-yet-assigned"}},
         "required": []},
        False, _tool_issues),
    Tool("issues-scan", "Run a real CVE scan against the active build's "
        "own SBOM -- the same thing '/issues' or 'seine issues' does, so "
        "'issues' above has something to read. A real container run (or "
        "the configured external program) with '--update-db', real "
        "network activity a person reviews first. Refused if this build "
        "has no SBOM yet ([SBOM-NEEDS-ROOTFS]). 'rescan' forces a fresh "
        "scan even if the cached one is still fresh for this SBOM.",
        {"type": "object",
         "properties": {"rescan": {"type": "boolean",
                                   "description": "force a fresh scan even "
                                                  "if the cache is still "
                                                  "fresh"}},
         "required": []},
        True, _tool_issues_scan, _issues_scan_preview),
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
        "isn't known yet. Give one or the other, not both. Not a way to "
        "get an SBOM or CVE picture on its own ([SBOM-NEEDS-ROOTFS]).",
        {"type": "object",
         "properties": {"target": {"type": "string",
                                   "description": "a task name (e.g. "
                                                  "'deploy:linux') to "
                                                  "build, plus whatever "
                                                  "it needs; omit to "
                                                  "build everything, no "
                                                  "other value needed for "
                                                  "that"},
                        "packages_only": {"type": "boolean",
                                          "description": "stop after the "
                                                         "'packages:' section "
                                                         "builds, before rootfs/"
                                                         "image assembly"}},
         "required": []},
        True, _tool_start_build),
    Tool("cancel-build", "Cancel the running build -- the same thing "
        "'/cancel' does.", _no_args(), True, _tool_cancel_build,
        _cancel_build_preview),
    Tool("start-vendor", "Start a real 'seine vendor' run against the "
        "active specification's own 'vendor:' section: every source "
        "package it names, and its full build-dependency closure, "
        "resolved and fetched into a signed apt repository. Switches to "
        "the Vendor screen, same as '/vendor'. Runs for real, network "
        "and container activity both -- refused outright (before "
        "approval) if there is no 'vendor:' section, a bad 'suite', or "
        "a build already running: '/build' and '/vendor' never run at "
        "once (both drive the same underlying task engine).",
        {"type": "object",
         "properties": {"suite": {"type": "string",
                                  "description": "vendor only this "
                                                 "suite; omit to vendor "
                                                 "every suite 'vendor:' "
                                                 "names, no other value "
                                                 "needed for that"}},
         "required": []},
        True, _tool_start_vendor, _start_vendor_preview),
    Tool("cancel-vendor", "Cancel the running vendor -- the same thing "
        "'/cancel' does.", _no_args(), True, _tool_cancel_vendor,
        _cancel_vendor_preview),
]
