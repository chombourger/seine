#!/usr/bin/env python3

import avocado
import contextlib
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

# Same '_tui_required' guard tests/spec/tui.py uses for anything that
# needs the 'tui' extra -- commands._target()'s mutating verbs reach
# ai.confirm(), which needs textual. Kept self-contained here rather
# than imported from tui.py, same reason tui.py gives for not sharing
# helpers across test files.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

class TargetCommand(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import commands, ai
        self.commands = commands
        self.ai = ai
        self._real_confirm = ai.confirm
        self.addCleanup(setattr, ai, "confirm", self._real_confirm)

        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        mtda_pkg = types.ModuleType("mtda")
        mtda_client_mod = types.SimpleNamespace(Client=FakeClient)
        mtda_pkg.client = mtda_client_mod
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_client_mod

        self.app = types.SimpleNamespace(_target_client=None, said=[])
        self.app.say = lambda text, error=False: self.app.said.append((text, error))
        self.app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)
        self.app.run_worker = lambda fn, thread=True, exclusive=True, group=None: fn()

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

    def test_status_is_a_plain_read_no_worker_no_confirm(self):
        confirmed = []
        self.ai.confirm = lambda *a, **k: confirmed.append(1) or True
        self.commands.dispatch(self.app, "/target status")
        self.assertEqual(confirmed, [])
        self.assertEqual(self._client().calls,
                         [("target_status",), ("target_uptime",),
                          ("storage_status",), ("usb_ports",)])

    def test_bare_target_is_the_same_as_status(self):
        self.commands.dispatch(self.app, "/target")
        self.assertEqual(self._client().calls[0], ("target_status",))

    def test_power_on_asks_for_confirmation_naming_the_action(self):
        seen = {}
        def fake_confirm(app, tool, arguments, preview):
            seen["name"] = tool.name
            seen["description"] = tool.description
            return True
        self.ai.confirm = fake_confirm
        self.commands.dispatch(self.app, "/target on")
        self.assertEqual(seen["name"], "target on")
        self.assertEqual(self._client().calls, [("target_on",)])

    def test_denied_confirmation_skips_the_action(self):
        self.ai.confirm = lambda *a, **k: False
        self.commands.dispatch(self.app, "/target off")
        self.assertIsNone(self.app._target_client)
        self.assertIn(("denied by user", False), self.app.said)

    def test_usb_needs_a_port_and_a_state(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target usb")

    def test_usb_on_reaches_the_right_port_as_an_int(self):
        self.ai.confirm = lambda *a, **k: True
        self.commands.dispatch(self.app, "/target usb 3 on")
        self.assertEqual(self._client().calls, [("usb_on", 3)])

    def test_write_needs_an_image_path(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target write")

    def test_write_confirms_then_swaps_storage_to_target(self):
        self.ai.confirm = lambda *a, **k: True
        self.commands.dispatch(self.app, "/target write /path/to.img")
        self.assertEqual(self._client().calls,
                         [("storage_write_image", "/path/to.img"),
                          ("storage_to_target",)])

    def test_storage_needs_host_or_target(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target storage bogus")

    def test_console_dump_head_tail_wait_are_plain_reads(self):
        confirmed = []
        self.ai.confirm = lambda *a, **k: confirmed.append(1) or True
        self.commands.dispatch(self.app, "/target console dump")
        self.commands.dispatch(self.app, "/target console head")
        self.commands.dispatch(self.app, "/target console tail")
        self.commands.dispatch(self.app, "/target console wait login: 5")
        self.assertEqual(confirmed, [])
        self.assertEqual(self._client().calls,
                         [("console_dump",), ("console_head",), ("console_tail",),
                          ("console_wait", "login:", 5.0)])

    def test_console_send_and_run_confirm_first(self):
        self.ai.confirm = lambda *a, **k: True
        self.commands.dispatch(self.app, "/target console send hello")
        self.commands.dispatch(self.app, "/target console run uname")
        self.assertEqual(self._client().calls,
                         [("console_send", "hello", True), ("console_run", "uname")])

    def test_unknown_verb_is_a_command_error(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target bogus")

    def test_no_mtda_is_a_plain_command_error_not_a_crash(self):
        sys.modules["mtda.client"] = None
        target._available = None
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target status")

# TargetState.on_event() parses one line exactly as mtda's main.py:
# notify() publishes it on the 'EVT' topic -- confirmed against
# main.py's own _storage_event()/_power_event() call sites and
# constants.py's actual POWER/STORAGE string values, not guessed.
class LiveState(avocado.Test):
    def setUp(self):
        self.state = target.TargetState()

    def test_power_on_and_off(self):
        self.state.on_event("POWER ON")
        self.assertEqual(self.state.power, "ON")
        self.state.on_event("POWER OFF")
        self.assertEqual(self.state.power, "OFF")

    def test_storage_location_changes(self):
        self.state.on_event("STORAGE TARGET")
        self.assertEqual(self.state.storage, "TARGET")
        self.state.on_event("STORAGE HOST")
        self.assertEqual(self.state.storage, "HOST")

    def test_writing_progress_is_parsed_in_order(self):
        self.state.on_event("STORAGE WRITING 4194304 8589934592 12582912.0 4194304")
        self.assertTrue(self.state.writing)
        self.assertEqual(self.state.write_read, 4194304)
        self.assertEqual(self.state.write_total, 8589934592)
        self.assertEqual(self.state.write_speed, 12582912.0)
        self.assertEqual(self.state.write_written, 4194304)

    def test_power_off_clears_an_in_progress_write(self):
        self.state.on_event("STORAGE WRITING 1 100 1.0 1")
        self.state.on_event("POWER OFF")
        self.assertFalse(self.state.writing)

    def test_a_storage_location_change_also_clears_writing(self):
        self.state.on_event("STORAGE WRITING 1 100 1.0 1")
        self.state.on_event("STORAGE TARGET")
        self.assertFalse(self.state.writing)

    def test_bytes_are_decoded_the_same_as_a_plain_string(self):
        self.state.on_event(b"POWER ON")
        self.assertEqual(self.state.power, "ON")

    # LOCKED/UNLOCKED/OPENED/CORRUPTED/INITIALIZED/CONNECTION/SESSION/
    # SYSTEM -- nothing seine tracks yet depends on these; must not crash.
    def test_unhandled_domains_and_reasons_are_ignored_not_a_crash(self):
        self.state.on_event("STORAGE LOCKED")
        self.state.on_event("STORAGE OPENED some-session")
        self.state.on_event("SYSTEM 0.42")
        self.state.on_event("CONNECTION ESTABLISHED")
        self.state.on_event("")
        self.assertIsNone(self.state.power)
        self.assertIsNone(self.state.storage)
        self.assertFalse(self.state.writing)

# The footer chip's own behaviour lives in seine/tui/base.py's
# TargetIndicator.refresh_text() -- a plain read of TargetState, same
# shape Indicators.refresh_text() already has, and (like Indicators)
# left without a dedicated widget-level test: tests/spec/tui.py has
# none for Indicators either, only for whole-screen behaviour.
