# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# User settings: the TUI's /set and startup_commands (seine/tui/
# settings.py), and a jobs default the plain CLI picks up too
# (BuildCmd.__init__, seine/build.py). One flat JSON file, XDG-located,
# same tolerant-read/atomic-write shape as seine.cache_index.Index.

import json
import os

# Every default is "do nothing different" -- None means no override.
# llm_model unset means the AI chat (seine/tui/ai.py) is off.
DEFAULTS = {"jobs": None, "theme": None, "startup_commands": [],
           "llm_model": None, "llm_api_base": None}

def default_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "seine", "settings.json")

# A missing, unreadable or non-object file is settings with nothing set,
# not an error. Unknown keys still come back in the merged dict, so
# save() never drops what this version doesn't understand.
def load(path=None):
    try:
        with open(path or default_path()) as f:
            recorded = json.load(f)
    except (OSError, ValueError):
        recorded = {}
    if not isinstance(recorded, dict):
        recorded = {}
    merged = dict(DEFAULTS)
    merged.update(recorded)
    return merged

# Written beside itself and moved into place, same as Index._write() --
# a reader never sees half of a write.
def save(settings, path=None):
    path = path or default_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = "%s.new" % path
    with open(temporary, "w") as f:
        json.dump(settings, f, indent=1, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
