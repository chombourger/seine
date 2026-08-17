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
import time

from seine import analyze
from seine import packages
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

# The SBOM filename 'seine/sbom.py' writes to: '<image>-sbom.spdx.json',
# derived here without an 'SBOM' instance (that class ties the name to
# its own 'options["sbom"]' flag, which this has no reason to carry) --
# same suffix-stripping rule as 'SBOM._output_file()'.
def _sbom_path(image_output):
    path = image_output
    if path.endswith(".img"):
        path = path[:-len(".img")]
    return path + "-sbom.spdx.json"

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
        sbom_path = _sbom_path(output) if output else None
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
# 'Image.plan()' -- captured, not reimplemented. The newest recorded run
# only: history beyond that is what 'seine analyze' itself is for.
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
        sections.append(text)
    return "\n\n".join(sections)

# 'CacheCmd.info()' prints too -- not spec-scoped, a cache is shared by
# every build, so this ignores 'context' on purpose.
def render_cache():
    from seine.cache import CACHES, CacheCmd
    return _captured(lambda: CacheCmd().info(list(CACHES.keys()), entries=True))

def render_doctor(pull=False):
    from seine import doctor
    return doctor.render(doctor.run(pull=pull))
