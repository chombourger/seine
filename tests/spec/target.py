#!/usr/bin/env python3

import avocado
import os
import sys
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tui import target

# available() caches its result in target._available -- each test resets
# it, so one test's outcome can never leak into the next.
class Availability(avocado.Test):
    def setUp(self):
        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)

    def tearDown(self):
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client

    # sys.modules[name] = None is the standard way to force 'import
    # name' to raise ImportError without mtda actually being uninstalled.
    def test_not_available_when_mtda_cannot_be_imported(self):
        sys.modules["mtda.client"] = None
        self.assertFalse(target.available())

    def test_available_when_mtda_imports_cleanly(self):
        sys.modules["mtda"] = types.ModuleType("mtda")
        sys.modules["mtda.client"] = types.ModuleType("mtda.client")
        self.assertTrue(target.available())

    def test_result_is_cached_not_rechecked(self):
        sys.modules["mtda.client"] = None
        self.assertFalse(target.available())
        # Presence now wouldn't matter -- the cached False must stick.
        sys.modules["mtda"] = types.ModuleType("mtda")
        sys.modules["mtda.client"] = types.ModuleType("mtda.client")
        self.assertFalse(target.available())

# A stand-in for mtda.client.Client: records every call it receives (in
# order, with arguments) so a test can assert on exactly what target.py
# sent it, without a real mtda service anywhere. Same role as ai.py's
# fake_litellm().
class FakeClient:
    def __init__(self):
        self.calls = []
        self.started = False

    def start(self):
        self.started = True

    def target_on(self):
        self.calls.append(("target_on",))
        return True

    def target_off(self):
        self.calls.append(("target_off",))
        return True

    def target_toggle(self):
        self.calls.append(("target_toggle",))
        return True

    def target_status(self):
        self.calls.append(("target_status",))
        return "ON"

    def target_uptime(self):
        self.calls.append(("target_uptime",))
        return 42

    def usb_on(self, ndx):
        self.calls.append(("usb_on", ndx))

    def usb_off(self, ndx):
        self.calls.append(("usb_off", ndx))

    def usb_toggle(self, ndx):
        self.calls.append(("usb_toggle", ndx))

    def usb_ports(self):
        self.calls.append(("usb_ports",))
        return []

    def storage_to_host(self):
        self.calls.append(("storage_to_host",))
        return True

    def storage_to_target(self):
        self.calls.append(("storage_to_target",))
        return True

    def storage_write_image(self, path):
        self.calls.append(("storage_write_image", path))

    def storage_commit(self):
        self.calls.append(("storage_commit",))
        return True

    def storage_rollback(self):
        self.calls.append(("storage_rollback",))
        return True

    def storage_status(self):
        self.calls.append(("storage_status",))
        return ("idle", 0, 0)

    def console_send(self, data, raw=False):
        self.calls.append(("console_send", data, raw))

    def console_run(self, cmd):
        self.calls.append(("console_run", cmd))
        return "output"

    def console_dump(self):
        self.calls.append(("console_dump",))
        return "log so far"

    def console_head(self):
        self.calls.append(("console_head",))
        return "first line"

    def console_tail(self):
        self.calls.append(("console_tail",))
        return "last line"

    def console_wait(self, what, timeout=None):
        self.calls.append(("console_wait", what, timeout))
        return "matched line"

# Injects FakeClient as mtda.client.Client for the duration of one test,
# same sys.modules trick Availability uses above, plus a plain
# SimpleNamespace standing in for 'app' -- target.py only ever needs
# getattr/setattr on it for '_target_client'.
class Actions(avocado.Test):
    def setUp(self):
        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        mtda_pkg = types.ModuleType("mtda")
        mtda_client_mod = types.SimpleNamespace(Client=FakeClient)
        # Pre-inserting into sys.modules skips the import machinery's own
        # parent-attribute binding, so 'import mtda.client;
        # mtda.client.Client()' needs it set by hand here.
        mtda_pkg.client = mtda_client_mod
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_client_mod
        self.app = types.SimpleNamespace()

    def tearDown(self):
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client

    def _client(self):
        return self.app._target_client

    def test_get_client_is_cached_and_started_once(self):
        first = target.get_client(self.app)
        second = target.get_client(self.app)
        self.assertIs(first, second)
        self.assertTrue(first.started)

    def test_get_client_raises_unavailable_when_mtda_is_not_importable(self):
        sys.modules["mtda.client"] = None
        target._available = None
        with self.assertRaises(target.Unavailable):
            target.get_client(self.app)

    def test_power_dispatches_to_the_matching_verb(self):
        for state, rpc in (("on", "target_on"), ("off", "target_off"),
                           ("toggle", "target_toggle")):
            self.app = types.SimpleNamespace()
            target.power(self.app, state)
            self.assertEqual(self._client().calls, [(rpc,)])

    def test_usb_passes_the_port_as_an_int(self):
        target.usb(self.app, "2", "on")
        self.assertEqual(self._client().calls, [("usb_on", 2)])

    def test_write_image_swaps_storage_to_the_target_afterwards(self):
        target.write_image(self.app, "/path/to.img")
        self.assertEqual(self._client().calls,
                         [("storage_write_image", "/path/to.img"),
                          ("storage_to_target",)])

    def test_snapshot_and_rollback(self):
        target.snapshot(self.app)
        target.rollback(self.app)
        self.assertEqual(self._client().calls,
                         [("storage_commit",), ("storage_rollback",)])

    def test_console_send_is_always_raw(self):
        target.console_send(self.app, "root\n")
        self.assertEqual(self._client().calls, [("console_send", "root\n", True)])

    def test_console_run_returns_the_output(self):
        result = target.console_run(self.app, "uname -a")
        self.assertEqual(result, "output")
        self.assertEqual(self._client().calls, [("console_run", "uname -a")])

    def test_console_dump_and_wait_are_read_only(self):
        self.assertEqual(target.console_dump(self.app), "log so far")
        self.assertEqual(target.console_wait(self.app, "login:", timeout=30),
                         "matched line")
        self.assertEqual(self._client().calls,
                         [("console_dump",), ("console_wait", "login:", 30)])

    # head/tail exist so the model can check a boot without the token
    # cost of the whole buffer every time.
    def test_console_head_and_tail_return_one_line_each(self):
        self.assertEqual(target.console_head(self.app), "first line")
        self.assertEqual(target.console_tail(self.app), "last line")
        self.assertEqual(self._client().calls,
                         [("console_head",), ("console_tail",)])

    def test_status_reads_power_uptime_storage_and_usb(self):
        result = target.status(self.app)
        self.assertEqual(result, {"power": "ON", "uptime": 42,
                                  "storage": ("idle", 0, 0), "usb": []})
