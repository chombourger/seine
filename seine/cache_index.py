# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import json
import os
import sqlite3
import threading
import time

from seine.container import ContainerEngine

# What seine has cached, when it was made and when it was last used.
#
# Filesystem atime is unreliable ('relatime'/'noatime' mounts, and podman
# tracks no last-used time at all), so seine keeps its own record.
#
# Advisory only: no build decision reads it (rebuild/staleness/currency
# checks still use their own stamps), so a lost or stale index just makes
# a worse report, never a wrong build -- a missing entry is never an error.
#
# SQLite + JSON metadata column: scales, indexed on used/kind, no
# whole-file rewrite per step.
INDEX = "index.db"

# Kinds of cached object. 'downloads' isn't a per-object cache: apt picks
# .debs from the archive cache itself, so the release is the unit recorded
# there.
CHROOT = "chroot"
IMAGE = "image"
PACKAGE = "package"
DOWNLOADS = "downloads"
# One entry per artifact a 'vendor:' section pinned (unlike DOWNLOADS,
# which only names the release as a whole) -- so a superseded version
# ages out of 'seine cache clear vendor' on its own.
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
            # fail. Nothing to migrate, just a bad file to drop.
            try:
                os.unlink(self._path)
            except OSError:
                pass
            return self._connect()

    # Single entry lookup: None if not present or on error. Same merged
    # shape as entries() (made/used/uses + metadata keys).
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

    # Merges patch into existing metadata and bumps used (hit), creating
    # the entry if missing. Unlike hit(metadata=...), keeps other keys.
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

    # Compatibility shims for old JSON-backed internals (tests/cache/
    # cache.py's `aged` helper) -- not public API, kept working via sqlite.
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
                # made/used/uses are real columns -- a metadata key with the
                # same name would be unreadable on read (column wins), so
                # drop it here.
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

    # What an export carries: entry identity + made time only, nothing
    # about this machine's use -- a use count/last-used from elsewhere
    # would skew this machine's own eviction sweep.
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

    # The import side: a carried entry has been used by nobody here, so
    # it's last-used now, used zero times. Only 'made' carries over --
    # the one thing this machine can't work out itself.
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

# What this build reused vs made, counted where the index is written so
# count and record can't disagree. Steps run beside each other, hence
# the lock.
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

# Logged only in verbose mode -- the index already records cache
# decisions, this just says them out loud.
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
