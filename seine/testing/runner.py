# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Loads 'files' the same way 'seine build' does (BuildCmd -- 'requires:',
# '[[ ]]' variables, several files on one command line, all of it), reads
# the merged specification's own 'test:' section, and runs it. Reports
# the outcome two ways: through the same seine.reporter.Reporter every
# build already reports through (progress.Display on the command line,
# TextualReporter in the TUI -- neither needed a line of new code to work
# with tests), and as Robot's own output.xml, kept for anyone wanting
# Robot's richer log.html/rebot tooling rather than seine's own summary.

import os
import time

from seine.testing import context as ctx
from seine.testing import loader

# Shared by 'seine test', '/test', and the AI chat's 'run-test' tool --
# the same logs root a multi-group build's own logs land under
# (multiconfig.py's own _logs()), one timestamped directory per run.
def default_outdir():
    from seine.utils import ContainerEngine
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

# Bridges Robot's own listener callbacks onto seine.reporter.Reporter --
# 'started(name)'/'finished(name, failed=)' per test (not per keyword: a
# test is the unit a build's own Task already reports at), 'say(text)'
# for anything else worth a line while it runs.
class _Listener:
    def __init__(self, reporter, outcomes):
        self.reporter = reporter
        self.outcomes = outcomes
        self._started = {}
        # The test currently running, for log_message() below to
        # attribute a line to -- None outside any test (suite setup,
        # library import), where there is no single test to blame it on.
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

    # Every message Robot's own log level lets through (INFO by
    # default) reaches 'output' (optional, a no-op if unsupported);
    # only FAIL/WARN also become the transient 'say' status line.
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
# and requires a valid 'image:' section for that, which a fragment
# contributing only a 'test:' entry has no reason to carry.
# 'requires:'/'[[ ]]' are both already resolved by load_all() alone;
# 'Build Image' (ImageLibrary) runs its own full parse() when needed.
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
# made, in order -- so a person (or a CI job) has one place to start a
# post-mortem from without already knowing which test produced which
# screenshot, or that a console.log even exists. Robot's own output.xml
# already has the full keyword-by-keyword trace; this doesn't repeat
# it, only what output.xml has no notion of.
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
