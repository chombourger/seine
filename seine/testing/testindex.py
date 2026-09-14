# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Catalogs test runs like logindex.py catalogs builds, so the TUI
# overview pane can show last-run/all-runs without parsing every
# output.xml. Advisory only -- a failure to write never fails a run.

import os
import time

from seine.container import ContainerEngine
from seine.utils import digest, locked

INDEX_RELPATH = os.path.join("tests", "index.json")

# Same shape/reasoning as logindex.KEEP: entries are small, and only
# recent runs matter to a developer asking "did this just work".
KEEP = 20


def _path():
    return os.path.join(ContainerEngine.logs_root(), INDEX_RELPATH)


# Only runs under logs_root() are indexed -- a custom --outdir
# elsewhere has no portable form, and indexing it would pollute
# the real catalog with every test-suite run.
def _inside_root(outdir):
    absolute = os.path.abspath(outdir)
    root = os.path.abspath(ContainerEngine.logs_root())
    try:
        relative = os.path.relpath(absolute, root)
    except ValueError:
        return False
    return not relative.startswith("..")


# The run directory stored relative to logs_root(), so index.json
# stays portable when the log directory moves.
def _stored_dir(outdir):
    return os.path.relpath(os.path.abspath(outdir),
                           os.path.abspath(ContainerEngine.logs_root()))


# The reverse: a stored dir back to something open()-able. Absolute
# values pass through untouched, like logindex.resolve().
def resolve(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(ContainerEngine.logs_root(), path)


def _entry(files, outdir, outcomes, started, ok):
    tests = []
    for outcome in outcomes or []:
        if isinstance(outcome, dict):
            name = outcome.get("name")
            status = outcome.get("status")
            elapsed = outcome.get("elapsed")
        else:
            name = getattr(outcome, "name", None)
            status = getattr(outcome, "status", None)
            elapsed = getattr(outcome, "elapsed", None)
        tests.append({"name": name, "status": status, "elapsed": elapsed})
    if ok is None:
        ok = all(t["status"] != "FAIL" for t in tests)
    return {
        "digest": digest(files, 8),
        "started": started,
        "dir": _stored_dir(outdir),
        "ok": ok,
        "tests": tests,
    }


def _append(entry):
    path = _path()
    try:
        with locked(path):
            try:
                import json
                with open(path) as f:
                    index = json.load(f)
            except (OSError, ValueError):
                index = []
            index.append(entry)
            index = index[-KEEP:]
            temporary = "%s.new" % path
            import json
            with open(temporary, "w") as f:
                json.dump(index, f)
            os.replace(temporary, path)
    except OSError:
        pass


# No-op when 'outdir' is falsy (nothing on disk to catalog) or
# outside logs_root() (a custom --outdir, a test's temp dir).
def record(files, outdir, outcomes, started=None, ok=None):
    if not outdir:
        return
    if not _inside_root(outdir):
        return
    _append(_entry(files, outdir, outcomes,
                   started if started is not None else time.time(), ok))


# Every recorded entry, newest first. A record that fails to read
# (e.g. half-written) is just skipped, like analyze.runs().
def entries():
    import json
    try:
        with open(_path()) as f:
            index = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(index, list):
        return []
    return list(reversed(index))


# Matches the exact qualified name ('suite.case') first, falling
# back to the short name so a suite rename doesn't break lookup.
def runs_for(name, recorded=None):
    recorded = entries() if recorded is None else recorded
    short = name.rsplit(".", 1)[-1]
    found = []
    for entry in recorded:
        names = [(t.get("name") or "") for t in entry.get("tests", [])]
        if name in names:
            found.append(entry)
        elif any(n.rsplit(".", 1)[-1] == short for n in names):
            found.append(entry)
    return found


# One entry's row for 'name' (same matching as runs_for()), or None.
def status_of(entry, name):
    short = name.rsplit(".", 1)[-1]
    for test in entry.get("tests", []):
        if test.get("name") == name:
            return test
    for test in entry.get("tests", []):
        if (test.get("name") or "").rsplit(".", 1)[-1] == short:
            return test
    return None


# Path to the screencast replaying 'qualified' in the run 'entry'
# covers, or None. Prefers the test's own cast, falling back to
# the run-global console.cast.
def cast_for(entry, qualified):
    outdir = resolve(entry.get("dir"))
    if not outdir or not os.path.isdir(outdir):
        return None
    casts = {}
    try:
        import json
        with open(os.path.join(outdir, "interactions.json")) as f:
            data = json.load(f)
        casts = data.get("console_casts") or {}
    except (OSError, ValueError):
        casts = {}
    short = qualified.rsplit(".", 1)[-1]
    for name, basename in casts.items():
        if name == qualified or (name or "").rsplit(".", 1)[-1] == short:
            path = os.path.join(outdir, basename)
            if os.path.isfile(path):
                return path
    path = os.path.join(outdir, "console.cast")
    if os.path.isfile(path):
        return path
    return None


def _elapsed(status):
    try:
        return float(status.get("elapsed") or 0)
    except (TypeError, ValueError):
        return None


# Every run directory on disk holding an output.xml, newest first --
# used by rebuild() below, and directly by callers that want old runs
# without waiting for a rebuild.
def scan():
    import glob
    import xml.etree.ElementTree as ET
    pattern = os.path.join(ContainerEngine.logs_root(), "tests", "*", "output.xml")
    found = []
    for path in sorted(glob.glob(pattern)):
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        outdir = os.path.dirname(path)
        started = _started_of(root, outdir)
        tests = []
        for suite in root.iter("suite"):
            suite_name = suite.get("name")
            for node in suite.findall("test"):
                status = node.find("status")
                elapsed = _elapsed(status) if status is not None else None
                tests.append({
                    "name": "%s.%s" % (suite_name, node.get("name", "")),
                    "status": status.get("status") if status is not None else None,
                    "elapsed": elapsed,
                })
        ok = all(t["status"] != "FAIL" for t in tests)
        found.append({
            "digest": None,
            "started": started,
            "dir": _stored_dir(outdir),
            "ok": ok,
            "tests": tests,
        })
    return sorted(found, key=lambda e: e["started"], reverse=True)


def _started_of(root, outdir):
    generated = root.get("generated")
    if generated:
        try:
            import datetime
            moment = datetime.datetime.fromisoformat(generated)
            return moment.timestamp()
        except ValueError:
            pass
    try:
        return os.path.getmtime(os.path.join(outdir, "output.xml"))
    except OSError:
        return 0
