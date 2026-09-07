# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Thin client for snapshot.debian.org's API, used by 'seine vendor
# --refresh' to find a permanent download URL once the live archive
# has moved past a pinned version. A plain 'seine vendor' run never
# calls this API; it just downloads the URL '--refresh' recorded.

import hashlib
import os
import time

import requests

BASE_URL = "https://snapshot.debian.org"

# Retry on transient failures (connection reset, 429, 5xx).
GET_ATTEMPTS = 3
GET_BACKOFF = 2

# Precedence for archives sharing a name+version (e.g. a security
# update reusing a base suite's version).
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

# Permanent hash-addressed download URL for any file snapshot.debian.org
# has seen, independent of which dated archive first captured it.
def file_url(sha1, filename):
    return "%s/file/%s/%s" % (BASE_URL, sha1, filename)

# Every file known for a source package's version, as
# {filename: [(sha1, url), ...]}, sorted by archive precedence. All
# candidates are kept (not just the top-priority one): the same
# archive can carry more than one upload under the same version
# string, and only the caller's already-known hash can tell them
# apart. Returns {} if snapshot.debian.org has nothing for this
# name/version.
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

# Same idea for a binary package, resolved through its owning source
# (name/version), needed since the API keys binfiles that way. Returns
# every arch's candidate hashes in one call: {arch: [sha1, ...]}.
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

# Downloads 'url' to 'dest_path' and returns the sha256 of what was
# written, for the caller to check against an expected hash. Written
# through a temp file and renamed into place, so 'dest_path' never
# shows a partial file.
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
