# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Reusable spec fragments, kept outside any one project's source tree so
# they travel between projects -- same XDG lookup shape as settings.py's
# XDG_CONFIG_HOME, but XDG_DATA_HOME: this is user content meant to
# persist and be reused, not a regenerable setting.

import os
import re

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

def default_dir():
    override = os.environ.get("SEINE_GISTS_DIR")
    if override:
        return override
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "seine", "gists")

def path_for(name, directory=None):
    if not NAME_RE.match(name):
        raise ValueError("'%s' is not a usable gist name -- kebab-case, "
                         "letters/digits only" % name)
    return os.path.join(directory or default_dir(), "%s.yaml" % name)

# A gist's description is its first line, a plain YAML comment --
# already ignored by any YAML parser, so the file stays a normal,
# directly side-loadable fragment; no sidecar index to fall out of sync.
def _description(path):
    try:
        with open(path) as f:
            first = f.readline()
    except OSError:
        return None
    first = first.rstrip("\n")
    return first[2:] if first.startswith("# ") else ""

# (name, description) pairs, sorted by name -- empty, not an error, if
# the directory doesn't exist yet (nobody has created a gist).
def list_gists(directory=None):
    directory = directory or default_dir()
    if not os.path.isdir(directory):
        return []
    names = sorted(n[:-5] for n in os.listdir(directory) if n.endswith(".yaml"))
    return [(name, _description(os.path.join(directory, "%s.yaml" % name)))
           for name in names]

def read(name, directory=None):
    with open(path_for(name, directory)) as f:
        return f.read()

def create(name, description, content, directory=None):
    directory = directory or default_dir()
    path = path_for(name, directory)
    if os.path.exists(path):
        raise ValueError("gist '%s' already exists" % name)
    os.makedirs(directory, exist_ok=True)
    body = content if content.endswith("\n") else content + "\n"
    with open(path, "w") as f:
        f.write("# %s\n%s" % (description, body))
    return path

def delete(name, directory=None):
    path = path_for(name, directory)
    if not os.path.isfile(path):
        raise ValueError("no such gist '%s'" % name)
    os.remove(path)
