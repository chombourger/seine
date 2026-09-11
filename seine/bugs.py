# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Package-level defects for an SBOM's source packages, via UDD's own
# bugs search (https://udd.debian.org/bugs/) in JSON form -- one bulk
# HTTP request per scan, not one per package. debsbom already reports
# source package names (seine/sbom.py), which is what the BTS files
# bugs against, so the SBOM's own names go straight into the query;
# no binary-to-source mapping is needed (UDD's own 'bin2src' option
# answers '500 Internal Server Error' regardless of release).
#
# Only bugs that likely matter are kept by default: severity at or
# above 'important' (critical/grave/serious/important), not done, not
# merged away. The UDD 'release' parameter scopes 'affects_*' to the
# build's own release (trixie, bookworm, ...).

import json
import os
import time
import urllib.parse
import urllib.request
from collections import Counter
from typing import NamedTuple

# One defect: a BTS bug against a single source package. 'package' is
# the binary package the bug was filed against (usually the same name,
# occasionally 'src:foo'); 'source' is what it rolls up under.
class Bug(NamedTuple):
    id: int
    source: str
    package: str
    severity: str
    title: str
    status: str
    last_modified: str

# UDD's own bugs search, JSON form. 'dmd=1' enables the package-selection
# section the 'packages' field lives in; 'done=ign'/'merged=ign' drop
# closed bugs and merged duplicates server-side; 'cseverity=1' asks the
# severity back (not shown by default). No 'rc'/'allbugs' type flag: those
# select site-wide sets unioned with the packages, not a filter on them.
UDD_BUGS_URL = "https://udd.debian.org/bugs/"

# Most to least severe, the BTS's own scale. Anything below 'important'
# (normal/minor/wishlist) is noise for an image triage -- filter_bugs()
# drops it unless asked otherwise.
SEVERITY_ORDER = ["critical", "grave", "serious", "important",
                  "normal", "minor", "wishlist"]
DEFAULT_MIN_SEVERITY = "important"

# UDD only knows some releases by name; anything else (a local fork,
# say) falls back to 'any' rather than silently querying forky, the
# form's own default.
UDD_RELEASES = ("forky", "sid", "trixie", "bookworm", "bullseye")

# One request's package list stays well under typical URL length
# limits; an SBOM with hundreds of packages goes out as several.
CHUNK_SIZE = 40

# A cached scan younger than this is reused, same role as secscan's
# mtime check but time-based: UDD has no revision to compare against,
# and a day-old defect list is still a good triage.
CACHE_TTL = 24 * 3600

USER_AGENT = "seine-bugs"

def cache_path(sbom_path):
    return sbom_path + ".bugs.json"

# Source package names straight from the SBOM debsbom wrote -- sorted,
# deduplicated, no empty entries.
def sources_from_sbom(sbom_path):
    with open(sbom_path) as f:
        spdx = json.load(f)
    return sorted({entry.get("name", "") for entry in spdx.get("packages", [])} - {""})

# One UDD query URL for a chunk of sources. 'distro' maps to UDD's
# 'release' (the 'affects_*' scope); unknown distros query 'any'.
def _query_url(sources, distro):
    release = distro if distro in UDD_RELEASES else "any"
    query = urllib.parse.urlencode([
        ("release", release),
        ("dmd", "1"),
        ("packages", " ".join(sources)),
        ("done", "ign"),
        ("merged", "ign"),
        ("cseverity", "1"),
        ("format", "json"),
    ])
    return "%s?%s" % (UDD_BUGS_URL, query)

# Split out so tests can stub the network without touching the cache,
# filter, or stats logic around it.
def _fetch_url(url, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")

def _parse_entry(entry):
    try:
        bug_id = int(entry.get("id"))
    except (TypeError, ValueError):
        return None
    source = entry.get("source") or ""
    if not source:
        return None
    return Bug(id=bug_id,
               source=source,
               package=entry.get("package", ""),
               severity=entry.get("severity", ""),
               title=entry.get("title", ""),
               status=entry.get("status", ""),
               last_modified=entry.get("last_modified", ""))

# Every chunk's bugs concatenated; one entry per bug id (chunks can
# overlap when a bug's source spans chunk boundaries -- it can't, but
# cheap to guarantee).
def fetch(sources, distro=None):
    seen = {}
    sources = sorted(set(sources))
    for at in range(0, len(sources), CHUNK_SIZE):
        raw = _fetch_url(_query_url(sources[at:at + CHUNK_SIZE], distro))
        try:
            entries = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            bug = _parse_entry(entry)
            if bug is not None:
                seen[bug.id] = bug
    return sorted(seen.values(), key=lambda bug: bug.id)

# Kept public so callers that must never trigger a real fetch (e.g. the
# TUI's read-only stats pane) can read the cache without calling scan().
def read_cache(sbom_path):
    path = cache_path(sbom_path)
    try:
        with open(path) as f:
            recorded = json.load(f)
        if time.time() - recorded.get("fetched_at", 0) > CACHE_TTL:
            return None
        return [Bug(**entry) for entry in recorded.get("bugs", [])]
    except (OSError, ValueError, TypeError):
        return None

def _write_cache(sbom_path, bugs):
    path = cache_path(sbom_path)
    temporary = "%s.new" % path
    with open(temporary, "w") as f:
        json.dump({"fetched_at": time.time(), "bugs": [bug._asdict() for bug in bugs]}, f)
    os.replace(temporary, path)

# Fetches every source the SBOM names in bulk, using a fresh cache
# instead unless 'rescan' is set. Network errors propagate to the
# caller -- a stale/empty defect list must never read as "no bugs".
def scan(sbom_path, distro=None, rescan=False):
    sbom_path = os.path.realpath(sbom_path)
    if not rescan:
        cached = read_cache(sbom_path)
        if cached is not None:
            return cached
    bugs = fetch(sources_from_sbom(sbom_path), distro=distro)
    _write_cache(sbom_path, bugs)
    return bugs

# Aggregate counts for a caller to render however it likes.
def stats(bugs):
    return {
        "total": len(bugs),
        "sources": len({bug.source for bug in bugs}),
        "by_severity": Counter(bug.severity for bug in bugs),
        "by_status": Counter(bug.status for bug in bugs),
        "by_source": Counter(bug.source for bug in bugs),
    }

# 'source' narrows by source package name (substring, case-insensitive);
# 'min_severity' drops anything less severe, per SEVERITY_ORDER --
# defaulting to DEFAULT_MIN_SEVERITY, the "likely matters" cut. A bug
# with a severity not in that list is dropped rather than guessed into
# a bucket.
def filter_bugs(bugs, source=None, min_severity=DEFAULT_MIN_SEVERITY):
    if min_severity is not None:
        if min_severity not in SEVERITY_ORDER:
            raise ValueError("'min_severity' must be one of %s, not '%s'" %
                             (", ".join(SEVERITY_ORDER), min_severity))
        cutoff = SEVERITY_ORDER.index(min_severity)
        bugs = [b for b in bugs if b.severity in SEVERITY_ORDER
                and SEVERITY_ORDER.index(b.severity) <= cutoff]
    if source:
        needle = source.lower()
        bugs = [b for b in bugs if needle in b.source.lower()]
    return bugs
