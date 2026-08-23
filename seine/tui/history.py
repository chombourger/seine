# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The prompt's command history, oldest first, so Up/Down walks it like a
# shell. Kept under the current directory, not a user-wide state dir --
# a build session's spec files are project-specific.

import json
import os
import time

def path():
    return os.path.join(".seine", "history.json")

# Read by _prune_after() below, and by '/set history_pruning' (commands.py)
# to validate a new value before it's saved.
DEFAULT_PRUNE_AFTER = 30 * 86400  # 30 days, in seconds

# Turns a '/set history_pruning' value into a prune-after age in seconds,
# or None for "never prune" -- '0' is the explicit opt-out, distinct from
# an unset setting (None here) defaulting to DEFAULT_PRUNE_AFTER instead.
# Raises ValueError on anything else, same as int() would -- '/set' turns
# that into a CommandError, and a bad value already saved falls back to
# the default rather than breaking history entirely (see _prune_after()).
def parse_prune_after(value):
    if value is None:
        return DEFAULT_PRUNE_AFTER
    value = value.strip()
    if value.endswith("d"):
        value = value[:-1]
    if value == "0":
        return None
    try:
        days = int(value)
    except ValueError:
        days = 0  # falls through to the same message as '0d'/'-5'
    if days <= 0:
        raise ValueError(
            "history_pruning expects 'Nd' (N days) or '0' for never, not '%s'" % value)
    return days * 86400

class History:
    def __init__(self, where=None):
        self.where = where or path()
        self.lines = self._load()
        if self._prune():
            self._write()
        self.at = len(self.lines)

    # Anything short of a JSON array of {"line", "ts"} objects is no
    # history rather than an error -- same "unreadable counts as empty"
    # rule 'seine.build.recall()' already follows for a lost baseline.
    # No migration from the older plain-string-array format: it simply
    # doesn't match this shape, so it is read as empty, same as any
    # other stale/foreign content here.
    def _load(self):
        try:
            with open(self.where) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        if not isinstance(data, list) or not all(
                isinstance(e, dict) and "line" in e and "ts" in e for e in data):
            return []
        return data

    # A stored value bad enough to raise (hand-edited settings.json, a
    # future/older seine disagreeing on the format) falls back to the
    # default age rather than taking history down with it.
    def _prune_after(self):
        from seine import settings
        try:
            return parse_prune_after(settings.load()["history_pruning"])
        except ValueError:
            return DEFAULT_PRUNE_AFTER

    # Pure -- callers decide whether the result is worth a write. Runs
    # on every load (so a long-idle history is already pruned once
    # opened, not just after the next add()) and on every add().
    def _prune(self):
        after = self._prune_after()
        if after is None:
            return False
        cutoff = time.time() - after
        before = len(self.lines)
        self.lines = [e for e in self.lines if e["ts"] >= cutoff]
        return len(self.lines) != before

    # Rewritten whole rather than appended -- a JSON array has no line to
    # append to, and a session's history is small enough to not matter.
    def add(self, line):
        self.lines.append({"line": line, "ts": time.time()})
        self._prune()
        self.at = len(self.lines)
        self._write()

    def _write(self):
        try:
            os.makedirs(os.path.dirname(self.where), exist_ok=True)
            temporary = "%s.new" % self.where
            with open(temporary, "w") as f:
                json.dump(self.lines, f)
            os.replace(temporary, self.where)
        except OSError:
            pass

    def prev(self):
        if self.at > 0:
            self.at -= 1
        return self.lines[self.at]["line"] if self.at < len(self.lines) else None

    def next(self):
        if self.at < len(self.lines):
            self.at += 1
        return self.lines[self.at]["line"] if self.at < len(self.lines) else ""
