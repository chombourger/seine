# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# A thin client for snapshot.debian.org's machine-usable API
# (https://salsa.debian.org/snapshot-team/snapshot/raw/master/API), used
# by 'seine vendor --refresh' alone (see vendor.py's own
# '_enrich_for_lock()') to find a permanent download URL for a source's
# or binary's own file once the live archive has moved past the exact
# version a lock has pinned -- and to record that URL, not to query it
# again: an ordinary 'seine vendor' run (no '--refresh') never talks to
# this module's API at all, it only reads what '--refresh' already
# wrote onto the lock, then downloads the file directly -- no apt, no
# container, since a snapshot URL is a plain, permanent HTTPS download.

import hashlib
import os
import time

import requests

BASE_URL = "https://snapshot.debian.org"

# A public, unauthenticated mirror -- a transient failure (connection
# reset, 429, a 5xx) is common at the volume '--refresh' calls this at
# and should not by itself cost a source or binary its snapshot record.
GET_ATTEMPTS = 3
GET_BACKOFF = 2

# Archives snapshot.debian.org tracks separately, in the precedence a
# real Debian install would give them -- a name+version pinned on more
# than one (rare, but real: a security update reusing a base suite's own
# version number) resolves to whichever comes first here.
ARCHIVE_ORDER = ["debian", "debian-security", "debian-debug", "debian-ports"]

USER_AGENT = "seine-vendor (+https://github.com/chombourger/seine)"

class SnapshotError(Exception):
    pass

def session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s

def _get(sess, path):
    last = None
    for attempt in range(GET_ATTEMPTS):
        if attempt > 0:
            time.sleep(GET_BACKOFF * attempt)
        try:
            response = sess.get(BASE_URL + path, timeout=30)
        except requests.exceptions.RequestException as e:
            last = SnapshotError(str(e))
            continue
        if response.status_code == 404:
            return None
        if response.status_code == 429 or response.status_code >= 500:
            last = SnapshotError("HTTP %d" % response.status_code)
            continue
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise SnapshotError(str(e))
        try:
            return response.json()
        except ValueError as e:
            raise SnapshotError(str(e))
    raise last

def _archive_priority(name):
    return ARCHIVE_ORDER.index(name) if name in ARCHIVE_ORDER else len(ARCHIVE_ORDER)

# The one permanent, hash-addressed download URL for any file
# snapshot.debian.org has ever seen -- independent of which dated
# archive run first captured it, so a caller only ever needs to keep
# 'sha1' and the filename around (both already known wherever this
# matters: vendor.py's own lock keeps the former, a source's 'files'
# or a binary's own name/arch/version already gives the latter),
# never the URL itself.
def file_url(sha1, filename):
    return "%s/file/%s/%s" % (BASE_URL, sha1, filename)

# Every file snapshot.debian.org knows for a source package's own
# version, as {filename: [(sha1, url), ...]}, each filename's own list
# sorted by archive precedence. 'sha1' is snapshot's own declared
# checksum for the file -- the whole point of exposing it rather than
# just the url: a caller that already has the file (an ordinary
# '--refresh', fetched through apt) can cross-check against an
# independent source for the price of a metadata lookup, no second
# download. 'url' is the hash-addressed '/file/<sha1>/<name>' endpoint,
# permanent regardless of which dated archive run first captured it, so
# nothing about *when* it was seen needs recording, only that it was.
#
# Every candidate is kept, not collapsed to the single highest-priority
# one: 'golang-github-grpc-ecosystem-go-grpc-middleware_1.3.0-1' is a
# real example of the same archive_name ('debian') carrying three
# different uploads under the exact same version string (a maintainer
# re-uploading without a version bump) -- ARCHIVE_ORDER alone cannot
# tell those apart, only the caller's own already-known hash can, by
# trying each candidate in turn. Collapsing here (an earlier version of
# this function did) meant an arbitrary one of the three was compared
# against, and the two that did not happen to match logged a false
# "does not match snapshot.debian.org" warning over a file that was
# genuinely there, just under a different one of this filename's own
# entries.
#
# Empty, not an error, if snapshot.debian.org has never heard of this
# name/version at all (see this module's own docstring) -- 'seine
# vendor --refresh' treats that as "nothing to record", not a failure.
def source_files(sess, name, version):
    data = _get(sess, "/mr/package/%s/%s/srcfiles?fileinfo=1" % (name, version))
    if data is None:
        return {}
    fileinfo = data.get("fileinfo", {})
    files = {}
    for result in data.get("result", []):
        h = result["hash"]
        for info in fileinfo.get(h, []):
            fname = info["name"]
            files.setdefault(fname, []).append(
                (_archive_priority(info["archive_name"]), h, file_url(h, fname)))
    return {fname: [(h, url) for _, h, url in sorted(candidates)]
           for fname, candidates in files.items()}

# The same, for one binary package's own version -- resolved through its
# owning source (name/version), which is what 'binfiles' needs to
# disambiguate a binary name that more than one source could plausibly
# have produced. Unlike source_files(), the API's own 'result' entries
# already carry the architecture, one query covering every one of them
# at once -- so this returns every arch's own candidate hash list in one
# round trip, as {arch: [sha1, ...]}, rather than taking a single arch
# and being called again per arch a caller cares about (only the sha1
# is ever compared against a locally fetched file's own hash; the url
# every caller would otherwise reconstruct anyway via file_url() once
# it knows which sha1 actually matched). Empty if snapshot.debian.org
# has nothing for this exact source/binary/version.
def binary_files(sess, srcname, srcversion, binname, binversion):
    path = "/mr/package/%s/%s/binfiles/%s/%s?fileinfo=1" % (
        srcname, srcversion, binname, binversion)
    data = _get(sess, path)
    if data is None:
        return {}
    by_arch = {}
    for result in data.get("result", []):
        by_arch.setdefault(result["architecture"], []).append(result["hash"])
    return by_arch

# Downloads 'url' straight to 'dest_path' (a plain HTTPS GET, no apt, no
# container -- see this module's own docstring) and returns the sha256
# of what was written, for the caller to check against whatever it
# already expects (the lock's own recorded hash, or -- at '--refresh'
# time -- what apt itself just fetched). Written through a temporary
# file and renamed into place, same as every other atomic write in this
# codebase (save_manifest(), save_lock()): a caller must never see a
# partially-written file at 'dest_path'.
def download(sess, url, dest_path):
    try:
        response = sess.get(url, timeout=120)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise SnapshotError(str(e))
    temporary = "%s.snapshot-tmp" % dest_path
    digest = hashlib.sha256()
    with open(temporary, "wb") as f:
        for chunk in response.iter_content(chunk_size=1 << 20):
            digest.update(chunk)
            f.write(chunk)
    os.replace(temporary, dest_path)
    return digest.hexdigest()
