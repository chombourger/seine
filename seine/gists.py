# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Reusable spec fragments, kept outside any one project's source tree so
# they travel between projects -- same XDG lookup shape as settings.py's
# XDG_CONFIG_HOME, but XDG_DATA_HOME: this is user content meant to
# persist and be reused, not a regenerable setting.

import getopt
import os
import re
import sys

from seine.cmd import Cmd

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

USAGE = """
Usage:
  seine gist ls
  seine gist show NAME
  seine gist rm NAME

List, show, or remove reusable spec fragments kept outside any one
project's source tree (%s, or SEINE_GISTS_DIR).

Description:
  A gist is a plain spec fragment -- side-loadable in 'seine tui'
  exactly like any other file -- with a one-line '# description'
  comment as its first line. Creating one is the AI chat's own
  'gist-create' tool; by hand, any '.yaml' file dropped into the
  directory above becomes a gist named after itself.
""" % default_dir()

class GistCmd(Cmd):
    def main(self, argv):
        try:
            opts, args = getopt.gnu_getopt(argv, "h", ["help"])
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, USAGE))
            sys.exit(1)
        for o, a in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                sys.exit()

        ACTIONS = ["ls", "show", "rm"]
        if len(args) == 0:
            sys.stderr.write("error: gist command expects one of %s\n"
                             % ", ".join(ACTIONS))
            sys.exit(1)
        action, rest = args[0], args[1:]
        if action not in ACTIONS:
            sys.stderr.write("error: unknown gist action '%s'\n" % action)
            sys.exit(1)

        if action == "ls":
            self._ls()
        else:
            if len(rest) != 1:
                sys.stderr.write("error: gist %s expects one NAME\n" % action)
                sys.exit(1)
            try:
                if action == "show":
                    sys.stdout.write(read(rest[0]))
                else:
                    delete(rest[0])
            except (OSError, ValueError) as e:
                sys.stderr.write("error: %s\n" % e)
                sys.exit(1)

    def _ls(self):
        found = list_gists()
        if not found:
            print("no gists yet -- %s" % default_dir())
            return
        width = max(len(name) for name, _ in found)
        for name, description in found:
            print("%-*s  %s" % (width, name, description))
