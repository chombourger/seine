# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import json
import os
import sqlite3
import threading
import time

from seine.utils import ContainerEngine

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
# SQLite with a JSON metadata column: scales to many entries, indexed
# on used/kind, no whole-file rewrite per step, and arbitrary data can
# be attached to an entry via the metadata column.
INDEX = "index.db"

# The kinds of thing recorded, which are the caches that hold objects a
# build takes one at a time. 'downloads' is not among them: which .deb apt
# took out of the archive cache is decided by apt inside the container, so
# the honest unit there is the release, recorded as its own entry.
CHROOT = "chroot"
IMAGE = "image"
PACKAGE = "package"
DOWNLOADS = "downloads"
# One entry per artifact a 'vendor:' section pinned -- unlike DOWNLOADS,
# which only ever names the release as a whole (apt decides which .deb it
# takes out of that cache): a vendor's own repository is what 'seine
# vendor' built, and what it built is worth naming down to the file, so a
# superseded version ages out of 'seine cache clear vendor' on its own.
VENDOR = "vendor"

class Index:
    def __init__(self, path=None):
        self._path = path or ContainerEngine.cache(INDEX)

    def _connect(self):
        conn = sqlite3.connect(self._path, timeout=5.0,
                               isolation_level=None,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cache_entries (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                made INTEGER,
                used INTEGER,
                uses INTEGER,
                metadata TEXT,
                PRIMARY KEY (kind, key)
            ) STRICT""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_used ON cache_entries(used)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kind ON cache_entries(kind)")
        return conn

    def _conn(self):
        d = os.path.dirname(self._path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
        try:
            return self._connect()
        except sqlite3.DatabaseError:
            # Corrupt store -- advisory index, so start fresh rather than
            # fail. index.db is a different file from the old index.json,
            # so there is nothing to migrate here, just a bad file to drop.
            try:
                os.unlink(self._path)
            except OSError:
                pass
            return self._connect()

    # Single entry lookup, advisory: None if not present or on error.
    # Returns the same merged dict as entries() would (made/used/uses +
    # metadata keys), or None.
    def get(self, kind, key):
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return None
        try:
            cur = conn.execute(
                "SELECT made, used, uses, metadata FROM cache_entries WHERE kind=? AND key=?",
                (kind, key))
            row = cur.fetchone()
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            return None
        try:
            conn.close()
        except Exception:
            pass
        if row is None:
            return None
        made, used, uses, metadata = row
        entry = {}
        if made is not None:
            entry["made"] = made
        if used is not None:
            entry["used"] = used
        if uses is not None:
            entry["uses"] = uses
        if metadata:
            try:
                meta = json.loads(metadata)
                if isinstance(meta, dict):
                    for k, v in meta.items():
                        if k not in entry:
                            entry[k] = v
                else:
                    entry["metadata"] = meta
            except (ValueError, TypeError):
                pass
        return entry

    # Merge patch into metadata for (kind, key) and bump used (hit).
    # If no entry exists, creates one via hit. Preserves other metadata
    # keys, unlike hit(..., metadata=...) which replaces wholesale.
    def patch(self, kind, key, patch):
        if not isinstance(patch, dict):
            patch = {"metadata": patch}
        # strip column names if caller passed them inside patch
        patch = {k: v for k, v in patch.items() if k not in ("made", "used", "uses")}
        if not patch:
            return self.hit(kind, key)
        existing = self.get(kind, key)
        base = {}
        if existing is not None:
            base = {k: v for k, v in existing.items() if k not in ("made", "used", "uses")}
        base.update(patch)
        return self.hit(kind, key, metadata=base)

    # Everything recorded, oldest use first, as (kind, key, entry). Entries
    # whose object has gone are dropped as they are read: a cache someone
    # cleared with rm -rf is a cache seine should stop talking about.
    def entries(self, present=None):
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return []
        try:
            cur = conn.execute(
                "SELECT kind, key, made, used, uses, metadata FROM cache_entries")
            rows = cur.fetchall()
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            return []
        try:
            conn.close()
        except Exception:
            pass
        listed = []
        for kind, key, made, used, uses, metadata in rows:
            if present is not None and present(kind, key) is False:
                continue
            entry = {}
            if made is not None:
                entry["made"] = made
            if used is not None:
                entry["used"] = used
            if uses is not None:
                entry["uses"] = uses
            if metadata:
                try:
                    meta = json.loads(metadata)
                    if isinstance(meta, dict):
                        for k, v in meta.items():
                            if k not in entry:
                                entry[k] = v
                    else:
                        entry["metadata"] = meta
                except (ValueError, TypeError):
                    pass
            listed.append((kind, key, entry))
        return sorted(listed, key=lambda e: e[2].get("used") or 0)

    # Compatibility shims for tests and any code reaching into the old
    # JSON-backed internals. Not part of the public API but kept so
    # existing callers (tests/cache/cache.py's `aged` helper) continue to
    # work without change. Implemented in terms of the sqlite store.
    def _read(self):
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return {}
        try:
            cur = conn.execute(
                "SELECT kind, key, made, used, uses, metadata FROM cache_entries")
            rows = cur.fetchall()
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            return {}
        try:
            conn.close()
        except Exception:
            pass
        recorded = {}
        for kind, key, made, used, uses, metadata in rows:
            entry = {}
            if made is not None:
                entry["made"] = made
            if used is not None:
                entry["used"] = used
            if uses is not None:
                entry["uses"] = uses
            if metadata:
                try:
                    meta = json.loads(metadata)
                    if isinstance(meta, dict):
                        for k, v in meta.items():
                            if k not in entry:
                                entry[k] = v
                    else:
                        entry["metadata"] = meta
                except (ValueError, TypeError):
                    pass
            recorded.setdefault(kind, {})[key] = entry
        return recorded

    def _write(self, recorded):
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM cache_entries")
            for kind, keys in (recorded or {}).items():
                if not isinstance(keys, dict):
                    continue
                for key, entry in keys.items():
                    if not isinstance(entry, dict):
                        continue
                    made = entry.get("made")
                    used = entry.get("used")
                    uses = entry.get("uses")
                    meta = {k: v for k, v in entry.items()
                            if k not in ("made", "used", "uses")}
                    meta_json = json.dumps(meta) if meta else None
                    conn.execute(
                        "INSERT INTO cache_entries (kind, key, made, used, uses, metadata) VALUES (?,?,?,?,?,?)",
                        (kind, key, made, used, uses, meta_json))
            conn.execute("COMMIT")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # A build took this one and found it already there.
    def hit(self, kind, key, metadata=None):
        return self._touch(kind, key, made=False, metadata=metadata)

    # A build had to make it.
    def made(self, kind, key, metadata=None):
        return self._touch(kind, key, made=True, metadata=metadata)

    def _touch(self, kind, key, made, metadata=None):
        counted(kind, made)
        now = int(time.time())
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return {"made": now, "used": now, "uses": 0 if made else 1}
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "SELECT made, used, uses, metadata FROM cache_entries WHERE kind=? AND key=?",
                (kind, key))
            row = cur.fetchone()
            if row is None:
                made_val = now
                used_val = now
                uses_val = 0 if made else 1
                # made/used/uses are the real columns; a metadata key of
                # the same name would be unreadable back (column wins on
                # read), so it is dropped here rather than stored and lost.
                stored = {k: v for k, v in metadata.items()
                          if k not in ("made", "used", "uses")} \
                    if isinstance(metadata, dict) else metadata
                meta_json = None
                if stored is not None:
                    try:
                        meta_json = json.dumps(stored)
                    except (ValueError, TypeError):
                        meta_json = None
                conn.execute(
                    "INSERT INTO cache_entries (kind, key, made, used, uses, metadata) VALUES (?,?,?,?,?,?)",
                    (kind, key, made_val, used_val, uses_val, meta_json))
                entry = {"made": made_val, "used": used_val, "uses": uses_val}
                if isinstance(stored, dict):
                    entry.update(stored)
                elif stored is not None:
                    entry["metadata"] = stored
            else:
                old_made, old_used, old_uses, old_meta = row
                if made or old_made is None:
                    new_made = now
                    new_uses_base = 0
                else:
                    new_made = old_made
                    new_uses_base = old_uses if old_uses is not None else 0
                new_used = now
                new_uses = new_uses_base + (0 if made else 1)
                if metadata is not None:
                    stored = {k: v for k, v in metadata.items()
                              if k not in ("made", "used", "uses")} \
                        if isinstance(metadata, dict) else metadata
                    try:
                        new_meta_json = json.dumps(stored)
                    except (ValueError, TypeError):
                        new_meta_json = old_meta
                else:
                    new_meta_json = old_meta
                conn.execute(
                    "UPDATE cache_entries SET made=?, used=?, uses=?, metadata=? WHERE kind=? AND key=?",
                    (new_made, new_used, new_uses, new_meta_json, kind, key))
                entry = {"made": new_made, "used": new_used, "uses": new_uses}
                if new_meta_json:
                    try:
                        meta = json.loads(new_meta_json)
                        if isinstance(meta, dict):
                            entry.update(meta)
                        else:
                            entry["metadata"] = meta
                    except (ValueError, TypeError):
                        pass
            conn.execute("COMMIT")
        except sqlite3.Error:
            # Advisory index: losing a write to lock contention or a
            # transient error is a worse report, never a worse build.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            entry = {"made": now, "used": now, "uses": 0 if made else 1}
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return entry

    # What an export carries: what each entry is and when it was made, and
    # nothing about this machine's use of it. A count of how often someone
    # else reached for a chroot says nothing about the machine reading it,
    # and a last-used time from over there would have the first eviction
    # sweep deleting on another machine's history.
    def stripped(self):
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return {}
        try:
            cur = conn.execute("SELECT kind, key, made FROM cache_entries")
            rows = cur.fetchall()
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            return {}
        try:
            conn.close()
        except Exception:
            pass
        result = {}
        for kind, key, made in rows:
            result.setdefault(kind, {})[key] = {"made": made}
        return result

    # The other side of that: what arrives has been used by nobody here, so
    # it is last-used now -- it arrived now -- and used no times. What it
    # keeps is when it was made, which is the one thing the other machine
    # knew and this one cannot work out.
    def merge(self, carried):
        if not carried:
            return
        now = int(time.time())
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            for kind, keys in carried.items():
                for key, entry in keys.items():
                    made_val = None
                    extra_meta = {}
                    if isinstance(entry, dict):
                        made_val = entry.get("made")
                        # Any extra keys beyond made/used/uses become metadata
                        extra_meta = {k: v for k, v in entry.items()
                                      if k not in ("made", "used", "uses")}
                    if made_val is None:
                        made_val = now
                    cur = conn.execute(
                        "SELECT uses, metadata FROM cache_entries WHERE kind=? AND key=?",
                        (kind, key))
                    row = cur.fetchone()
                    if row is None:
                        meta_json = json.dumps(extra_meta) if extra_meta else None
                        conn.execute(
                            "INSERT INTO cache_entries (kind, key, made, used, uses, metadata) VALUES (?,?,?,?,?,?)",
                            (kind, key, made_val, now, 0, meta_json))
                    else:
                        old_uses, old_meta = row
                        new_uses = old_uses if old_uses is not None else 0
                        # Preserve existing metadata unless carried has extra
                        if extra_meta:
                            # Merge with existing if both dicts
                            merged = {}
                            if old_meta:
                                try:
                                    old_dict = json.loads(old_meta)
                                    if isinstance(old_dict, dict):
                                        merged.update(old_dict)
                                except (ValueError, TypeError):
                                    pass
                            merged.update(extra_meta)
                            new_meta_json = json.dumps(merged)
                        else:
                            new_meta_json = old_meta
                        conn.execute(
                            "UPDATE cache_entries SET made=?, used=?, uses=?, metadata=? WHERE kind=? AND key=?",
                            (made_val, now, new_uses, new_meta_json, kind, key))
            conn.execute("COMMIT")
        except sqlite3.Error:
            # Advisory index: a carried entry that can't be merged in is
            # simply not merged, not a failed build.
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # A whole kind, one entry of one, or the lot.
    def forget(self, kind=None, key=None):
        try:
            conn = self._conn()
        except (OSError, sqlite3.Error):
            return
        try:
            if kind is None:
                conn.execute("DELETE FROM cache_entries")
            elif key is None:
                conn.execute("DELETE FROM cache_entries WHERE kind=?", (kind,))
            else:
                conn.execute("DELETE FROM cache_entries WHERE kind=? AND key=?", (kind, key))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

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
