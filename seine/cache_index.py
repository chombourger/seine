# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import json
import os
import threading
import time

from seine.utils import ContainerEngine, locked

# What seine has cached, when it was made and when it was last used.
#
# The filesystem knows what is in a cache but not when it was last wanted:
# atime would say, except that a machine mounted 'relatime' answers to the
# day and one mounted 'noatime' does not answer at all, and podman does not
# record a last-used time for an image under any mount options. So seine
# keeps its own record.
#
# The index is advisory and nothing decides a build from it. Whether a
# package needs rebuilding is still its stamp, whether a chroot is stale is
# still the digest beside it, and whether an image is current is still its
# label. That is what keeps a lost, stale or truncated index a matter of a
# less useful report rather than a wrong build -- and it is why a missing
# entry is never an error here.
#
# One file, rewritten whole under a lock. There are tens of entries, not
# thousands, and it is written once per step of a build.
INDEX = "index.json"

# The kinds of thing recorded, which are the caches that hold objects a
# build takes one at a time. 'downloads' is not among them: which .deb apt
# took out of the archive cache is decided by apt inside the container, so
# the honest unit there is the release, recorded as its own entry.
CHROOT = "chroot"
IMAGE = "image"
PACKAGE = "package"
DOWNLOADS = "downloads"

class Index:
    def __init__(self, path=None):
        self._path = path or ContainerEngine.cache(INDEX)

    # Everything recorded, oldest use first, as (kind, key, entry). Entries
    # whose object has gone are dropped as they are read: a cache someone
    # cleared with rm -rf is a cache seine should stop talking about.
    def entries(self, present=None):
        recorded = self._read()
        listed = []
        for kind, keys in sorted(recorded.items()):
            for key, entry in keys.items():
                if present is not None and present(kind, key) == False:
                    continue
                listed.append((kind, key, entry))
        return sorted(listed, key=lambda e: e[2].get("used") or 0)

    # A build took this one and found it already there.
    def hit(self, kind, key):
        return self._touch(kind, key, made=False)

    # A build had to make it.
    def made(self, kind, key):
        return self._touch(kind, key, made=True)

    def _touch(self, kind, key, made):
        counted(kind, made)
        now = int(time.time())
        with locked(self._path):
            recorded = self._read()
            entry = recorded.setdefault(kind, {}).setdefault(key, {})
            if made or entry.get("made") is None:
                entry["made"] = now
                entry["uses"] = 0
            entry["used"] = now
            entry["uses"] = (entry.get("uses") or 0) + (0 if made else 1)
            self._write(recorded)
        return entry

    # What an export carries: what each entry is and when it was made, and
    # nothing about this machine's use of it. A count of how often someone
    # else reached for a chroot says nothing about the machine reading it,
    # and a last-used time from over there would have the first eviction
    # sweep deleting on another machine's history.
    def stripped(self):
        recorded = self._read()
        return {kind: {key: {"made": entry.get("made")}
                       for key, entry in keys.items()}
                for kind, keys in recorded.items()}

    # The other side of that: what arrives has been used by nobody here, so
    # it is last-used now -- it arrived now -- and used no times. What it
    # keeps is when it was made, which is the one thing the other machine
    # knew and this one cannot work out.
    def merge(self, carried):
        now = int(time.time())
        with locked(self._path):
            recorded = self._read()
            for kind, keys in (carried or {}).items():
                for key, entry in keys.items():
                    mine = recorded.setdefault(kind, {}).setdefault(key, {})
                    mine["made"] = entry.get("made") or now
                    mine["used"] = now
                    mine["uses"] = mine.get("uses") or 0
            self._write(recorded)

    # A whole kind, one entry of one, or the lot.
    def forget(self, kind=None, key=None):
        with locked(self._path):
            recorded = self._read()
            if kind is None:
                recorded = {}
            elif key is None:
                recorded.pop(kind, None)
            else:
                recorded.get(kind, {}).pop(key, None)
                if len(recorded.get(kind, {})) == 0:
                    recorded.pop(kind, None)
            self._write(recorded)

    # A file that is not there, not readable or not JSON is an index with
    # nothing in it: this is a record of what happened, not a record
    # anything depends on.
    def _read(self):
        try:
            with open(self._path, "r") as f:
                recorded = json.load(f)
        except (OSError, ValueError):
            return {}
        return recorded if type(recorded) == type({}) else {}

    # Written beside itself and moved into place, so a reader either sees
    # the index as it was or as it is, and a build interrupted mid-write
    # leaves neither half.
    def _write(self, recorded):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        temporary = "%s.new" % self._path
        with open(temporary, "w") as f:
            json.dump(recorded, f, indent=1, sort_keys=True)
        os.replace(temporary, self._path)

# What this build reused and what it made, counted where the index is
# written so the count and the record cannot disagree. A build's steps run
# beside each other, hence the lock.
_counted = {}
_counting = threading.Lock()

def counted(kind, made):
    with _counting:
        seen = _counted.setdefault(kind, {"made": 0, "reused": 0})
        seen["made" if made else "reused"] += 1

# The line a build ends with, or nothing when it decided nothing -- a build
# that reused and made nothing has no cache to report on.
def summary():
    with _counting:
        if len(_counted) == 0:
            return None
        said = []
        for what in ["reused", "made"]:
            counts = ["%d %s" % (seen[what], plural(kind, seen[what]))
                      for kind, seen in sorted(_counted.items()) if seen[what] > 0]
            said.append("%s: %s" % (what, ", ".join(counts) if counts else "nothing"))
        # A plain separator: this goes wherever a build's output goes, and
        # that is not always a terminal that can carry more.
        return "; ".join(said)

def plural(kind, count):
    if count == 1 or kind.endswith("s"):
        return kind
    return "%ss" % kind

# A line for the log when someone asked to see what a build is doing. The
# index is where a build's cache decisions are already written down, so this
# is where they can be said out loud.
def say(options, message):
    if (options or {}).get("verbose"):
        print("cache: %s" % message)

# A span of time as someone types it: '30d', '6h', '2w'. Days by default,
# since that is the unit a cache is thought about in.
def span(said):
    units = {"h": 3600, "d": 86400, "w": 604800}
    seconds = units.get(said[-1:])
    number = said[:-1] if seconds else said
    try:
        count = int(number)
    except ValueError:
        raise ValueError("'%s' is not a length of time, try 30d, 6h or 2w"
                         % said)
    if count < 1:
        raise ValueError("a length of time has to be at least 1")
    return count * (seconds or units["d"])

# How long ago, in the roughest terms that are still useful for deciding
# what to delete.
def since(when, now=None):
    if when is None:
        return "never"
    seconds = max(0, (now or int(time.time())) - when)
    for length, unit in [(86400, "d"), (3600, "h"), (60, "m")]:
        if seconds >= length:
            return "%d%s ago" % (seconds // length, unit)
    return "just now"
