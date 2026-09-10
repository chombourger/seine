# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# A shared, top-level catalog of what task logs exist and which spec/
# release/arch/run each belongs to -- distinct from analyze.py's own
# per-run records (spec-content-keyed, its own KEEP pruning, feeds
# timing/critical-chain analysis) and from cache_index.py (cache-object
# hit/made events, no run/status concept at all). Read by the TUI to
# show only the logs relevant to whichever spec is currently active:
# the log directory's own key (a digest of spec file *paths*, see
# Image._logs()) says nothing about release/arch, so two different
# arches built from an identical file set land in the same directory.
#
# Advisory only, like analyze.py's records: a failure to write here
# never fails a build.

import json
import os
import time

from seine.container import ContainerEngine
from seine.utils import digest, locked

INDEX_FILE = "index.json"

# Same shape/reasoning as analyze.py's own KEEP: records are small, and
# only recent runs matter to a developer looking for "did this just
# work". Not the same constant (different file, different pruning
# unit -- entries here, not per-digest directories there), kept equal
# for now purely because there's no reason yet to pick a different number.
KEEP = 20

def _path():
    return os.path.join(ContainerEngine.logs_root(), INDEX_FILE)

# Task log paths are stored relative to logs_root() (i.e. relative to
# index.json's own directory), never absolute -- an absolute path bakes
# in a machine-specific $SEINE_LOG_DIR and breaks the moment the log
# directory moves. Callers pass whatever they have (today: absolute
# os.path.join(logs, "<task>.log")); normalized here so every writer
# gets it right without thinking about it. Already-relative values
# pass through untouched; None (a cache-hit task with no log file)
# stays None.
def _relative_log(path):
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.relpath(path, ContainerEngine.logs_root())
    return path

# The reverse: an index.json-relative log path back to something
# open()-able. Absolute values pass through untouched, so entries
# written before relative paths existed still resolve.
def resolve(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(ContainerEngine.logs_root(), path)

# One entry dict, shared by begin()/record() below. 'ok' is None
# while the build is still running, True/False once it finished.
def _entry(files, release, arch, logs, tasks, ok, started):
    return {
        "digest": digest(files, 8),
        "release": release,
        "arch": arch,
        "started": started,
        "dir": os.path.relpath(logs, ContainerEngine.logs_root()),
        "ok": ok,
        "tasks": tasks,
    }

def _append(entry):
    path = _path()
    try:
        with locked(path):
            try:
                with open(path) as f:
                    index = json.load(f)
            except (OSError, ValueError):
                index = []
            index.append(entry)
            index = index[-KEEP:]
            temporary = "%s.new" % path
            with open(temporary, "w") as f:
                json.dump(index, f)
            os.replace(temporary, path)
    except OSError:
        pass

# Announce a run that is just starting, so index.json exists (and the
# TUI's Logs: section has something to link) while the build is still
# running -- not only after it finishes. 'tasks' is every planned
# task's {"name", "failed", "cached", "log"} dict (all failed False,
# none cached yet); the finishing record() call rewrites them with
# their real outcomes. Returns the entry's 'started' stamp, the handle
# the caller passes back to record() -- or None when 'logs' is falsy
# (nothing on disk to catalog, same no-op as record()).
def begin(files, release, arch, logs, tasks):
    if not logs:
        return None
    started = time.time()
    _append(_entry(files, release, arch, logs,
                   [dict(t, log=_relative_log(t.get("log"))) for t in tasks],
                   None, started))
    return started

# One entry per completed run (call once per group for a multiconfig
# run -- see multiconfig.py's caller). 'files' is the spec file list
# this run/group actually loaded, used only to compute 'digest': NOT
# necessarily the same digest the run's log *directory* is keyed by (a
# multiconfig run shares one directory, keyed by every group's files
# combined) -- each group's own entry still gets its own, narrower
# digest so a TUI showing one group can match just its own entries.
#
# 'tasks' is the caller's own list of {"name", "failed", "cached",
# "log"} dicts (mirroring analyze.record()'s 'ran' filter: only tasks
# that actually started, or were skipped as a cache hit) -- each task's
# 'log' is stored relative to logs_root() (see _relative_log()), so
# index.json never holds an absolute path. A no-op if
# 'logs' is falsy -- nothing was written to disk to catalog.
#
# When 'started' is the handle begin() returned, the in-progress entry
# it wrote is updated in place (matched on log dir + stamp, both
# unique per run) instead of appending a second entry. Without it --
# older call paths, tests -- the entry is appended as before.
def record(files, release, arch, logs, tasks, ok, started=None):
    if not logs:
        return
    tasks = [dict(t, log=_relative_log(t.get("log"))) for t in tasks]
    path = _path()
    try:
        with locked(path):
            try:
                with open(path) as f:
                    index = json.load(f)
            except (OSError, ValueError):
                index = []
            if started is not None:
                wanted = os.path.relpath(logs, ContainerEngine.logs_root())
                for entry in index:
                    if (entry.get("dir") == wanted
                            and entry.get("started") == started):
                        entry["tasks"] = tasks
                        entry["ok"] = ok
                        break
                else:
                    index.append(_entry(files, release, arch, logs, tasks,
                                        ok, started))
            else:
                index.append(_entry(files, release, arch, logs, tasks,
                                    ok, time.time()))
            index = index[-KEEP:]
            temporary = "%s.new" % path
            with open(temporary, "w") as f:
                json.dump(index, f)
            os.replace(temporary, path)
    except OSError:
        pass

# Every recorded entry, newest first. No 'spec'/'digest' filter at this
# layer (unlike analyze.runs()) -- callers (the TUI) filter by
# release/arch/task name themselves, since a raw digest match alone
# isn't enough to tell two same-file-set, different-arch runs apart.
def entries():
    try:
        with open(_path()) as f:
            index = json.load(f)
    except (OSError, ValueError):
        return []
    return list(reversed(index))
