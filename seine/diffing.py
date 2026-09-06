# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Diffing one specification's plan against the last real build of it:
# the on-disk baseline a build leaves behind (recall()/remember()),
# and the colored, YAML-shaped diff BuildCmd prints from it -- split
# out of seine/build.py, which grew too large to navigate.

import difflib
import os
import shutil
import sys
import yaml

from seine.container import ContainerEngine
from seine.utils import digest


# Full-width bars rather than a one-character marker: what a specification
# gained and lost is read off the shape of the block. Green for a gained
# line, red for a lost one.
ADDED   = "\x1b[48;5;22m\x1b[97m"
REMOVED = "\x1b[48;5;52m\x1b[97m"
RESET   = "\x1b[0m"

# Where the last build's specification is kept.
def _baseline(files):
    return os.path.join(ContainerEngine.cache("plans"),
                        "%s.yml" % digest(files))

# What these files last built, or None. Unreadable counts as never built: a
# plan is still worth printing without a baseline.
def recall(files):
    try:
        with open(_baseline(files)) as f:
            return f.read()
    except OSError:
        return None

def remember(files, text):
    path = _baseline(files)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    except OSError:
        pass

def _bar(color, text, width):
    return "%s%s%s" % (color, text.ljust(width) if len(text) < width else text,
                       RESET)

# One 'name: value' and everything under it. Rendered setting by setting
# rather than by yaml.dump on the whole document, since every line needs a
# mark of its own.
def _pair(name, value, indent):
    pad = "  " * indent
    if isinstance(value, dict) and len(value) > 0:
        return [pad + "%s:" % name] + _body(value, indent + 1)
    if isinstance(value, list) and len(value) > 0:
        # Items indented under their key, which YAML allows: folded context
        # can then say which list a change is in.
        return [pad + "%s:" % name] + _items(value, indent + 2)
    return [pad + line for line in
            yaml.dump({name: value}, default_flow_style=False).splitlines()]

def _body(mapping, indent):
    lines = []
    for name in sorted(mapping):
        lines += _pair(name, mapping[name], indent)
    return lines

def _items(values, indent):
    lines = []
    for value in values:
        lines += _item(value, indent)
    return lines

def _item(value, indent):
    if isinstance(value, dict) and len(value) > 0:
        return _dashed(_body(value, indent), indent)
    if isinstance(value, list) and len(value) > 0:
        return _dashed(_items(value, indent), indent)
    return ["  " * (indent - 1) + "- " +
            yaml.dump(value, default_flow_style=False).splitlines()[0]]

# The item's '-' on its first line, where a YAML reader looks for it.
def _dashed(lines, indent):
    return ["  " * (indent - 1) + "- " + lines[0].lstrip()] + lines[1:]

def _dashed_marks(marked, indent):
    mark, text = marked[0]
    if mark == " ":
        return [(" ", "  " * (indent - 1) + "- " + text.lstrip())] + marked[1:]
    # The item did not change, its contents did: keep the '-' on an unmarked
    # line of its own, or it reads as the whole item added or removed.
    return [(" ", "  " * (indent - 1) + "-")] + marked

def _marked(mark, lines):
    return [(mark, line) for line in lines]

# What a list item goes by, so a partition whose size changed reads as that
# partition changed rather than one gone and another arrived.
#
# ponytail: hard-coded keys, not something the sections declare. An item
# nothing matches prints as one removed and one added, which is still true
# -- add the section's own key here if that reads badly for it.
NAMES = ("name", "label", "filename", "where", "suite", "package")

def _named(item, others):
    if isinstance(item, dict) == False:
        return None
    for key in NAMES:
        if key in item:
            for other in others:
                if isinstance(other, dict) and other.get(key) == item[key]:
                    return other
    return None

# Compared setting by setting rather than line by line: a line diff of YAML
# calls an indentation that shifted a change.
def _changes(old, new, indent=0):
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in new:
            lines += _marked("-", _pair(name, old[name], indent))
        elif name not in old:
            lines += _marked("+", _pair(name, new[name], indent))
        elif old[name] == new[name]:
            lines += _marked(" ", _pair(name, new[name], indent))
        elif isinstance(old[name], dict) and isinstance(new[name], dict):
            lines.append((" ", "  " * indent + "%s:" % name))
            lines += _changes(old[name], new[name], indent + 1)
        elif isinstance(old[name], list) and isinstance(new[name], list):
            lines.append((" ", "  " * indent + "%s:" % name))
            lines += _listed(old[name], new[name], indent + 2)
        else:
            lines += _marked("-", _pair(name, old[name], indent))
            lines += _marked("+", _pair(name, new[name], indent))
    return lines

