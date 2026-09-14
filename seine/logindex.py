# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# A shared catalog of what task logs exist and which spec/release/arch
# they belong to -- the log directory's own key (a digest of spec file
# paths) can't tell that apart. Advisory only: never fails a build.

import json
import os
import time

from seine.container import ContainerEngine
from seine.utils import digest, locked

INDEX_FILE = "index.json"

# Only recent runs matter to a developer looking for "did this just
# work"; a separate constant from analyze.py's own KEEP.
KEEP = 20

def _path():
    return os.path.join(ContainerEngine.logs_root(), INDEX_FILE)

# Log paths are stored relative to logs_root(), never absolute -- an
# absolute path bakes in a machine-specific $SEINE_LOG_DIR and breaks
# once the log directory moves.
def _relative_log(path):
    if not path:
        return path
    if os.path.isabs(path):
        return os.path.relpath(path, ContainerEngine.logs_root())
    return path

# The reverse: an index.json-relative log path back to something
# open()-able. Absolute values pass through, so old entries still resolve.
def resolve(path):
    if not path:
        return path
    if os.path.isabs(path):
        return path
    return os.path.join(ContainerEngine.logs_root(), path)

# One entry dict, shared by begin()/record() below.
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

# Announce a run just starting, so the TUI's Logs: section has
# something to link before the build finishes. Returns the 'started'
# stamp to pass back to record(), or None when 'logs' is falsy.
def begin(files, release, arch, logs, tasks):
    if not logs:
        return None
    started = time.time()
    _append(_entry(files, release, arch, logs,
                   [dict(t, log=_relative_log(t.get("log"))) for t in tasks],
                   None, started))
    return started

# One entry per completed run, 'digest' scoped to this group's own
# 'files'. When 'started' is begin()'s handle, the in-progress entry
# is updated in place instead of appended twice. No-op if 'logs' is falsy.
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

# Every recorded entry, newest first. No release/arch filter here --
# callers (the TUI) filter for themselves.
def entries():
    try:
        with open(_path()) as f:
            index = json.load(f)
    except (OSError, ValueError):
        return []
    return list(reversed(index))
