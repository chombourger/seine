#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import hashlib
import os
import shutil
import sys
import tempfile

import requests

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import snapshot

# A fake requests.Session -- 'get(url, timeout=...)' returns whatever
# 'responses' (keyed by the exact url this test set up) says, never
# touching a real network. Matches this module's own usage: one GET
# call per lookup, nothing to stream except in download()'s own test.
class FakeResponse:
    def __init__(self, status_code=200, json_body=None, content=b""):
        self.status_code = status_code
        self._json = json_body
        self.content = content
    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 404:
            raise requests.exceptions.HTTPError("HTTP %d" % self.status_code)
    def json(self):
        return self._json
    def iter_content(self, chunk_size=None):
        yield self.content

class FakeSession:
    # A url's own value may be a single FakeResponse (every call gets
    # the same one) or a list, popped one call at a time -- the shape
    # snapshot.py's own retry loop (_get()) needs to be exercised: one
    # call sees the earlier response, the next call the later one.
    def __init__(self, responses):
        self.responses = responses
        self.requested = []
    def get(self, url, timeout=None):
        self.requested.append(url)
        r = self.responses[url]
        if isinstance(r, list):
            return r.pop(0) if len(r) > 1 else r[0]
        return r

SRCFILES_URL = snapshot.BASE_URL + "/mr/package/bash/5.1-2/srcfiles?fileinfo=1"

# The real case this shape exists for: snapshot.debian.org's own
# 'golang-github-grpc-ecosystem-go-grpc-middleware_1.3.0-1' carries
# three separate uploads under the exact same version string, two of
# them sharing 'archive_name: debian' -- ARCHIVE_ORDER alone cannot
# tell those two apart, only the caller's own already-known hash can,
# by trying every candidate rather than just the one this function
# would otherwise have picked.
class SourceFilesKeepsEveryCandidatePerFilename(avocado.Test):
    def test(self):
        body = {
            "result": [{"hash": "aaa"}, {"hash": "ccc"}, {"hash": "bbb"}],
            "fileinfo": {
                "aaa": [
                    {"name": "bash_5.1-2.dsc", "archive_name": "debian-security",
                     "path": "/x", "size": 1, "first_seen": "20260615T083200Z"},
                ],
                # Same filename, same archive_name as no one else here,
                # but different bytes (a re-upload under one version).
                "ccc": [
                    {"name": "bash_5.1-2.dsc", "archive_name": "debian",
                     "path": "/y", "size": 1, "first_seen": "20260614T000000Z"},
                ],
                "bbb": [
                    {"name": "bash_5.1.orig.tar.xz", "archive_name": "debian",
                     "path": "/z", "size": 2, "first_seen": "20260614T000000Z"},
                ],
            },
        }
        sess = FakeSession({SRCFILES_URL: FakeResponse(json_body=body)})
        files = snapshot.source_files(sess, "bash", "5.1-2")
        # Both '.dsc' candidates survive, 'debian' (ccc) sorted ahead of
        # 'debian-security' (aaa) by ARCHIVE_ORDER -- neither is dropped.
        self.assertEqual(files["bash_5.1-2.dsc"],
                         [("ccc", snapshot.BASE_URL + "/file/ccc/bash_5.1-2.dsc"),
                          ("aaa", snapshot.BASE_URL + "/file/aaa/bash_5.1-2.dsc")])
        self.assertEqual(files["bash_5.1.orig.tar.xz"],
                         [("bbb", snapshot.BASE_URL + "/file/bbb/bash_5.1.orig.tar.xz")])

class SourceFilesEmptyWhenSnapshotHasNeverHeardOfIt(avocado.Test):
    def test(self):
        sess = FakeSession({SRCFILES_URL: FakeResponse(status_code=404)})
        self.assertEqual(snapshot.source_files(sess, "bash", "5.1-2"), {})

class BinaryFilesGroupsEveryArchitectureFromOneCall(avocado.Test):
    def test(self):
        url = (snapshot.BASE_URL +
              "/mr/package/bash/5.1-2/binfiles/bash/5.1-2?fileinfo=1")
        body = {
            "result": [{"hash": "aaa", "architecture": "amd64"},
                      {"hash": "bbb", "architecture": "arm64"}],
        }
        sess = FakeSession({url: FakeResponse(json_body=body)})
        found = snapshot.binary_files(sess, "bash", "5.1-2", "bash", "5.1-2")
        # One request covers every arch -- no second lookup for arm64.
        self.assertEqual(found, {"amd64": ["aaa"], "arm64": ["bbb"]})
        self.assertEqual(len(sess.requested), 1)
        self.assertEqual(found.get("armhf"), None)

class BinaryFilesEmptyWhenSnapshotHasNeverHeardOfIt(avocado.Test):
    def test(self):
        url = (snapshot.BASE_URL +
              "/mr/package/bash/5.1-2/binfiles/bash/5.1-2?fileinfo=1")
        sess = FakeSession({url: FakeResponse(status_code=404)})
        self.assertEqual(
            snapshot.binary_files(sess, "bash", "5.1-2", "bash", "5.1-2"), {})

class GetRaisesSnapshotErrorOnHttpFailure(avocado.Test):
    def test(self):
        real_backoff = snapshot.GET_BACKOFF
        snapshot.GET_BACKOFF = 0
        self.addCleanup(setattr, snapshot, "GET_BACKOFF", real_backoff)
        sess = FakeSession({SRCFILES_URL: FakeResponse(status_code=500)})
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.source_files(sess, "bash", "5.1-2")
        # Every attempt, not just the first, since a 5xx is treated as
        # transient (see _get()'s own comment).
        self.assertEqual(len(sess.requested), snapshot.GET_ATTEMPTS)

class GetRetriesATransientFailureThenSucceeds(avocado.Test):
    def test(self):
        real_backoff = snapshot.GET_BACKOFF
        snapshot.GET_BACKOFF = 0
        self.addCleanup(setattr, snapshot, "GET_BACKOFF", real_backoff)
        sess = FakeSession({SRCFILES_URL: [
            FakeResponse(status_code=503),
            FakeResponse(json_body={"result": [], "fileinfo": {}}),
        ]})
        self.assertEqual(snapshot.source_files(sess, "bash", "5.1-2"), {})
        self.assertEqual(len(sess.requested), 2)

class DownloadWritesAtomicallyAndReturnsItsOwnSha256(avocado.Test):
    def test(self):
        content = b"hello snapshot"
        sess = FakeSession({"http://x/file": FakeResponse(content=content)})
        workdir = tempfile.mkdtemp(prefix="seine-tests-snapshot-")
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        dest = os.path.join(workdir, "out.bin")
        digest = snapshot.download(sess, "http://x/file", dest)
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        with open(dest, "rb") as f:
            self.assertEqual(f.read(), content)
        # No leftover temporary file.
        self.assertEqual(os.listdir(workdir), ["out.bin"])

class DownloadRaisesSnapshotErrorOnHttpFailure(avocado.Test):
    def test(self):
        sess = FakeSession({"http://x/file": FakeResponse(status_code=500)})
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.download(sess, "http://x/file",
                              os.path.join(tempfile.gettempdir(), "unused"))