# Two lists, in the new one's order. Equal items match first, the rest by
# the name they go by (NAMES) and are then compared as two of the same
# thing.
def _listed(old, new, indent):
    # repr because dicts cannot be hashed. Equal dicts have equal reprs
    # here: they came out of yaml, which orders their keys the same way.
    matcher = difflib.SequenceMatcher(None, [repr(item) for item in old],
                                      [repr(item) for item in new])
    lines = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for item in new[j1:j2]:
                lines += _marked(" ", _item(item, indent))
            continue
        gone, matched = list(old[i1:i2]), {}
        for n, item in enumerate(new[j1:j2]):
            match = _named(item, gone)
            if match is not None:
                gone.remove(match)
                matched[n] = match
        # Removals before additions, so a replacement reads old then new.
        for item in gone:
            lines += _marked("-", _item(item, indent))
        for n, item in enumerate(new[j1:j2]):
            if n not in matched:
                lines += _marked("+", _item(item, indent))
            else:
                lines += _dashed_marks(_changes(matched[n], item, indent),
                                       indent)
    return lines

def _depth(text):
    return len(text) - len(text.lstrip())

# How much of what did not change is printed around what did.
CONTEXT = 3

# A specification is hundreds of lines and a change to it is a few, so what
# did not change is folded away -- keeping the lines around a change and the
# keys it sits under, so it still reads as a place in the specification.
# With nothing changed there is nothing to fold around, so all of it stays.
def _folded(lines, context=CONTEXT):
    changed = [i for i, (mark, _) in enumerate(lines) if mark != " "]
    if len(changed) == 0:
        return lines
    keep = set()
    for i in changed:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    # An added 'size' is worth little without the partition it is in.
    for i in sorted(keep):
        depth = _depth(lines[i][1])
        for j in range(i - 1, -1, -1):
            if depth == 0:
                break
            if _depth(lines[j][1]) < depth:
                keep.add(j)
                depth = _depth(lines[j][1])
    # Folding one or two lines costs a line to say so, and saves nothing.
    run = []
    for i in range(len(lines)):
        if i not in keep:
            run.append(i)
            continue
        if 0 < len(run) <= 2:
            keep.update(run)
        run = []
    folded, hidden = [], 0
    for i, line in enumerate(lines):
        if i not in keep:
            hidden += 1
            continue
        if hidden > 0:
            folded.append((" ", "%s... (%d line%s unchanged)"
                           % (" " * _depth(line[1]), hidden,
                              "" if hidden == 1 else "s")))
            hidden = 0
        folded.append(line)
    if hidden > 0:
        folded.append((" ", "... (%d line%s unchanged)"
                       % (hidden, "" if hidden == 1 else "s")))
    return folded

# The new specification with what changed since the baseline marked, and
# the rest folded away. Coloured for a terminal, a '+' or a '-' in column
# one for a pipe or a file.
def diff(old, new, color=True, width=None):
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    after = yaml.safe_load(new) or {}
    # No baseline means nothing to say about what changed, not that every
    # line is new.
    try:
        before = (yaml.safe_load(old) or {}) if old else after
    except yaml.YAMLError:
        before = after
    lines = []
    for mark, text in _folded(_changes(before, after)):
        # Marked even when coloured: a bar is lost in a paste, and not
        # everyone can tell the two colours apart.
        text = mark + text
        if color == False:
            lines.append(text.rstrip())
        elif mark == "+":
            lines.append(_bar(ADDED, text, width))
        elif mark == "-":
            lines.append(_bar(REMOVED, text, width))
        else:
            lines.append(text)
    return "\n".join(lines) + "\n"

# Colour for a terminal and nothing else. NO_COLOR is the convention;
# '--no-color' is what someone reaches for without knowing it.
def colorless(options, stream=None):
    if options.get("color") == False:
        return True
    stream = stream or sys.stdout
    return os.environ.get("NO_COLOR") is not None or stream.isatty() == False
