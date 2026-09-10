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
# that actually started, or were skipped as a cache hit). A no-op if
# 'logs' is falsy -- nothing was written to disk to catalog.
def record(files, release, arch, logs, tasks, ok):
    if not logs:
        return
    entry = {
        "digest": digest(files, 8),
        "release": release,
        "arch": arch,
        "started": time.time(),
        "dir": os.path.relpath(logs, ContainerEngine.logs_root()),
        "ok": ok,
        "tasks": tasks,
    }
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
