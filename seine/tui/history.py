# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The prompt's command history, oldest first, so Up/Down walks it like a
# shell. Kept under the current directory, not a user-wide state dir --
# a build session's spec files are project-specific.

import json
import os

def path():
    return os.path.join(".seine", "history.json")

class History:
    def __init__(self, where=None):
        self.where = where or path()
        self.lines = self._load()
        self.at = len(self.lines)

    # Anything short of a JSON array of strings is no history rather
    # than an error -- the same "unreadable counts as empty" rule
    # 'seine.build.recall()' already follows for a lost baseline.
    def _load(self):
        try:
            with open(self.where) as f:
                lines = json.load(f)
        except (OSError, ValueError):
            return []
        return lines if isinstance(lines, list) else []

    # Rewritten whole rather than appended -- a JSON array has no line to
    # append to, and a session's history is small enough to not matter.
    def add(self, line):
        self.lines.append(line)
        self.at = len(self.lines)
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
        return self.lines[self.at] if self.at < len(self.lines) else None

    def next(self):
        if self.at < len(self.lines):
            self.at += 1
        return self.lines[self.at] if self.at < len(self.lines) else ""
