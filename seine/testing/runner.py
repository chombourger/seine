# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Loads 'files' the same way 'seine build' does (BuildCmd), reads the
# merged spec's 'test:' section, and runs it. Reports through
# seine.reporter.Reporter (progress.Display / TextualReporter) and as
# Robot's own output.xml, for anyone wanting log.html/rebot tooling.

import os
import time

from seine.testing import context as ctx
from seine.testing import loader

# Shared by 'seine test', '/test', and 'run-test': one timestamped
# directory per run, under the same logs root builds use.
def default_outdir():
    from seine.container import ContainerEngine
    base = os.path.join(ContainerEngine.logs_root(), "tests")
    return os.path.join(base, time.strftime("%Y%m%d-%H%M%S", time.gmtime()))

class TestOutcome:
    def __init__(self, name, suite, status, message, tags, elapsed):
        self.name = name
        self.suite = suite
        self.status = status    # "PASS" / "FAIL" / "SKIP"
        self.message = message
        self.tags = tags
        self.elapsed = elapsed

    @property
    def failed(self):
        return self.status == "FAIL"

class SuiteResult:
    def __init__(self, tests, output_xml):
        self.tests = tests
        self.output_xml = output_xml

    @property
    def ok(self):
        return all(not t.failed for t in self.tests)

    def summary(self):
        passed = sum(1 for t in self.tests if t.status == "PASS")
        failed = sum(1 for t in self.tests if t.status == "FAIL")
        skipped = sum(1 for t in self.tests if t.status == "SKIP")
        return "%d test%s, %d passed, %d failed, %d skipped" % (
            len(self.tests), "" if len(self.tests) == 1 else "s", passed, failed, skipped)

# Bridges Robot's listener callbacks onto seine.reporter.Reporter:
# 'started'/'finished' per test, 'say' for anything else worth a line.
class _Listener:
    def __init__(self, reporter, outcomes):
        self.reporter = reporter
        self.outcomes = outcomes
        self._started = {}
        # Test currently running, for log_message() to attribute a line
        # to; None outside any test (suite setup, library import).
        self._current = None

    def start_test(self, data, result):
        name = "%s.%s" % (result.parent.name, data.name)
        self._started[id(data)] = time.time()
        self._current = name
        if self.reporter:
            self.reporter.started(name)

    def end_test(self, data, result):
        name = "%s.%s" % (result.parent.name, data.name)
        started = self._started.pop(id(data), None)
        elapsed = time.time() - started if started else None
        self.outcomes.append(TestOutcome(
            name, result.parent.name, result.status, result.message,
            list(result.tags), elapsed))
        self._current = None
        if self.reporter:
            self.reporter.finished(name, failed=(result.status == "FAIL"))

    # Every message Robot's log level lets through (INFO by default)
    # reaches 'output'; only FAIL/WARN also become the 'say' status line.
    def log_message(self, message):
        if not self.reporter:
            return
        if message.level in ("FAIL", "WARN"):
            self.reporter.say(message.message)
        if self._current is not None:
            output = getattr(self.reporter, "output", None)
            if output is not None:
                output(self._current, "[%s] %s" % (message.level, message.message))

class NoTests(ValueError):
    pass

# load_all() only, not .parse(): the latter also resolves partitions
# and needs a valid 'image:' section, which a 'test:'-only fragment has
# no reason to carry. 'Build Image' (ImageLibrary) runs parse() itself
# when it actually needs to build.
def _load_spec(files):
    from seine.build import BuildCmd
    build = BuildCmd()
    build.options = dict(build.options, ansible_library=[])
    return build.load_all(files)

def run_spec(files, tags=None, outdir=None, reporter=None, dryrun=False, spec=None):
    if outdir is None:
        outdir = default_outdir()
    os.makedirs(outdir, exist_ok=True)

    if spec is None:
        spec = _load_spec(files)
    entries = spec.get("test") or []
    if not entries:
        raise NoTests(
            "%s has no 'test:' section -- nothing to run" % " ".join(files))

    with ctx.RunContext(spec=spec, spec_files=files, outdir=outdir) as context:
        suite = loader.compile(entries, context)
        if tags:
            suite.filter(included_tags=list(tags))

        outcomes = []
        output_xml = os.path.join(outdir, "output.xml")
        suite.run(output=output_xml, report=None, log=None,
                 stdout=open(os.devnull, "w"), dryrun=dryrun,
                 listener=[_Listener(reporter, outcomes), context])

        _write_interactions(context)

    return SuiteResult(outcomes, output_xml)

# One JSON file naming every real-hardware action and artifact this run
# made, in order, so a post-mortem has one place to start from. Robot's
# output.xml already has the full keyword trace; this adds only what
# output.xml has no notion of.
def _ensure_console_cast(context, path=None):
    cast = path or getattr(context, "console_cast_path", None)
    if not cast:
        return
    if os.path.isfile(cast):
        return
    import json as _json
    import time as _time
    from seine.tui.target import CONSOLE_COLUMNS, CONSOLE_LINES
    header = {
        "version": 2,
        "width": CONSOLE_COLUMNS,
        "height": CONSOLE_LINES,
        "timestamp": int(_time.time()),
        "env": {"TERM": "xterm-256color"},
    }
    with open(cast, "w", encoding="utf-8") as f:
        _json.dump(header, f)
        f.write("\n")


def _write_interactions(context):
    import json
    _ensure_console_cast(context)
    # Write a header-only cast for every per-test file that has none yet,
    # so a CI job sees one .cast per test even for a quiet run.
    for name in list(getattr(context, "console_casts", {}).keys()):
        cast = context.console_casts[name]
        if cast:
            _ensure_console_cast(context, cast)
    path = os.path.join(context.outdir, "interactions.json")
    console_log = context.console_log_path
    console_cast = getattr(context, "console_cast_path", None)
    console_casts = getattr(context, "console_casts", {}) or {}
    # Basename map for JSON -- keeps interactions.json portable
    cast_map = {k: os.path.basename(v)
                for k, v in console_casts.items()
                if v and os.path.isfile(v)}
    with open(path, "w") as f:
        json.dump({
            "console_log": (os.path.basename(console_log)
                            if console_log and os.path.isfile(console_log) else None),
            "console_cast": (os.path.basename(console_cast)
                             if console_cast and os.path.isfile(console_cast) else None),
            "console_casts": cast_map,
            "interactions": context.interactions,
        }, f, indent=2)
