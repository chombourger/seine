# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Optional '/target' support: driving a real device through mtda
# (github.com/mtda-project/mtda), a gRPC service already exposing
# power/storage/USB/console control. mtda is a system package (like
# python3-guestfs), never a pip dependency of seine -- see
# doctor.check_mtda() for the install-time report; available() below is
# the runtime gate '/target' and its AI tools check before doing
# anything.

_available = None

# A real import, not importlib.util.find_spec: proves mtda.client
# actually loads (catches a broken system install), not just that it is
# on the path. Cached after the first call -- this only needs to run
# once per process.
def available():
    global _available
    if _available is None:
        try:
            import mtda.client  # noqa: F401
            _available = True
        except Exception:
            _available = False
    return _available

# Raised by every function below -- '/target' (commands.py) and the AI
# tools (ai.py) each catch it and translate it their own way, same as
# context.side_load()'s OSError/ValueError is handled in each caller.
class Unavailable(Exception):
    pass

# Lazily dialled: whichever side (typed command or AI tool call) touches
# the target first pays the connection cost, then both reuse the same
# mtda.client.Client cached on the app. No host/port of our own here --
# a bare Client() reads mtda's own local config, exactly like running
# mtda-cli with no '--remote' does; seine-specific overrides land in
# settings.py once the screen (and its "not on this system" case) exist.
def get_client(app):
    if not available():
        raise Unavailable("mtda is not installed on this system")
    client = getattr(app, "_target_client", None)
    if client is None:
        import mtda.client
        client = mtda.client.Client()
        client.start()
        app._target_client = client
    return client

# --- Power ---

_POWER_VERBS = {"on": "target_on", "off": "target_off", "toggle": "target_toggle"}

def power(app, state):
    client = get_client(app)
    return getattr(client, _POWER_VERBS[state])()

# --- USB ---

_USB_VERBS = {"on": "usb_on", "off": "usb_off", "toggle": "usb_toggle"}

def usb(app, port, state):
    client = get_client(app)
    return getattr(client, _USB_VERBS[state])(int(port))

# --- Storage ---

def storage_to_host(app):
    return get_client(app).storage_to_host()

def storage_to_target(app):
    return get_client(app).storage_to_target()

# storage_write_image() itself only opens/copies/closes the shared
# storage device (mtda/client.py's own Client.storage_write_image) --
# it never re-attaches storage to the target afterwards, so that is a
# second, explicit call here, matching what mtda-cli's own 'storage
# write' + a following 'storage target' would do by hand.
def write_image(app, path):
    client = get_client(app)
    client.storage_write_image(path)
    client.storage_to_target()

def snapshot(app):
    return get_client(app).storage_commit()

def rollback(app):
    return get_client(app).storage_rollback()

# --- Console ---

def console_send(app, data):
    return get_client(app).console_send(data, raw=True)

def console_run(app, cmd):
    return get_client(app).console_run(cmd)

# Read-only, no _confirm() needed: the model has to be able to see what
# a target is doing, not just poke it blind with send/run.
def console_dump(app):
    return get_client(app).console_dump()

# First/last line only -- lets the model check "did it boot" or "what's
# the last line" without paying full-buffer tokens for console_dump()
# every time.
def console_head(app):
    return get_client(app).console_head()

def console_tail(app):
    return get_client(app).console_tail()

def console_wait(app, what, timeout=None):
    return get_client(app).console_wait(what, timeout=timeout)

# --- Status (one-shot reads; commit 5 adds the live event-driven path) ---

def status(app):
    client = get_client(app)
    return {"power": client.target_status(),
            "uptime": client.target_uptime(),
            "storage": client.storage_status(),
            "usb": client.usb_ports()}

# --- Live state (fed by the 'EVT' topic on mtda's Subscribe stream) ---

# What the footer chip and (later) the Remote Target screen's status
# pane render -- kept apart from any widget so it is testable without a
# running App, the same split build.py's BuildState uses. Nothing here
# starts a connection; commit that adds the console pane owns
# subscribing a RemoteConsole to feed on_event() below.
class TargetState:
    def __init__(self):
        self.power = None      # CONSTS.POWER value ('ON'/'OFF'/...), None until seen
        self.storage = None    # CONSTS.STORAGE location ('HOST'/'NETWORK'/'TARGET'), None until seen
        self.writing = False
        self.write_read = 0
        self.write_total = 0
        self.write_speed = 0.0
        self.write_written = 0

    # One line off the 'EVT' topic, exactly as mtda's main.py:notify()
    # publishes it: f"{domain} {info}" -- 'POWER ON', 'STORAGE TARGET',
    # 'STORAGE WRITING <read> <total> <speed> <written>' (main.py:757-
    # 764's _storage_event()/1235-1248's _power_event(), the WRITING
    # shape from writer.py per mtda.md). mtda-cli's own AppOutput.
    # on_event() is only a reference for the WRITING case -- it ignores
    # every other domain, so POWER/STORAGE-location handling below is
    # derived straight from main.py's notify() call sites instead.
    def on_event(self, line):
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        info = line.split()
        if not info:
            return
        domain, rest = info[0], info[1:]
        if not rest:
            return
        if domain == "POWER":
            self.power = rest[0]
            if rest[0] == "OFF":
                self.writing = False
        elif domain == "STORAGE":
            if rest[0] == "WRITING" and len(rest) == 5:
                self.writing = True
                self.write_read = int(rest[1])
                self.write_total = int(rest[2])
                self.write_speed = float(rest[3])
                self.write_written = int(rest[4])
            elif rest[0] in ("HOST", "NETWORK", "TARGET"):
                self.storage = rest[0]
                self.writing = False
            # LOCKED/UNLOCKED/OPENED/CORRUPTED/INITIALIZED/??? -- no
            # seine-visible state depends on these yet.
