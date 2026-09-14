# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import atexit
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from seine.container import ContainerEngine
from seine.vault.base import VaultError, VaultNotFound, VaultProvider
from seine.vault.openbao import OpenBaoProvider

LABEL = "seine.vault"
OWNER_LABEL = "seine.vault.owner"
CREATED_LABEL = "seine.vault.created"

# The dev server runs the custom image (upstream plus our plugins),
# built from the checkout on first use and cached in podman storage.
CUSTOM_IMAGE = "localhost/seine-vault"

# Image label recording the sources a build used, so edits rebuild.
SOURCES_LABEL = "seine.vault.sources"

# Backstop for pid reuse: an owner that looks alive cannot be trusted
# forever, so anything older than this is reaped whatever it claims.
STALE_AFTER = 24 * 3600

# Fixed throwaways for local development only. Documented here, never
# fresh-random (identical specs must keep stable digests across runs),
# and never consulted for remote (which fails closed instead).
DEV_DEFAULTS = {
    # Password is "welcome123".
    "kv/data/accounts/root#hash":
        "$6$seinedev$ccTHYIq2vYrIL7os5.9sArjOo5MONfs9SyRg.4uN.8tMy8IPezo0N7Q4olw9TkaH3weI5YYRC7NeZrbi/ZKH2/",
}

START_TIMEOUT = 120


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# Every ephemeral instance, as (name, labels). Best effort like the
# rest of teardown: a broken engine fails start() below instead.
def _ephemeral():
    try:
        out = ContainerEngine.check_output(
            ["ps", "-a", "--filter", "label=%s=ephemeral" % LABEL,
             "--format", "json"])
    except (OSError, subprocess.CalledProcessError):
        return []
    try:
        listed = json.loads(out.decode() or "[]")
    except ValueError:
        return []
    return [((entry.get("Names") or [None])[0], entry.get("Labels") or {})
            for entry in listed]


def _remove(name):
    try:
        ContainerEngine.check_output(["container", "rm", "-f", name])
    except (OSError, subprocess.CalledProcessError):
        pass


# Builds the custom image unless committed sources already match what
# it was built from; shared by dev instances and the contract tests
# so both run the same artifact.
def ensure_image():
    try:
        if _image_label() == _sources_digest():
            return
    except (OSError, subprocess.CalledProcessError):
        pass
    root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ContainerEngine.run([
        "build", "--label", "%s=%s" % (SOURCES_LABEL, _sources_digest()),
        "-t", CUSTOM_IMAGE, "-f",
        os.path.join(root, "vault-image", "Dockerfile"), root], check=True)


def _image_label():
    return ContainerEngine.check_output(
        ["image", "inspect", "-f", "{{index .Labels \"%s\"}}" % SOURCES_LABEL,
         CUSTOM_IMAGE]).decode().strip()


# What the image was built from: plugin sources plus the Dockerfile
# itself, so editing any of them rebuilds it on next use.
def _sources_digest():
    root = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    digest = hashlib.sha256()
    paths = [os.path.join("vault-image", "Dockerfile")]
    for plugindir in ("seine-pgp", "seine-pkcs7", "seine-sbsign"):
        full = os.path.join(root, plugindir)
        if not os.path.isdir(full):
            continue
        paths += [os.path.join(plugindir, name)
                  for name in sorted(os.listdir(full))
                  if name.endswith(".go") or name in ("go.mod", "go.sum")]
    for path in paths:
        with open(os.path.join(root, path), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:16]


def _check_pgp_args(data, timestamp):
    if not isinstance(data, bytes):
        raise VaultError("pgp signing expects bytes, got %s"
                         % type(data).__name__)
    if not isinstance(timestamp, int) or timestamp < 0:
        raise VaultError("pgp signing expects a unix epoch timestamp")


# Drops instances whose owner is gone (kills and crashes) or which are
# older than STALE_AFTER. Returns what was removed, for the tests.
def reap_stale():
    reaped = []
    now = time.time()
    for name, labels in _ephemeral():
        if name is None:
            continue
        try:
            owner = int(labels.get(OWNER_LABEL) or "")
            owned = True
        except ValueError:
            owner, owned = None, False
        try:
            age = now - int(labels.get(CREATED_LABEL) or "")
        except ValueError:
            age = None
        if (owned and not _alive(owner)) \
                or (age is not None and age > STALE_AFTER):
            _remove(name)
            reaped.append(name)
    return reaped


