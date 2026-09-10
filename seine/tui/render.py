# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Text for the Overview/Plan screens, built from the same calls
# 'seine plan'/'seine build --dry-run' make. Kept apart from the widgets
# so it can be tested without a running App.

import contextlib
import io
import json
import os
import subprocess
import time

from seine import analyze
from seine import multiconfig
from seine import packages
from seine import sbom
from seine import secscan
from seine.diffing import diff, recall
from seine.progress import elapsed
from seine.sbuild import BuilderImage
from seine.container import ContainerEngine
from seine.utils import git_status, lock_sibling

# One build's overview lines: distro/arch, last-build history,
# baseline-diff status, output path. Shared by render_overview() (every
# active group, one after another) and render_root_node() (just the one
# group a root node was selected for).
def _overview_lines(files, build, name):
    parts = []
    distro = build.spec["distribution"]
    output = build.image._output
    parts.append("%s -- %s/%s" % (name, distro["release"], distro["architecture"]))

    digest = analyze.spec_digest(build.spec)
    history = analyze.runs(digest)
    if history:
        latest = history[0]
        ago = max(0, time.time() - latest["started"])
        status = "built" if latest.get("ok", True) else "FAILED"
        parts.append("  last build: %s %s ago, took %s"
                    % (status, elapsed(ago), elapsed(analyze.spent(latest))))
    else:
        parts.append("  never built from here")

    baseline = recall(files)
    if baseline is None:
        parts.append("  spec: not built from here yet")
    elif build.dump(build.spec) == baseline:
        parts.append("  spec: unchanged since the last build")
    else:
        parts.append("  spec: changed since the last build -- '/plan' shows how")

    if output:
        parts.append("  would write: %s" % output)
    return parts

