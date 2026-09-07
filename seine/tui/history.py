# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The prompt's history, oldest first, so Up/Down walks it like a shell.
# Kept under the current directory, not a user-wide state dir, since a
# build session's spec files are project-specific. SEINE_HISTORY_FILE
# moves it, same as SEINE_CACHE_DIR moves the caches.
#
# Every entry carries a scope ('commands', 'chat', 'target', ...): what
# kind of line it is, not which screen typed it. entries(scopes) is how
# a screen's Prompt (base.py) asks for its own filtered, merged,
# chronological recall -- e.g. the target screen wants
# {'commands', 'target'}, everywhere else wants {'commands', 'chat'}.
# The recall cursor lives on Prompt itself, not here, since different
# screens recall different scope sets at once.

import json
import os
import time

def path():
    return os.environ.get("SEINE_HISTORY_FILE") or os.path.join(".seine", "history.json")

# Read by _prune_after() below, and by '/set history_pruning' to
# validate a new value before it's saved.
DEFAULT_PRUNE_AFTER = 30 * 86400  # 30 days, in seconds

# Turns a '/set history_pruning' value into a prune-after age in
# seconds, or None for "never prune". '0' is the explicit opt-out,
# distinct from an unset setting (None) defaulting to
# DEFAULT_PRUNE_AFTER. Raises ValueError on anything else.
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
        # scope -> list of {"line", "ts"}, growable via add_side()
        # below, never read from or written to 'self.where'.
        # side_load()/side_unload() is how a caller (TargetScreen) gets
        # its own recall without a word of it ever reaching disk, e.g.
        # a password typed at a console login prompt.
        self._side = {}

    # Persisted entries (in the given scopes) plus every matching
    # side-loaded group, oldest first: what a Prompt's recall cursor
    # (base.py) walks.
    def entries(self, scopes):
        pool = [e for e in self.lines if e.get("scope") in scopes]
        for scope, group in self._side.items():
            if scope in scopes:
                pool.extend(group)
        pool.sort(key=lambda e: e["ts"])
        return pool

    # The line entries(scopes) would show first on Up: a convenience
    # for callers that just want to know what recall currently holds.
    def last(self, scopes):
        entries = self.entries(scopes)
        return entries[-1]["line"] if entries else None

    # Resets (or starts) an empty in-memory record set under 'scope'.
    # Not additive: a second side_load() of the same scope drops
    # whatever it held.
    def side_load(self, scope):
        self._side[scope] = []

    # A no-op if 'scope' was never side-loaded.
    def side_unload(self, scope):
        self._side.pop(scope, None)

    # Auto-creates 'scope' if add_side() is called without a
    # side_load() first, rather than raising.
    def add_side(self, scope, line):
        self._side.setdefault(scope, []).append({"line": line, "ts": time.time()})

    # Anything short of a JSON array of {"line", "ts", "scope"} objects
    # is read as no history rather than an error, same as any other
    # stale/foreign content here.
    def _load(self):
        try:
            with open(self.where) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        if not isinstance(data, list) or not all(
                isinstance(e, dict) and "line" in e and "ts" in e and "scope" in e
                for e in data):
            return []
        return data

    # A stored value bad enough to raise falls back to the default age
    # rather than taking history down with it.
    def _prune_after(self):
        from seine import settings
        try:
            return parse_prune_after(settings.load()["history_pruning"])
        except ValueError:
            return DEFAULT_PRUNE_AFTER

    # Pure: callers decide whether the result is worth a write. Runs
    # on every load and on every add().
    def _prune(self):
        after = self._prune_after()
        if after is None:
            return False
        cutoff = time.time() - after
        before = len(self.lines)
        self.lines = [e for e in self.lines if e["ts"] >= cutoff]
        return len(self.lines) != before

    # Rewritten whole rather than appended: a JSON array has no line
    # to append to, and a session's history is small enough not to matter.
    def add(self, line, scope="commands"):
        self.lines.append({"line": line, "ts": time.time(), "scope": scope})
        self._prune()
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