# A private dev-mode instance per process: localhost-only, empty on
# boot, removed on exit. Speaks the same provider interface as remote,
# so callers never know which one they hold.
class DevVault(VaultProvider):
    IMAGE = "docker.io/openbao/openbao:2.6.2"

    def __init__(self):
        self._pid = os.getpid()
        self._name = "seine-vault-%d-%s" % (self._pid, secrets.token_hex(4))
        self._token = secrets.token_hex(16)
        self._port = None
        self._inner = None
        self._transit_keys = set()
        self._pgp_keys = {}
        self._lock = threading.Lock()
        self._closed = False

    def kv_read(self, ref):
        self._ensure_started()
        try:
            return self._inner.kv_read(ref)
        except VaultNotFound:
            return self._seed(ref)

    # A miss stores the tabled throwaway and warns loudly; an unknown
    # ref stays a miss, so typos fail closed in dev too.
    def _seed(self, ref):
        try:
            value = DEV_DEFAULTS[ref]
        except KeyError:
            raise VaultNotFound(
                "dev vault has no '%s' and no throwaway default for it" % ref
            ) from None
        sys.stderr.write(
            "warning: dev vault has no '%s'; seeding throwaway default "
            "(local development only, never production)\n" % ref)
        path, _, field = ref.partition("#")
        try:
            reply = self._inner._request(
                "GET", "/v1/%s" % path, None, self._token)
            data = reply.get("data") or {}
            current = dict(data.get("data") if isinstance(data.get("data"), dict)
                           else data)
        except VaultNotFound:
            current = {}
        current[field] = value
        self._inner._request(
            "PUT", "/v1/%s" % path, {"data": current}, self._token)
        return value

    # Generic crypto, the first consumer that needs no plugin. Missing
    # keys are minted on first use (and said so loudly); the types fit
    # the operation, since an encryption key cannot sign.
    def encrypt(self, key, plaintext):
        self._ensure_started()
        with self._lock:
            self._ensure_transit_key(key, "aes256-gcm96")
            return self._inner.encrypt(key, plaintext)

    def decrypt(self, key, ciphertext):
        self._ensure_started()
        with self._lock:
            self._ensure_transit_key(key, "aes256-gcm96")
            return self._inner.decrypt(key, ciphertext)

    def sign(self, key, data):
        self._ensure_started()
        with self._lock:
            self._ensure_transit_key(key, "rsa-2048")
            return self._inner.sign(key, data)

    def verify(self, key, data, signature):
        self._ensure_started()
        with self._lock:
            self._ensure_transit_key(key, "rsa-2048")
            return self._inner.verify(key, data, signature)

    def _ensure_transit_key(self, name, key_type):
        if name in self._transit_keys:
            return
        quoted = urllib.parse.quote(name, safe="")
        try:
            self._inner._request(
                "GET", "/v1/transit/keys/%s" % quoted, None, self._token)
        except VaultNotFound:
            sys.stderr.write(
                "warning: dev vault has no transit key '%s'; generating %s "
                "(local development only, never production)\n" % (name, key_type))
            self._inner._request("POST", "/v1/transit/keys/%s" % quoted,
                                 {"type": key_type}, self._token)
        self._transit_keys.add(name)

    # Repository signing through the plugin. Missing keys are minted on
    # first use like transit keys; reads stay fail-closed.
    def pgp_fingerprint(self, name):
        self._ensure_started()
        with self._lock:
            self._ensure_pgp_key(name)
            return self._inner.pgp_fingerprint(name)

    def pgp_public_key(self, name):
        self._ensure_started()
        with self._lock:
            self._ensure_pgp_key(name)
            return self._inner.pgp_public_key(name)

    def pgp_clearsign(self, name, data, timestamp):
        self._ensure_started()
        _check_pgp_args(data, timestamp)
        with self._lock:
            return self._inner.pgp_clearsign(
                name, data, self._effective_timestamp(name, timestamp))

    def pgp_detach_sign(self, name, data, timestamp):
        self._ensure_started()
        _check_pgp_args(data, timestamp)
        with self._lock:
            return self._inner.pgp_detach_sign(
                name, data, self._effective_timestamp(name, timestamp))

    def _ensure_pgp_key(self, name):
        birth = self._pgp_keys.get(name)
        if birth is not None:
            return birth
        quoted = urllib.parse.quote(name, safe="")
        try:
            self._inner._request(
                "GET", "/v1/seine-pgp/keys/%s/public" % quoted, None,
                self._token)
            birth = 0
        except VaultNotFound:
            sys.stderr.write(
                "warning: dev vault has no pgp key '%s'; generating a "
                "throwaway (local development only, never production)\n" % name)
            self._inner._request("POST", "/v1/seine-pgp/keys/%s" % quoted,
                                 {"generate": {}}, self._token)
            birth = int(time.time())
        self._pgp_keys[name] = birth
        return birth

    # A freshly minted dev key postdates old build epochs, which the
    # plugin refuses to sign at; clamp to key birth instead. Remote
    # never clamps: prod keys predate any build.
    def _effective_timestamp(self, name, timestamp):
        effective = max(timestamp, self._ensure_pgp_key(name))
        if effective != timestamp:
            sys.stderr.write(
                "warning: dev pgp key '%s' is newer than the build epoch; "
                "signing at key creation instead\n" % name)
        return effective

    def running(self):
        try:
            ContainerEngine.check_output(["container", "exists", self._name])
            return True
        except (OSError, subprocess.CalledProcessError):
            return False

    # Idempotent and silent: exit paths and signal handlers share it.
    def close(self):
        if self._closed:
            return
        self._closed = True
        _remove(self._name)

    def _ensure_started(self):
        with self._lock:
            if self._inner is not None:
                return
            reap_stale()
            try:
                ensure_image()
                ContainerEngine.check_output([
                    "run", "-d", "--name", self._name,
                    "--label", "%s=ephemeral" % LABEL,
                    "--label", "%s=%d" % (OWNER_LABEL, self._pid),
                    "--label", "%s=%d" % (CREATED_LABEL, int(time.time())),
                    "-p", "127.0.0.1::8200",
                    "-e", 'BAO_LOCAL_CONFIG={"plugin_directory":"/vault/plugins"}',
                    "-e", "BAO_DEV_ROOT_TOKEN_ID=" + self._token,
                    CUSTOM_IMAGE,
                    "server", "-dev",
                    "-dev-listen-address=0.0.0.0:8200",
                    "-dev-no-store-token"])
                out = ContainerEngine.check_output(
                    ["port", self._name, "8200"]).decode()
                self._port = int(out.strip().splitlines()[0].rsplit(":", 1)[1])
                self._wait_ready()
                self._inner = OpenBaoProvider(
                    addr="http://127.0.0.1:%d" % self._port, token=self._token)
                self._provision()
            except (OSError, subprocess.CalledProcessError, ValueError) as e:
                _remove(self._name)
                raise VaultError("ephemeral vault failed to start: %s" % e) from e
            atexit.register(self.close)
            self._install_handlers()

    def _wait_ready(self):
        deadline = time.time() + START_TIMEOUT
        while True:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/v1/sys/seal-status" % self._port,
                        timeout=5) as reply:
                    body = json.loads(reply.read().decode() or "{}")
                if body.get("initialized") and not body.get("sealed"):
                    return
            except (OSError, ValueError):
                pass
            if time.time() > deadline:
                raise VaultError("dev server did not come up in time")
            time.sleep(0.5)

    def _provision(self):
        mounts = self._inner._request(
            "GET", "/v1/sys/mounts", None, self._token).get("data") or {}
        if "kv/" not in mounts:
            self._inner._request("POST", "/v1/sys/mounts/kv",
                                 {"type": "kv", "options": {"version": "2"}},
                                 self._token)
        if "transit/" not in mounts:
            self._inner._request("POST", "/v1/sys/mounts/transit",
                                 {"type": "transit"}, self._token)
        if "seine-pgp/" not in mounts:
            digest = ContainerEngine.check_output(
                ["run", "--rm", CUSTOM_IMAGE, "sha256sum",
                 "/vault/plugins/seine-pgp.so"]).decode().split()[0]
            self._inner._request(
                "PUT", "/v1/sys/plugins/catalog/secret/seine-pgp",
                {"sha_256": digest, "command": "seine-pgp.so"}, self._token)
            self._inner._request("POST", "/v1/sys/mounts/seine-pgp",
                                 {"type": "seine-pgp"}, self._token)

    # SIGTERM never runs atexit handlers; chained so whatever was there
    # (tasks.py's own SIGINT handling, ...) still gets its turn.
    def _install_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous = signal.getsignal(sig)
                signal.signal(sig, self._handler(sig, previous))
            except (OSError, ValueError):
                pass

    def _handler(self, sig, previous):
        def handle(signum, frame):
            self.close()
            if callable(previous):
                previous(signum, frame)
            elif sig == signal.SIGINT:
                raise KeyboardInterrupt
            else:
                signal.signal(sig, signal.SIG_DFL)
                os.kill(os.getpid(), sig)
        return handle