def render_overview(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    parts = []
    for files, build in zip(context.groups, context.builds):
        output = build.image._output
        name = output.rsplit("/", 1)[-1].rsplit(".", 1)[0] if output else \
               context.label()
        parts += _overview_lines(files, build, name)
    return "\n".join(parts) + "\n"

# Root/group node: the spec files this group loaded, each marked with
# its 'git status --porcelain' code (mirrors git's own vocabulary
# rather than a single invented "changed" marker, so worktree/index/
# untracked nuance comes for free) and a trailing lock icon for any
# with a loaded '.lock.yaml' sibling (trailing, so the file names stay
# left-aligned regardless of the icon's cell width), then the same
# per-build stats render_overview() shows above, scoped to just this
# one group.
def render_root_node(files, build):
    lines = ["spec files:"]
    for path in files:
        status = git_status(path) or "  "
        sibling = lock_sibling(path)
        lock = " \U0001F512" if sibling and os.path.isfile(sibling) else ""
        lines.append("  %s %s%s" % (status, path, lock))
    output = build.image._output
    name = (output.rsplit("/", 1)[-1].rsplit(".", 1)[0] if output else
           multiconfig._label(build))
    lines.append("")
    lines += _overview_lines(files, build, name)
    return "\n".join(lines) + "\n"

# Image node: partitions/volumes drawn as proportional stacked boxes,
# not the plain key/value dump the generic node fallback would give.
# seine's spec has no 'swap'/'ESP' concept, so roles are read off what
# a partition/volume actually declares (boot/xbootldr flags, 'where',
# fs type) rather than inventing fields that don't exist. A container
# (an LVM-PV partition, the only entry without a 'where') gets its own
# color -- purple -- rather than sharing plain blue 'data'.
_ROLE_STYLES = {"boot": "cyan", "root": "green", "lvm": "purple",
                "data": "blue", "verity": "magenta"}
_ROLE_ORDER = ("boot", "root", "lvm", "data", "verity")

def _role(entry):
    flags = entry.get("flags") or []
    if "boot" in flags or "xbootldr" in flags:
        return "boot"
    if entry.get("type") == "verity-hash":
        return "verity"
    if entry.get("where") == "/":
        return "root"
    # No 'where' at all (only an LVM-PV partition can lack one --
    # PartitionHandler._parse_part() requires it otherwise): the
    # container itself, not a mounted leaf.
    if "where" not in entry:
        return "lvm"
    return "data"

# A partition carries its own 'group' AND has no 'where' only when it's
# the LVM-PV holding a group of volumes -- the one structural signal
# PartitionHandler's own parsing guarantees (see _role() above).
def _is_container(entry):
    return "where" not in entry and entry.get("group") is not None

FLOOR_ROWS = 2  # label + size, the decided two-row box content
# ponytail: an arbitrary but readable spread of extra rows the
# image's largest leaf can earn over the floor; tune if it reads wrong.
EXTRA_ROWS_BUDGET = 16
# Widest a box ever gets -- the whole pane on a wide terminal. The
# actual width comes from the pane itself (render_image_node()'s
# 'width'): the right pane only owns a third of the screen, so a fixed
# 45 overflows it on a standard 80-column terminal.
BOX_WIDTH = 45
# Narrowest a box still reads as one: borders plus a few label
# characters. Only matters on a comically narrow terminal; the pane
# hands down something wider in practice.
MIN_BOX_WIDTH = 12

def _extra_rows(sizes, budget=EXTRA_ROWS_BUDGET):
    total = sum(sizes)
    if total <= 0:
        return [0] * len(sizes)
    return [round(budget * size / total) for size in sizes]

# Leaves only, but budgeted across the WHOLE image in one go: top-level
# partitions and every LVM group's volumes compete for the same extra
# rows. Per-group budgets made cross-group comparisons lie -- a 128MiB
# top-level partition sharing a budget with only a 16MiB sibling earned
# nearly as many rows as a 2GiB volume sharing its own group's budget
# with a 512MiB sibling, and could even render taller than it.
def _leaf_boxes(entries):
    boxes = [{"label": e["label"], "type": e.get("type", ""),
             "size": e.get("size") or 0, "role": _role(e), "children": []}
            for e in entries]
    for box, extra in zip(boxes, _extra_rows([b["size"] for b in boxes])):
        box["rows"] = FLOOR_ROWS + extra
    return boxes

# A container's height is the sum of its own children stacked (plus
# its own header row), not a proportional share computed against its
# sibling leaves -- simpler than computing proportion at every level,
# and what a container displays is already exactly what it contains.
# Its children still earn their rows from the image-wide budget above,
# so a container and a plain partition stay comparable: both bottom
# out at leaves measured on the same scale.
def _layout(image_spec):
    partitions = image_spec.get("partitions") or []
    volumes = image_spec.get("volumes") or []
    by_group = {}
    for volume in volumes:
        by_group.setdefault(volume.get("group"), []).append(volume)

    container_entries = [p for p in partitions if _is_container(p)]
    leaf_entries = [p for p in partitions if not _is_container(p)]
    for entry in container_entries:
        leaf_entries.extend(by_group.get(entry.get("group"), []))

    boxes_by_label = {box["label"]: box for box in _leaf_boxes(leaf_entries)}
    for entry in container_entries:
        children = [boxes_by_label[v["label"]]
                    for v in by_group.get(entry.get("group"), [])]
        rows = 1 + sum(child["rows"] for child in children) if children else FLOOR_ROWS
        boxes_by_label[entry["label"]] = {
            "label": entry["label"], "type": entry.get("type", ""),
            "size": entry.get("size") or 0, "role": _role(entry),
            "children": children, "rows": rows,
        }
    # On-disk order, not leaves-then-containers.
    return [boxes_by_label[p["label"]] for p in partitions]

# One row per box line, each row a list of (segment, role) pairs -- a
# nested child's own frame keeps the CHILD's role, while the outer
# wrapping frame ('| ' / ' |') keeps the PARENT's: the container's
# frame stays one consistent color (purple for an LVM group) around
# inner boxes drawn in their own (green for a root LV, ...). Label/
# detail are truncated to the box, never widening it: a long label
# must not push the art past the pane.
def _box_lines(box, width):
    width = max(width, 8)  # nesting subtracts per level; never let the border math go negative
    inner = width - 2
    field = max(inner - 1, 1)
    role = box["role"]
    size_text = _human_size(box["size"]) if box["size"] else "?"
    detail = "%s  %s" % (box["type"], size_text) if box["type"] else size_text
    lines = [
        [("┌" + "─" * inner + "┐", role)],
        [("│ " + box["label"][:field].ljust(field) + "│", role)],
        [("│ " + detail[:field].ljust(field) + "│", role)],
    ]
    if box["children"]:
        for child in box["children"]:
            for child_row in _box_lines(child, width - 4):
                lines.append([("│ ", role)] + child_row + [(" │", role)])
    else:
        # A leaf's proportional share over the floor (_extra_rows()) is
        # blank filler, not more detail -- the box's height alone is
        # what carries the size comparison, not repeated text.
        blank = "│" + " " * inner + "│"
        for _ in range(box["rows"] - FLOOR_ROWS):
            lines.append([(blank, role)])
    lines.append([("└" + "─" * inner + "┘", role)])
    return lines

def _flatten(boxes):
    for box in boxes:
        yield box
        yield from _flatten(box["children"])

def render_image_node(image_spec, width=BOX_WIDTH):
    from rich.text import Text
    width = max(MIN_BOX_WIDTH, min(width, BOX_WIDTH))
    boxes = _layout(image_spec)
    text = Text()
    if not boxes:
        text.append("no partitions defined\n")
        return text
    present = {box["role"] for box in _flatten(boxes)}
    text.append("legend: ")
    first = True
    for role in _ROLE_ORDER:
        if role not in present:
            continue
        if not first:
            text.append("  ")
        text.append(role, style=_ROLE_STYLES[role])
        first = False
    text.append("\n\n")
    for box in boxes:
        for row in _box_lines(box, width):
            for segment, role in row:
                text.append(segment, style=_ROLE_STYLES.get(role, ""))
            text.append("\n")
    return text

# Fallback right-pane content for a selected spec-tree node without a
# dedicated renderer of its own (root/image/logs get one; everything
# else lands here): the node's own text, plus its immediate children's
# labels when it's a branch rather than a scalar leaf.
def render_node(node):
    if node.children:
        lines = [str(node.data), ""]
        lines += ["  %s" % child.data for child in node.children]
        return "\n".join(lines) + "\n"
    return "%s\n" % node.data

# Same marks as the Build screen's task pane (BuildState.MARKS) and
# the Test screen's own list (TestState.render()), so a test reads
# the same in the overview as where it ran. Passed/failed marks are
# coloured green/red; pending/running stay unstyled, like the task
# pane itself (which has no colour at all).
TEST_MARKS = {"pending": "○", "running": "●", "done": "✔", "failed": "✘"}
TEST_MARK_STYLES = {"done": "green", "failed": "red"}

def _test_state_for(qualified, rows):
    row = (rows or {}).get(qualified)
    if row is None:
        return "pending"
    return row.get("state", "pending")

def _tests_under(subpath, test_paths):
    prefix = tuple(subpath)
    found = []
    for qualified, path in (test_paths or {}).items():
        if tuple(path)[:len(prefix)] == prefix:
            found.append(qualified)
    return found

def _aggregate_test_state(qualifieds, rows):
    states = [_test_state_for(q, rows) for q in qualifieds]
    if any(s == "failed" for s in states):
        return "failed"
    if any(s == "running" for s in states):
        return "running"
    if any(s == "pending" for s in states):
        return "pending"
    return "done"

# A 'test'-branch node with its children's test status, one mark per
# child that leads to test(s) -- the overview equivalent of the Test
# screen's own list. Returns None when 'subpath' holds no test (a
# scalar field under a case, e.g.), so the caller falls back to
# render_node(). 'subpath' is the node's path below its group root
# (e.g. ("test", "[0]", "tests")), 'test_state' the app's TestState.
def render_test_node(node, subpath, test_state):
    from rich.text import Text
    subpath = tuple(subpath)
    test_paths = getattr(test_state, "test_paths", None) or {}
    rows = getattr(test_state, "rows", None) or {}
    qualified_here = _tests_under(subpath, test_paths)
    if not qualified_here:
        return None
    by_name = {}
    result = getattr(test_state, "result", None)
    if result is not None:
        by_name = {t.name: t for t in (result.tests or [])}
    text = Text()
    state = _aggregate_test_state(qualified_here, rows)
    mark = TEST_MARKS[state]
    style = TEST_MARK_STYLES.get(state, "")
    if style:
        text.append(mark + " ", style=style)
    else:
        text.append(mark + " ")
    text.append("%s\n" % node.data)
    # A single failed test's reason, same as TestState.render() shows
    # under its own row, so a failure always shows a reason here too.
    if len(qualified_here) == 1:
        outcome = by_name.get(qualified_here[0])
        if outcome is not None and outcome.failed and outcome.message:
            text.append("    %s\n" % outcome.message)
    if node.children:
        text.append("\n")
        for child in node.children:
            child_sub = subpath + (child.data,)
            qualified_child = _tests_under(child_sub, test_paths)
            if not qualified_child:
                text.append("  %s\n" % child.data)
                continue
            child_state = _aggregate_test_state(qualified_child, rows)
            child_mark = TEST_MARKS[child_state]
            child_style = TEST_MARK_STYLES.get(child_state, "")
            text.append("  ")
            if child_style:
                text.append(child_mark + " ", style=child_style)
            else:
                text.append(child_mark + " ")
            text.append("%s\n" % child.data)
            if len(qualified_child) == 1:
                outcome = by_name.get(qualified_child[0])
                if outcome is not None and outcome.failed and outcome.message:
                    text.append("      %s\n" % outcome.message)
    return text

# How many runs' links a task-kind bullet shows -- '[latest] [-1] [-2]',
# not the whole history logindex.KEEP keeps on disk.
LOGS_SHOWN = 3

# 'branch' matches spectree.branch_for()'s return shape (a tuple, e.g.
# ("packages",)) -- one bullet per distinct task name that maps to it,
# across every logs/index.json entry for this release/arch. Newest
# entries first, since logindex.entries() already is.
def _logs_by_task(release, arch, branch):
    from seine import logindex
    from seine.tui.spectree import branch_for
    matches = {}
    for entry in logindex.entries():
        if entry["release"] != release or entry["arch"] != arch:
            continue
        for t in entry.get("tasks", []):
            if branch_for(t["name"]) != branch:
                continue
            matches.setdefault(t["name"], []).append(t)
    return matches

# One '@click'-free clickable span per run (see target.py's own
# 'target-click' precedent for why not Rich's '@click' meta): green/red
# for ok/failed, a rocket instead of a link for a cache hit (nothing to
# open -- see logindex.py's 'cached' field), age-labelled ('[latest]',
# '[-1]', ...) rather than a literal list index, since logindex.entries()
# is newest-first and a literal '[-1]' would read as *oldest* in Python
# terms -- the opposite of what's meant here.
def _logs_section(release, arch, branch):
    from rich.style import Style
    from rich.text import Text
    from seine import logindex
    matches = _logs_by_task(release, arch, branch)
    if not matches:
        return None
    text = Text()
    text.append("\nLogs:\n")
    for name in sorted(matches):
        text.append("  - %s: " % name)
        for i, run in enumerate(matches[name][:LOGS_SHOWN]):
            label = "[latest]" if i == 0 else "[-%d]" % i
            if run.get("cached"):
                text.append("\U0001F680", style=Style())
                text.append(label + " ", style=Style(dim=True))
                continue
            color = "red" if run["failed"] else "green"
            # A planned-but-not-started task (see logindex.begin(): a
            # running build announces every task upfront) has no log
            # file yet -- dimmed, no link, rather than a clickable
            # path that opens on "No such file or directory".
            path = logindex.resolve(run["log"]) if run.get("log") else None
            if path is not None and os.path.isfile(path):
                meta = {"log-click": path}
                text.append(label + " ", style=Style(color=color, meta=meta))
            else:
                text.append(label + " ", style=Style(dim=True))
        text.append("\n")
    return text

# Appends a 'Logs:' section to whatever a task-backed node already
# shows (a plain string from render_node(), or a Text from
# render_image_node()) -- None (nothing in logs/index.json for this
# release/arch/branch yet) leaves 'text' untouched.
def append_logs_section(text, release, arch, branch):
    from rich.text import Text
    section = _logs_section(release, arch, branch)
    if section is None:
        return text
    combined = Text(text) if isinstance(text, str) else text.copy()
    combined.append_text(section)
    return combined

# 'Image.plan()' prints straight to stdout, like every other 'seine'
# command; captured rather than reimplemented.
def _captured(fn):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn()
    return out.getvalue()

def render_plan(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for files, build in zip(context.groups, context.builds):
        text = _captured(build.image.plan)
        baseline = recall(files)
        if baseline is not None:
            changes = diff(baseline, build.dump(build.spec), color=False)
            text = "changed since last build:\n%s\n%s" % (changes, text)
        sections.append(text)
    return "\n\n".join(sections)

def _human_size(size):
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if size < 1024 or unit == "TiB":
            return "%.1f%s" % (size, unit) if unit != "B" else "%dB" % size
        size /= 1024

# What a build left behind: a stat-based listing of
# 'ContainerEngine.deploy_root()/<release>/', the same directory named
# by the "would write:" line on Overview.
def render_artifacts(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        release = build.spec["distribution"]["release"]
        root = os.path.join(ContainerEngine.deploy_root(), release)
        lines = ["%s/" % root]
        entries = sorted(os.listdir(root)) if os.path.isdir(root) else []
        if len(entries) == 0:
            lines.append("  nothing built here yet")
        for name in entries:
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            lines.append("  %-32s %10s  %s" % (name, _human_size(st.st_size), when))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"

# Two data sources, labelled apart: what 'packages:' asked to rebuild
# from source, and what the last '--sbom' run found installed.
def render_packages(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        distro = build.spec["distribution"]
        source_packages = build.image.packages
        lines = ["REBUILT FROM SOURCE (packages: section)", ""]
        if len(source_packages) == 0:
            lines.append("  none")
        else:
            builder = packages.Builder(distro, build.options,
                                       BuilderImage(distro, build.options))
            # 'Builder.current()' already only names a stamp on disk.
            current = {(package.name, architecture)
                      for package, architecture, _ in builder.current(source_packages)}
            for package in source_packages:
                for architecture in builder.architectures(package):
                    mark = "✔" if (package.name, architecture) in current else "○"
                    label = builder.label(package, architecture)
                    source = package.spec.get("source") or "(description only)"
                    lines.append("  %s %-24s %s" % (mark, label, source))
                    origin = package.origins.get("source") or package.origins.get("name")
                    if origin:
                        lines.append("      declared by: %s" % origin)

        lines.append("")
        lines.append("INSTALLED (from the last SBOM)")
        output = build.image._output
        sbom_path = sbom.output_path(output) if output else None
        if sbom_path is None or not os.path.isfile(sbom_path):
            lines.append(
                "  no SBOM for this build: run with --sbom to populate this section")
        else:
            try:
                with open(sbom_path) as f:
                    spdx = json.load(f)
                installed = sorted(spdx.get("packages", []),
                                  key=lambda p: p.get("name", ""))
                lines.append("  %d packages -- %s" % (len(installed), sbom_path))
                for entry in installed:
                    lines.append("  %-32s %s" % (entry.get("name", "?"),
                                                  entry.get("versionInfo", "")))
            except (OSError, ValueError) as e:
                lines.append("  could not read %s: %s" % (sbom_path, e))
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"

# 'analyze.blame()'/'analyze.critical_chain()' print, captured for the
# newest recorded run. 'ROOTFS SIZE' below is the one section that reads
# more than the newest run, since a single number can't show growth.
def render_analyze(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    sections = []
    for build in context.builds:
        digest = analyze.spec_digest(build.spec)
        history = analyze.runs(digest)
        if len(history) == 0:
            sections.append("no recorded run for this specification yet -- "
                            "'/build' it once first")
            continue
        latest = history[0]
        text = _captured(lambda run=latest: analyze.blame(run))
        text += "\n" + _captured(lambda run=latest: analyze.critical_chain(run))
        # Newest first, same order analyze.runs() returns. Older runs
        # made before this field existed just have nothing to add here.
        sized = [run for run in history if run.get("rootfs_size") is not None]
        if len(sized) > 0:
            lines = ["", "ROOTFS SIZE"]
            for i, run in enumerate(sized):
                ago = max(0, time.time() - run["started"])
                line = "  %s ago   %s" % (elapsed(ago), _human_size(run["rootfs_size"]))
                if i + 1 < len(sized):
                    delta = run["rootfs_size"] - sized[i + 1]["rootfs_size"]
                    if delta != 0:
                        line += "  (%s%s)" % ("+" if delta > 0 else "-",
                                              _human_size(abs(delta)))
                lines.append(line)
            text += "\n".join(lines) + "\n"
        sections.append(text)
    return "\n\n".join(sections)

# Not spec-scoped: a cache is shared by every build, so this ignores
# 'context'. 'matching' narrows the listing like
# 'seine cache info --entries-matching' does.
def render_cache(matching=None):
    import re
    from seine.cache import CACHES, CacheCmd
    pattern = re.compile(matching) if matching else None
    return _captured(lambda: CacheCmd().info(list(CACHES.keys()), entries=True,
                                             matching=pattern))

def render_doctor(pull=False):
    import re

    from rich.text import Text

    from seine import doctor
    raw = doctor.render(doctor.run(pull=pull))
    text = Text(raw)
    for m in re.finditer(r"  ! ", raw):
        text.stylize("dark_orange", m.start() + 2, m.start() + 3)
    return text

# ChatScreen's one-line #body: which spec the ai.py tools act on, and
# whether the AI chat is even configured.
def render_chat_header(context):
    from seine import settings
    from seine.tui.ai import configured
    spec = context.label() if context.active else "no active specification"
    if configured():
        model = os.environ.get("SEINE_LLM_MODEL") or settings.load()["llm_model"]
        return "%s -- %s" % (spec, model)
    return "%s -- not configured ('/settings' sets llm_model)" % spec

# Not spec-scoped: jobs/resources/theme/llm_* only; startup_commands has
# its own widget on Settings. Unset shows the real fallback value;
# llm_model/llm_api_base have no fallback, so '(unset)' is used instead.
def render_settings():
    from seine import settings
    from seine.build import format_resources
    current = settings.load()
    jobs = str(current["jobs"]) if current["jobs"] is not None else "1 (default)"
    resources = format_resources(current["resources"]) or "(unset, follows jobs)"
    theme = current["theme"] or "dark (default)"
    llm_model = current["llm_model"] or "(unset)"
    llm_api_base = current["llm_api_base"] or "(unset)"
    # Order matches GeneralSettings.KEYS (seine/tui/settings.py):
    # 'resources' last so 'theme' stays one 'down' press from the top.
    return "\n".join(
        "%-16s %s" % (key, value) for key, value in
        [("jobs", jobs), ("theme", theme), ("llm_model", llm_model),
         ("llm_api_base", llm_api_base), ("resources", resources)]
    ) + "\n"

# Same "exactly one active group" restriction ai.py's tools apply to a
# spec-scoped call; multi-group specifications aren't driven here yet.
def _issues_build(context):
    if not context.active:
        return None, "no active specification -- '/use SPEC...' picks one\n"
    if len(context.builds) != 1:
        return None, ("multi-group specifications ('/use a -- b') aren't "
                      "supported here yet -- '/use' a single one\n")
    return context.builds[0], None

# The active build's SBOM path, or the "not built yet" message, shared
# by both render_issues_* below so the two panes never disagree.
def _issues_sbom_path(build):
    path = sbom.output_path(build.image._output)
    if os.path.isfile(path):
        return path, None
    return None, ("no SBOM for this build yet -- every TUI '/build' writes "
                  "one, or run 'seine build --sbom' first\n")

def render_issues_table(context, package=None, min_urgency=None, rescan=False):
    build, error = _issues_build(context)
    if error:
        return error
    path, error = _issues_sbom_path(build)
    if error:
        return error
    release = build.spec["distribution"]["release"]
    try:
        findings = secscan.scan(path, distro=release, rescan=rescan)
        findings = secscan.filter_findings(findings, package=package, min_urgency=min_urgency)
    except ValueError as e:
        return "%s\n" % e
    except (OSError, subprocess.CalledProcessError) as e:
        return "scan failed: %s\n" % e
    if not findings:
        return "no known CVEs found\n"
    width = max(len(f.package) for f in findings)
    lines = ["%-16s %-*s %-18s %s" % (f.cve, width, f.package, f.urgency, f.status)
             for f in findings]
    return "\n".join(lines) + "\n"

# Reads whatever render_issues_table() left cached rather than
# scanning again; called after it in IssuesScreen.update_body(), so a
# '/issues --rescan' has already refreshed the cache by the time this runs.
def render_issues_stats(context):
    build, error = _issues_build(context)
    if error:
        return error
    path, error = _issues_sbom_path(build)
    if error:
        return error
    release = build.spec["distribution"]["release"]
    try:
        findings = secscan.scan(path, distro=release)
    except (OSError, subprocess.CalledProcessError) as e:
        return "scan failed: %s\n" % e
    data = secscan.stats(findings)
    lines = ["TOTALS", " %d findings" % data["total"],
             " %d unique CVEs" % data["unique_cves"],
             " %d packages affected" % data["packages"], "", "BY URGENCY"]
    for level in secscan.URGENCY_ORDER:
        lines.append(" %-17s %4d" % (level, data["by_urgency"].get(level, 0)))
    lines += ["", "BY STATUS"]
    for status, count in data["by_status"].most_common():
        lines.append(" %-17s %4d" % (status, count))
    lines += ["", "TOP PACKAGES"]
    for pkg, count in data["by_package"].most_common(10):
        lines.append(" %-17s %4d" % (pkg, count))
    return "\n".join(lines) + "\n"

# Shared by render_vendor()/render_vendor_why(): a build's 'vendor:'
# entries and the distribution they resolve against. Unlike
# _issues_build() above, this loops every group in context.builds
# rather than refusing more than one, same as render_packages().
def _vendor_entries(build):
    from seine import vendor, utils
    try:
        entries = vendor.parse(build.spec)
        distro = utils.distribution(build.spec)
    except ValueError as e:
        return None, None, "%s\n" % e
    return entries, distro, None

# One suite's summary: source/binary counts, which entries are
# 'direct' versus pulled in as a build dependency, and the graph's size
# if resolved. A suite never vendored says so rather than showing zeroes.
def _render_vendor_suite(suite, document):
    sources = document.get("sources", {})
    if len(sources) == 0:
        return "%s: no vendor graph yet -- 'seine vendor' first\n" % suite
    roots = sorted(name for name, entry in sources.items() if entry.get("direct"))
    binaries = sum(len(entry.get("binaries", {})) for entry in sources.values())
    lines = ["%s -- %d source package(s) (%d direct, %d pulled in), "
             "%d binary package(s)"
             % (suite, len(sources), len(roots), len(sources) - len(roots), binaries)]
    lines.append("  roots: %s" % (", ".join(roots) if roots else "(none)"))
    graph = document.get("graph")
    if graph is None:
        lines.append("  no dependency graph recorded yet -- vendored before "
                     "graph tracking existed, '--refresh' to get one")
    else:
        pruned = graph.get("pruned", {})
        lines.append("  %d edge(s), %d pruned build-dep(s)"
                     % (len(graph.get("edges", [])),
                        len(pruned.get("base_chroot", [])) +
                        len(pruned.get("excluded", []))))
    return "\n".join(lines)

# Every suite a specification's 'vendor:' section names (or just one,
# with 'suite'), summarised by _render_vendor_suite(). The first thing
# asked before narrowing to one package with render_vendor_why().
def render_vendor(context, suite=None):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    from seine import vendor
    sections = []
    for build in context.builds:
        entries, distro, error = _vendor_entries(build)
        if error:
            sections.append(error)
            continue
        if len(entries) == 0:
            sections.append("no 'vendor:' section in this specification\n")
            continue
        available = vendor.named_suites(entries, distro)
        if suite is not None and suite not in available:
            sections.append(
                "'%s' is not a suite this specification's 'vendor:' asks "
                "for -- expected one of %s\n" % (suite, ", ".join(available)))
            continue
        for s in ([suite] if suite is not None else available):
            sections.append(_render_vendor_suite(s, vendor.load_manifest(s)))
    return "\n\n".join(sections) + "\n"

# One package's breadcrumb: whether it's a root or an extra, and every
# recorded reason it's here, read from the graph's 'reverse' map. 'graph'
# being None ("we never asked") and 'reverse' having nothing for this
# package ("we asked and found nothing") are told apart deliberately.
def _render_vendor_why_suite(suite, package, entry, graph):
    kind = ("direct -- an explicit 'vendor:' entry" if entry.get("direct")
           else "extra -- pulled in as a build-dependency")
    lines = ["%s -- %s: %s" % (suite, package, kind)]
    reverse = (graph or {}).get("reverse", {})
    reasons = reverse.get(package, [])
    if reasons:
        lines.append("  reached via:")
        for r in reasons:
            lines.append("    %s build-depends on %s (%s, %s) -- depth %d"
                         % (r["parent"], r["via"], r["field"], r["arch"], r["depth"]))
    elif graph is None:
        lines.append("  no dependency graph recorded for this suite yet -- "
                     "vendored before graph tracking existed, '--refresh' "
                     "to get one")
    elif not entry.get("direct"):
        lines.append("  no recorded reason -- resolved before this graph "
                     "was written")
    return "\n".join(lines)

def render_vendor_why(context, package, suite=None):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    from seine import vendor
    sections = []
    for build in context.builds:
        entries, distro, error = _vendor_entries(build)
        if error or len(entries) == 0:
            continue
        available = vendor.named_suites(entries, distro)
        for s in ([suite] if suite is not None else available):
            if s not in available:
                continue
            document = vendor.load_manifest(s)
            sources = document.get("sources", {})
            if package not in sources:
                continue
            sections.append(_render_vendor_why_suite(
                s, package, sources[package], document.get("graph")))
    if len(sections) == 0:
        scope = " in '%s'" % suite if suite else ""
        return "'%s' is not in this specification's vendor%s\n" % (package, scope)
    return "\n\n".join(sections) + "\n"
