# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Text for the Overview/Plan screens, built from the same calls
# 'seine plan'/'seine build --dry-run' already make -- nothing here is
# computed twice. Kept apart from the widgets so it can be tested without
# a running App.

import contextlib
import io
import json
import os
import subprocess
import time

from seine import analyze
from seine import packages
from seine import sbom
from seine import secscan
from seine.build import diff, recall
from seine.progress import elapsed
from seine.sbuild import BuilderImage
from seine.utils import ContainerEngine

def render_overview(context):
    if not context.active:
        return "no active specification -- '/use SPEC...' picks one\n"
    parts = []
    for files, build in zip(context.groups, context.builds):
        distro = build.spec["distribution"]
        output = build.image._output
        name = output.rsplit("/", 1)[-1].rsplit(".", 1)[0] if output else \
               context.label()
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
    return "\n".join(parts) + "\n"

# 'Image.plan()' prints straight to stdout, the same as every other 'seine'
# command -- captured rather than reimplemented, so a change to what a plan
# says needs changing in one place.
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

# What a build actually left behind: a stat-based listing of
# 'ContainerEngine.deploy_root()/<release>/' -- the same directory
# 'Image._output' names (the "would write:" line on Overview), read
# back rather than tracked separately.
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
            # 'Builder.current()' already only names a stamp that exists
            # on disk -- nothing here re-checks that.
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

# 'analyze.blame()'/'analyze.critical_chain()' print, same as
# 'Image.plan()' -- captured, not reimplemented, for the newest recorded
# run. 'ROOTFS SIZE' below is the one section that reads more than the
# newest run -- a single number says nothing about "did it grow".
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
        # Newest first, same order 'analyze.runs()' already returns --
        # older runs made before this field existed just have nothing to
        # add here, not a gap reported as zero.
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

# 'CacheCmd.info()' prints too -- not spec-scoped, a cache is shared by
# every build, so this ignores 'context' on purpose. 'matching' (a regex)
# narrows the listing the same way 'seine cache info --entries-matching'
# does, and expands a surviving package entry with what it was actually
# built from.
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

# Not spec-scoped -- jobs/theme/llm_* only; startup_commands has its own
# widget on the Settings screen. Unset shows the real fallback value, not
# a bare "(default)" -- llm_model/llm_api_base have no fallback, so
# '(unset)' is the honest word for those instead.
def render_settings():
    from seine import settings
    current = settings.load()
    jobs = str(current["jobs"]) if current["jobs"] is not None else "1 (default)"
    theme = current["theme"] or "dark (default)"
    llm_model = current["llm_model"] or "(unset)"
    llm_api_base = current["llm_api_base"] or "(unset)"
    return "\n".join(
        "%-16s %s" % (key, value) for key, value in
        [("jobs", jobs), ("theme", theme),
         ("llm_model", llm_model), ("llm_api_base", llm_api_base)]
    ) + "\n"

# Same "exactly one active group" restriction seine/tui/ai.py's own
# tools apply to a spec-scoped call -- multi-group specifications
# ('/use a -- b') aren't driven from any of these surfaces yet.
def _issues_build(context):
    if not context.active:
        return None, "no active specification -- '/use SPEC...' picks one\n"
    if len(context.builds) != 1:
        return None, ("multi-group specifications ('/use a -- b') aren't "
                      "supported here yet -- '/use' a single one\n")
    return context.builds[0], None

# The active build's own SBOM path, or the "not built yet" message --
# shared by both render_issues_* below so the two panes never disagree
# about why there is nothing to show.
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

# Reads whatever render_issues_table() above just left cached rather
# than scanning again -- called after it in IssuesScreen.update_body(),
# so a '/issues --rescan' has already refreshed the cache this reads
# back by the time this runs. 'rescan' is deliberately not repeated
# here, not because a fresh scan is unwanted for this pane too.
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
