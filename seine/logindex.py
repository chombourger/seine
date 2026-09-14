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

# One entry per completed run (once per group for a multiconfig run).
# 'digest' is scoped to this group's own 'files' so a TUI showing one
# group matches only its own entries. No-op if 'logs' is falsy.
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

# Every recorded entry, newest first. No release/arch filter here --
# callers (the TUI) filter for themselves.
def entries():
    try:
        with open(_path()) as f:
            index = json.load(f)
    except (OSError, ValueError):
        return []
    return list(reversed(index))
