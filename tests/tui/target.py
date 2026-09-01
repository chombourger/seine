#!/usr/bin/env python3

import asyncio
import avocado
import contextlib
import os
import sys
import time
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tui import target
from tests.native_image import native_image

NATIVE_IMAGE = native_image()

def _run(scenario):
    asyncio.run(scenario())

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
    def __init__(self, host=None):
        self.host = host
        self.calls = []
        self.started = False
        self.stopped = False
        # No remote by default -- matches a Client() whose config has no
        # [remote] host set, so get_client() skips console_remote()
        # (real behaviour, main.py:348-366). Tests that care about the
        # console/EVT wiring set self.agent.remote themselves.
        self.agent = types.SimpleNamespace(remote=None)
        self.console_remote_calls = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def console_remote(self, host, screen):
        self.console_remote_calls.append((host, screen))

    def session(self):
        return "fake-session"

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

    # FakeClient.agent.remote defaults to None (matches a Client() with
    # no [remote] host set) -- console_remote() must not be called then.
    def test_no_console_subscription_without_a_configured_remote(self):
        target.get_client(self.app)
        self.assertEqual(self._client().console_remote_calls, [])
        # No pyte import either -- ConsoleAdapter is never constructed.
        self.assertIsNone(getattr(self.app, "_target_console", None))

    def test_console_subscription_starts_when_a_remote_is_configured(self):
        client = FakeClient()
        client.agent.remote = "192.0.2.1"
        sys.modules["mtda.client"].Client = lambda host=None: client
        target.get_client(self.app)
        self.assertEqual(len(client.console_remote_calls), 1)
        host, adapter = client.console_remote_calls[0]
        self.assertEqual(host, "192.0.2.1")
        self.assertIs(adapter, self.app._target_console)

    def test_connect_forwards_the_host_and_replaces_the_cached_client(self):
        target.get_client(self.app)  # first, default-host connection
        first = self._client()
        target.connect(self.app, "192.0.2.9:1234")
        second = self._client()
        self.assertIsNot(first, second)
        self.assertTrue(first.stopped)
        self.assertEqual(second.host, "192.0.2.9:1234")

    def test_connect_with_no_host_still_tears_down_and_redials(self):
        target.get_client(self.app)
        first = self._client()
        target.connect(self.app)
        self.assertIsNot(first, self._client())
        self.assertTrue(first.stopped)

    def test_disconnect_clears_the_client_and_resets_state(self):
        self.app.target_state = target.TargetState()
        target.get_client(self.app)
        client = self._client()
        target.disconnect(self.app)
        self.assertTrue(client.stopped)
        self.assertIsNone(self.app._target_client)
        self.assertIsNone(self.app.target_state.agent)

    def test_disconnect_without_a_client_is_a_no_op(self):
        target.disconnect(self.app)  # must not raise
        self.assertIsNone(self.app._target_client)

    def test_connect_side_loads_a_fresh_history_group(self):
        from seine.tui import history
        self.app.history = history.History(os.path.join(self.workdir, "history.json"))
        self.app.history.add_side(target.HISTORY_GROUP, "old-device-command")
        target.connect(self.app)
        # A fresh dial is a clean slate, same as the client itself --
        # the previous agent's console lines don't leak into this one.
        self.assertIsNone(self.app.history.last({target.HISTORY_GROUP}))

    def test_disconnect_side_unloads_the_history_group(self):
        from seine.tui import history
        self.app.history = history.History(os.path.join(self.workdir, "history.json"))
        target.connect(self.app)
        self.app.history.add_side(target.HISTORY_GROUP, "root")
        target.disconnect(self.app)
        self.assertIsNone(self.app.history.last({target.HISTORY_GROUP}))

    # No app.history at all (the lighter TargetCommand/AI-tool fixtures)
    # must not crash connect()/disconnect() -- same tolerance as the
    # existing 'no app.target_state' case.
    def test_connect_and_disconnect_tolerate_no_history_on_app(self):
        target.connect(self.app)
        target.disconnect(self.app)

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

    def test_console_send_defaults_to_raw(self):
        target.console_send(self.app, "root\n")
        self.assertEqual(self._client().calls, [("console_send", "root\n", True)])

    # raw=False is what makes mtda's own console_send() run
    # codecs.escape_decode() -- see target.py's own comment.
    def test_console_send_forwards_raw_false(self):
        target.console_send(self.app, "root\\n", raw=False)
        self.assertEqual(self._client().calls, [("console_send", "root\\n", False)])

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

# Same '_tui_required' guard tests/tui/tui.py uses for anything that
# needs the 'tui' extra (textual). Kept self-contained here rather than
# imported from tui.py, same reason tui.py gives for not sharing
# helpers across test files.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

class TargetCommand(avocado.Test):
    def setUp(self):
        from seine.tui import commands
        self.commands = commands

        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        mtda_pkg = types.ModuleType("mtda")
        mtda_client_mod = types.SimpleNamespace(Client=FakeClient)
        mtda_pkg.client = mtda_client_mod
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_client_mod

        self.app = types.SimpleNamespace(_target_client=None, said=[], shown=[],
                                         connect_calls=[], redraws=0)
        self.app.say = lambda text, error=False: self.app.said.append((text, error))
        self.app.call_from_thread = lambda fn, *a, **kw: fn(*a, **kw)
        self.app.run_worker = lambda fn, thread=True, exclusive=True, group=None: fn()
        self.app.show = lambda name: self.app.shown.append(name)
        # '/target connect'/'disconnect' (commands.py) only need
        # app.screen.connect()/._redraw_status() -- the real dial/redraw
        # behaviour behind those is TargetScreen's own, exercised in
        # TargetScreenIntegration; this is just wiring.
        self.app.screen = types.SimpleNamespace(
            connect=lambda host, force: self.app.connect_calls.append((host, force)),
            _redraw_status=lambda: setattr(self.app, "redraws", self.app.redraws + 1))

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

    def test_status_is_a_plain_read(self):
        self.commands.dispatch(self.app, "/target status")
        self.assertEqual(self._client().calls,
                         [("target_status",), ("target_uptime",),
                          ("storage_status",), ("usb_ports",)])

    def test_bare_target_switches_to_the_screen(self):
        self.commands.dispatch(self.app, "/target")
        self.assertEqual(self.app.shown, ["target"])
        # No RPC call -- switching screens doesn't dial mtda, only the
        # availability check (import, no network) runs first.
        self.assertIsNone(self.app._target_client)

    def test_connect_switches_to_the_screen_and_forces_a_reconnect(self):
        self.commands.dispatch(self.app, "/target connect")
        self.assertEqual(self.app.shown, ["target"])
        self.assertEqual(self.app.connect_calls, [(None, True)])

    def test_connect_forwards_the_named_agent(self):
        self.commands.dispatch(self.app, "/target connect 192.0.2.5:1234")
        self.assertEqual(self.app.connect_calls, [("192.0.2.5:1234", True)])

    def test_connect_takes_at_most_one_agent(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target connect a b")

    def test_disconnect_clears_the_client_and_redraws(self):
        target.get_client(self.app)  # something to disconnect from
        self.commands.dispatch(self.app, "/target disconnect")
        self.assertIsNone(self.app._target_client)
        self.assertEqual(self.app.redraws, 1)

    def test_disconnect_takes_no_arguments(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target disconnect now")

    # No confirmation: typing '/target on' already is the deliberate
    # act, unlike the AI's own tool calls (ai.py's own gating, tested
    # in tests/tui/ai.py's MtdaTools).
    def test_power_on_runs_directly_no_confirmation(self):
        self.commands.dispatch(self.app, "/target on")
        self.assertEqual(self._client().calls, [("target_on",)])

    def test_usb_needs_a_port_and_a_state(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target usb")

    def test_usb_on_reaches_the_right_port_as_an_int(self):
        self.commands.dispatch(self.app, "/target usb 3 on")
        self.assertEqual(self._client().calls, [("usb_on", 3)])

    def test_write_needs_an_image_path(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target write")

    def test_write_swaps_storage_to_target(self):
        self.commands.dispatch(self.app, "/target write /path/to.img")
        self.assertEqual(self._client().calls,
                         [("storage_write_image", "/path/to.img"),
                          ("storage_to_target",)])

    def test_storage_needs_host_or_target(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, self.app, "/target storage bogus")

    def test_console_dump_head_tail_wait_are_plain_reads(self):
        self.commands.dispatch(self.app, "/target console dump")
        self.commands.dispatch(self.app, "/target console head")
        self.commands.dispatch(self.app, "/target console tail")
        self.commands.dispatch(self.app, "/target console wait login: 5")
        self.assertEqual(self._client().calls,
                         [("console_dump",), ("console_head",), ("console_tail",),
                          ("console_wait", "login:", 5.0)])

    def test_console_send_and_run(self):
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
        self.assertIsNotNone(self.state.power_on_at)
        self.state.on_event("POWER OFF")
        self.assertEqual(self.state.power, "OFF")
        self.assertIsNone(self.state.power_on_at)

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

    # RemoteConsole's EVT stream never replays past state -- seed()
    # primes power/storage from a one-shot status() read instead.
    def test_seed_primes_power_and_storage_from_a_status_read(self):
        self.state.seed({"power": "ON", "uptime": 12, "storage": ("TARGET", False, 0),
                         "usb": []})
        self.assertEqual(self.state.power, "ON")
        self.assertEqual(self.state.storage, "TARGET")

    # Backdated by the real uptime status() read, not started fresh --
    # a target already up for a while shows its real age, not zero.
    def test_seed_backdates_power_on_at_by_the_real_uptime(self):
        self.state.seed({"power": "ON", "uptime": 3600, "storage": ("TARGET", False, 0),
                         "usb": []})
        self.assertAlmostEqual(self.state.power_on_at, time.time() - 3600, delta=1)

    def test_seed_leaves_power_on_at_none_when_off(self):
        self.state.seed({"power": "OFF", "uptime": 0, "storage": ("HOST", False, 0),
                         "usb": []})
        self.assertIsNone(self.state.power_on_at)

    def test_seed_does_not_touch_write_progress(self):
        self.state.on_event("STORAGE WRITING 1 100 1.0 1")
        self.state.seed({"power": "ON", "uptime": 0, "storage": ("HOST", True, 1),
                         "usb": []})
        self.assertTrue(self.state.writing)
        self.assertEqual(self.state.write_total, 100)

# The footer chip's own behaviour lives in seine/tui/base.py's
# TargetIndicator.refresh_text() -- a plain read of TargetState, same
# shape Indicators.refresh_text() already has, and (like Indicators)
# left without a dedicated widget-level test: tests/tui/tui.py has
# none for Indicators either, only for whole-screen behaviour.

# ConsoleAdapter needs pyte (setup.py's 'tui' extra, bundled with
# textual/rich) -- cancel rather than error if it truly isn't there,
# same convention tests/tui/tui.py's _tui_required uses.
@contextlib.contextmanager
def _pyte_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("pyte is not installed: %s" % e)

class Console(avocado.Test):
    def setUp(self):
        with _pyte_required(self):
            import pyte
        self.pyte = pyte
        self.app = types.SimpleNamespace(target_state=target.TargetState())
        self.app.said_refresh = []
        self.app.refresh_indicators = lambda: self.app.said_refresh.append(1)
        self.app.crossed_threads = []
        def call_from_thread(fn, *a, **kw):
            self.app.crossed_threads.append(1)
            return fn(*a, **kw)
        self.app.call_from_thread = call_from_thread
        self.adapter = target.ConsoleAdapter(self.app)

    def test_print_feeds_pyte_and_decodes_bytes(self):
        self.adapter.print(b"hello\r\n")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "hello")

    # dirty is a plain flag, not a push -- TargetScreen's own tick polls
    # it at a bounded rate instead.
    def test_print_sets_dirty_without_touching_the_thread(self):
        self.assertFalse(self.adapter.dirty)
        self.adapter.print(b"x")
        self.assertTrue(self.adapter.dirty)
        self.assertEqual(self.app.crossed_threads, [])

    def test_on_event_updates_target_state_and_refreshes_indicators(self):
        self.adapter.on_event("POWER ON")
        self.assertEqual(self.app.target_state.power, "ON")
        self.assertEqual(self.app.said_refresh, [1])

    def test_render_console_returns_plain_text_for_uncoloured_output(self):
        self.adapter.print("plain text\r\n")
        rendered = target.render_console(self.adapter.screen)
        self.assertIn("plain text", rendered.plain)

    def test_render_console_max_lines_caps_from_the_top(self):
        for i in range(5):
            self.adapter.print("line %d\r\n" % i)
        rendered = target.render_console(self.adapter.screen, max_lines=2)
        lines = rendered.plain.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("line 0", lines[0])

    def test_render_console_carries_colour_as_a_rich_style(self):
        self.adapter.print("\x1b[31mred\x1b[0m\r\n")
        rendered = target.render_console(self.adapter.screen)
        # First three characters ('red') should carry a red foreground.
        spans = [s for s in rendered.spans if s.start == 0]
        self.assertTrue(any(s.style.color is not None and
                            "red" in str(s.style.color) for s in spans))

    # A real terminal clips when its display is narrower than its own
    # width, it doesn't reflow -- rewrapping here would scramble the
    # fixed-column grid a BIOS/serial console assumes.
    def test_render_console_does_not_rewrap(self):
        rendered = target.render_console(self.adapter.screen)
        self.assertTrue(rendered.no_wrap)
        self.assertEqual(rendered.overflow, "crop")

    # See target.py's own CONSOLE_LINES comment: this firmware's own
    # boot-menu screen addresses absolute rows past 31.
    def test_console_screen_is_80_columns_wide_and_tall_enough_for_this_firmware(self):
        self.assertEqual(self.adapter.screen.columns, 80)
        self.assertGreater(self.adapter.screen.lines, 31)

    # Isolates the scroll-drift mechanism CONSOLE_LINES=40 fixes: an
    # absolute-position redraw, plain scrolling log lines, then the
    # identical redraw again. On a too-short screen the log lines
    # scroll the first redraw off its row while the second still lands
    # at the same absolute position -- two writes, two final positions,
    # looking like duplicated text.
    def test_a_too_short_screen_would_duplicate_via_scroll_drift(self):
        import pyte
        short_screen = pyte.Screen(80, 25)
        short_stream = pyte.Stream(short_screen)
        short_stream.feed("\x1b[10;4HDevice Manager")
        for i in range(20):
            short_stream.feed("log line %d\r\n" % i)
        short_stream.feed("\x1b[10;4HDevice Manager")
        occurrences = [l for l in short_screen.display if "Device Manager" in l]
        self.assertEqual(len(occurrences), 2, "expected the drift bug on a 25-line screen")

        self.adapter.print("\x1b[10;4HDevice Manager")
        for i in range(20):
            self.adapter.print("log line %d\r\n" % i)
        self.adapter.print("\x1b[10;4HDevice Manager")
        occurrences = [l for l in self.adapter.screen.display if "Device Manager" in l]
        self.assertEqual(len(occurrences), 1)

    # See target.py's _UNSUPPORTED_CSI: 'CSI = <n> <letter>' is a
    # legacy BIOS/DOS-ANSI mode-set convention pyte's parser leaks as
    # literal text; stripped since '=' has no safe recovery.
    def test_unsupported_csi_equals_sequence_is_stripped(self):
        self.adapter.print("before\x1b[=3hafter")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "beforeafter")

    def test_unsupported_csi_equals_sequence_split_across_chunks(self):
        self.adapter.print("before\x1b[=3")
        self.adapter.print("hafter")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "beforeafter")

    # Recovered, not just stripped: ':' is ECMA-48's own sub-parameter
    # separator, so deleting it reconstructs the 'CSI 25;0H' cursor
    # move the firmware meant, moving the cursor there instead of
    # leaving 'after' wherever it happened to land.
    def test_unsupported_csi_stray_colon_sequence_recovers_cursor_position(self):
        self.adapter.print("before\x1b[25;:0Hafter")
        self.assertNotIn("0H", "\n".join(self.adapter.screen.display))
        self.assertEqual(self.adapter.screen.display[24][:5], "after")

    def test_unsupported_csi_stray_colon_sequence_split_across_chunks(self):
        self.adapter.print("before\x1b[25;:")
        self.adapter.print("0Hafter")
        self.assertNotIn("0H", "\n".join(self.adapter.screen.display))
        self.assertEqual(self.adapter.screen.display[24][:5], "after")

    # A normal, pyte-supported private-mode sequence ('?') and an
    # ordinary multi-parameter cursor-position sequence must both keep
    # working -- this fix only targets a genuinely unexpected character
    # in the parameter area, not '?' or the usual digits/';'.
    def test_supported_dec_private_mode_is_unaffected(self):
        self.adapter.print("before\x1b[?25hafter")
        self.assertEqual(self.adapter.screen.display[0].rstrip(), "beforeafter")

    def test_ordinary_cursor_position_is_unaffected(self):
        self.adapter.print("before\x1b[08;71Hafter")
        # row 8, column 71 (1-indexed) -> row 7, column 70 zero-indexed.
        self.assertEqual(self.adapter.screen.display[7][70:75], "after")

    # Decoding each chunk independently corrupts a multi-byte character
    # split across a chunk boundary into U+FFFD on both sides of the
    # cut; pyte.ByteStream's own incremental decoder holds the partial
    # bytes across feed() calls instead.
    def test_multibyte_utf8_split_across_chunks_decodes_correctly(self):
        data = "─".encode("utf-8")
        self.adapter.print(data[:1])
        self.adapter.print(data[1:])
        self.assertEqual(self.adapter.screen.display[0][:1], "─")
        self.assertNotIn("�", self.adapter.screen.display[0])

    # Real captured BIOS/UEFI output (console_dump(), two captures) --
    # a regression fixture replaying all three bugs above together.
    def test_real_bios_capture_has_no_leaked_csi_or_replacement_chars(self):
        boot = ('\x1b[2J\x1b[01;01H\x1b[=3h\x1b[2J\x1b[01;01H\x1b[2J\x1b[01;01H'
               '\x1b[=3h\x1b[2J\x1b[01;01H\x1b[2J\x1b[01;01H\x1b[=3h\x1b[2J'
               '\x1b[01;01HBdsDxe: loading Boot0002 "UEFI QEMU HARDDISK '
               'QM00003 " from PciRoot(0x0)/Pci(0x1,0x1)/Ata(Secondary,'
               'Master,0x0)\n\x1b[2J\x1b[01;01H\x1b[0m\x1b[36m\x1b[40m'
               'EFI Boot Guard v0.22\n')
        menu = '\x1b[29;03H↑↓=Move Highlight              '
        # From the second capture (the boot-menu screen, taken after
        # navigating with the arrow keys) -- the stray-':' shape, real,
        # not synthesised.
        highlight = '\x1b[25;:0H\x1b[08;71H'
        # Fed in small, arbitrarily-cut chunks -- deliberately not
        # aligned to escape-sequence or character boundaries, the same
        # way mtda's own reader thread delivers hundreds of small,
        # arbitrarily-sized chunks a second.
        raw = (boot + menu + highlight).encode("utf-8")
        chunk = 7
        for i in range(0, len(raw), chunk):
            self.adapter.print(raw[i:i + chunk])
        text = "\n".join(self.adapter.screen.display)
        self.assertNotIn("=3h", text)
        self.assertNotIn("0H", text)
        self.assertNotIn("�", text)
        self.assertIn("Move Highlight", text)

class TargetStatusRendering(avocado.Test):
    def setUp(self):
        with _pyte_required(self):
            import pyte  # noqa: F401 -- render_target_status needs rich, not pyte,
                         # but keep the same cancel-if-missing guard for consistency
        self.state = target.TargetState()
        # Connected by default -- these tests are about the power/storage
        # icons' own click/colour behaviour, not the disconnected state
        # (see test_not_connected_* below for that).
        self.state.agent = "Local"

    def _clicks(self, rendered):
        return [s.style.meta.get("target-click") for s in rendered.spans
                if s.style.meta.get("target-click")]

    # A plain marker, not Rich's own '@click' action-link string --
    # Textual overlays *any* '@click' span with its own link colour/
    # underline, unconditionally on top of whatever this function
    # already set, which is exactly what broke the colour here and
    # forced every storage token underlined regardless of which one was
    # actually active. TargetStatusStatic (target_screen.py) reads this
    # marker in its own on_click() instead.
    def test_power_token_is_clickable(self):
        self.state.on_event("POWER OFF")
        rendered = target.render_target_status(self.state)
        self.assertIn("⏻", rendered.plain)
        self.assertIn("power", self._clicks(rendered))

    # Colour carries the state, not the text -- gray off/unknown,
    # dark_orange on.
    def test_power_icon_is_grey_off_and_orange_on(self):
        icon_style = lambda rendered: next(
            s.style for s in rendered.spans if s.style.meta.get("target-click") == "power")
        self.assertEqual(icon_style(target.render_target_status(self.state)).color.name, "grey50")
        self.state.on_event("POWER ON")
        self.assertEqual(icon_style(target.render_target_status(self.state)).color.name,
                         "dark_orange")

    # One icon toggles between HOST/TARGET -- clicking always names
    # the *other* one, same shape as the power icon's own toggle.
    def test_storage_icon_click_names_the_other_location(self):
        self.state.on_event("STORAGE TARGET")
        rendered = target.render_target_status(self.state)
        self.assertIn(("storage", "host"), self._clicks(rendered))

        self.state.on_event("STORAGE HOST")
        rendered = target.render_target_status(self.state)
        self.assertIn(("storage", "target"), self._clicks(rendered))

    def test_storage_icon_is_grey_on_host_and_orange_on_target(self):
        icon_style = lambda rendered: next(
            s.style for s in rendered.spans
            if isinstance(s.style.meta.get("target-click"), tuple))
        self.state.on_event("STORAGE HOST")
        self.assertEqual(icon_style(target.render_target_status(self.state)).color.name,
                         "grey50")
        self.state.on_event("STORAGE TARGET")
        self.assertEqual(icon_style(target.render_target_status(self.state)).color.name,
                         "dark_orange")

    def test_writing_progress_shown_only_while_writing(self):
        rendered = target.render_target_status(self.state)
        self.assertNotIn("WRITING", rendered.plain)
        self.state.on_event("STORAGE WRITING 50 100 1.0 50")
        rendered = target.render_target_status(self.state)
        self.assertIn("WRITING  50%", rendered.plain)

    # state.agent is None until connect()/get_client() actually dial --
    # no more auto-connect on screen mount, so a fresh TargetState()
    # (not this class's own connected self.state) is the common case.
    def test_not_connected_shows_a_hint_instead_of_agent_session(self):
        state = target.TargetState()
        rendered = target.render_target_status(state)
        self.assertIn("/target connect", rendered.plain)

    # Both icons grey and carry no 'target-click' meta at all --
    # TargetStatusStatic.on_click() (target_screen.py) no-ops when that
    # key is absent, which is the entire "disabled" mechanism.
    def test_not_connected_icons_are_grey_and_not_clickable(self):
        state = target.TargetState()
        state.on_event("POWER ON")
        state.on_event("STORAGE TARGET")
        rendered = target.render_target_status(state)
        self.assertEqual(self._clicks(rendered), [])
        colors = {s.style.color.name for s in rendered.spans
                  if s.style.bold}
        self.assertEqual(colors, {"grey50"})

# Full app, real Textual event loop -- the one place these pieces
# (screen, adapter, action worker, freeform-input override) are
# actually exercised together rather than as isolated units.
class TargetScreenIntegration(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.base import TargetIndicator
            from seine.tui.target_screen import TargetScreen
        self.SeineApp = SeineApp
        self.TargetIndicator = TargetIndicator
        self.TargetScreen = TargetScreen

        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        mtda_pkg = types.ModuleType("mtda")
        mtda_client_mod = types.SimpleNamespace(Client=FakeClient)
        mtda_pkg.client = mtda_client_mod
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_client_mod

        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        # This class builds a real SeineApp -- without this, its
        # History (cwd-relative by default) would write into whatever
        # directory avocado was actually run from, not a throwaway one.
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")
        # A default agent -- TargetScreen.on_mount() only auto-connects
        # when this (or an already-live client) says to; most of this
        # class is about what happens *after* a connection, same as
        # before '/target connect' existed. The no-auto-connect and
        # explicit-connect paths get their own tests further down.
        os.environ["MTDA_REMOTE"] = "192.0.2.1"

    def tearDown(self):
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client
        os.environ.pop("MTDA_REMOTE", None)
        os.environ.pop("SEINE_HISTORY_FILE", None)

    def test_slash_target_switches_screens_and_renders_both_panes(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/target"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.TargetScreen)
                # No remote configured (FakeClient default) -- console
                # pane says so instead of showing anything live.
                for _ in range(20):
                    if "not connected" in app.screen.query_one("#console").renderable:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn("not connected",
                             app.screen.query_one("#console").renderable)
                # Seeded from FakeClient.target_status() ("ON"), not left
                # blank/grey waiting for a live event that may never come.
                for _ in range(20):
                    rendered = app.screen.query_one("#targetstatus").renderable
                    icon = next((s.style for s in rendered.spans
                               if s.style.meta.get("target-click") == "power"), None)
                    if icon is not None and icon.color.name == "dark_orange":
                        break
                    await asyncio.sleep(0.02)
                self.assertIn("⏻", rendered.plain)
                self.assertEqual(icon.color.name, "dark_orange")
        _run(scenario)

    # The pyte screen stays a fixed 40 lines (scroll-drift margin,
    # target.py's own CONSOLE_LINES comment), but the widget showing it
    # must not force a 40-row box regardless of the real terminal size
    # -- a small terminal should show fewer rows, not require scrolling
    # to see the footer.
    def test_console_widget_does_not_exceed_a_small_terminal(self):
        client = FakeClient()
        client.agent.remote = "192.0.2.1"
        sys.modules["mtda.client"].Client = lambda host=None: client

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test(size=(100, 20)) as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_console", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                for i in range(60):
                    app._target_console.print(("line %d\r\n" % i).encode())
                for _ in range(50):
                    if "line 59" in app.screen.query_one("#console").renderable.plain:
                        break
                    await asyncio.sleep(0.02)
                console = app.screen.query_one("#console")
                self.assertLess(console.size.height, 40)
                self.assertLessEqual(console.size.height, app.screen.query_one("#console-pane").size.height)
        _run(scenario)

    # The console pane is the reason to be on this screen (same framing
    # IssuesScreen's table gets) -- unlike every other StaticPane, Tab
    # has to be able to reach it.
    def test_console_pane_is_a_tab_stop(self):
        from seine.tui.target_screen import ConsolePane
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(ConsolePane)
                self.assertTrue(pane.can_focus)
                pane.focus()
                await pilot.pause()
                self.assertIs(app.screen.focused, pane)
        _run(scenario)

    # A remote configured -> _connect() actually builds a ConsoleAdapter
    # -- the console pane's own tick should pick up bytes fed into it
    # without anything pushing a redraw directly.
    def test_console_pane_redraws_once_the_tick_finds_it_dirty(self):
        client = FakeClient()
        client.agent.remote = "192.0.2.1"
        sys.modules["mtda.client"].Client = lambda host=None: client

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_console", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                app._target_console.print(b"BOOTING\r\n")
                for _ in range(50):
                    if "BOOTING" in app.screen.query_one("#console").renderable.plain:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn("BOOTING",
                             app.screen.query_one("#console").renderable.plain)
        _run(scenario)

    def test_a_freeform_line_here_sends_to_the_console_not_ai_chat(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                prompt = app.screen.query_one("#prompt")
                prompt.value = "echo hi"
                await pilot.press("enter")
                for _ in range(50):
                    if ("console_send", "echo hi", False) in app._target_client.calls:
                        break
                    await asyncio.sleep(0.02)
                # _connect()'s own seed() read runs concurrently, on its
                # own worker group -- only check the freeform line's own
                # call landed, not the exact call list.
                self.assertIn(("console_send", "echo hi", False), app._target_client.calls)
        _run(scenario)

    # A console line is recallable (Up/Down) but never written to
    # history.json -- '/target ...' typed on the same screen still is,
    # same as everywhere else.
    def test_a_freeform_console_line_is_recallable_but_never_persisted(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "root"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.history.last(app.screen.HISTORY_SCOPES), "root")
                self.assertNotIn("root", [e["line"] for e in app.history.lines])

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/target status"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("/target status", [e["line"] for e in app.history.lines])
        _run(scenario)

    # target.disconnect() side-unloads the console's in-memory history --
    # a fresh connect starts recall clean, no leakage from the previous
    # agent's console session.
    def test_disconnecting_drops_the_consoles_recall(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "root"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.history.last(app.screen.HISTORY_SCOPES), "root")

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/target disconnect"
                await pilot.press("enter")
                await pilot.pause()
                # The disconnect command itself persists (it's a
                # '/command'), landing right where "root" used to be --
                # proof the side-loaded group is actually gone, not just
                # that the cursor moved.
                self.assertEqual(app.history.last(app.screen.HISTORY_SCOPES), "/target disconnect")
        _run(scenario)

    def test_clicking_power_toggles_directly_no_confirmation(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                app.screen.action_target_power_toggle()
                for _ in range(50):
                    if ("target_toggle",) in app._target_client.calls:
                        break
                    await asyncio.sleep(0.02)
                # _connect()'s own seed() read runs concurrently, on its
                # own worker group -- only check the click's own call
                # landed, not the exact call list.
                self.assertIn(("target_toggle",), app._target_client.calls)
        _run(scenario)

    # Exercises the actual click path (Rich Style meta '@click' ->
    # Textual's action DSL), not just the method it resolves to -- the
    # test above calls action_target_power_toggle() directly and would
    # never have caught the real bug: the click string embedded a
    # redundant 'action_' prefix (Textual's own DSL already adds one
    # when resolving 'screen.<name>'), so the real click silently
    # resolved nothing. A wide enough terminal (>= 84 cols, '#console-
    # pane' 's own min-width) is needed or '#targetstatus' gets
    # squeezed down to nothing.
    def test_clicking_the_power_token_in_the_status_pane_toggles_it(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test(size=(140, 45)) as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                app.target_state.power = "ON"
                app.screen._redraw_status()
                await pilot.pause()
                # Row 7: rows 0-4 are the 'Agent:' block (heading,
                # blank, remote, session, blank), row 5 is 'Controls:',
                # row 6 is blank. The icon row is indented one column
                # past 'Controls:' (no 'POWER' label). Offset by
                # #targetstatus's own 'padding: 1 2' (top 1, left 2)
                # on top of that.
                await pilot.click("#targetstatus", offset=(3, 8))
                for _ in range(50):
                    if ("target_toggle",) in app._target_client.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn(("target_toggle",), app._target_client.calls)
        _run(scenario)

    # Row 7 (rows 0-4 are the 'Agent:' block, row 5 'Controls:', row 6
    # blank). The icon row is indented one column, then "⏻ " (icon +
    # one space) is columns 1-2, a plain separator space is column 3,
    # and the double-width storage icon is columns 4-5. Offset by
    # #targetstatus's own 'padding: 1 2' (top 1, left 2) on top of
    # that.
    def test_clicking_the_storage_icon_in_the_status_pane_toggles_it(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test(size=(140, 45)) as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                app.target_state.storage = "HOST"
                app.screen._redraw_status()
                await pilot.pause()
                await pilot.click("#targetstatus", offset=(6, 8))
                for _ in range(50):
                    if ("storage_to_target",) in app._target_client.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn(("storage_to_target",), app._target_client.calls)
        _run(scenario)

    # refresh_indicators() (app.py) once only touched Indicators, not
    # TargetIndicator -- the write-progress chip lit up on the first
    # EVT but nothing ever refreshed it again once writing stopped.
    def test_footer_chip_clears_once_a_write_finishes(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                app.target_state.writing = True
                app.target_state.write_read = 50
                app.target_state.write_total = 100
                app.refresh_indicators()
                chip = app.screen.query_one(self.TargetIndicator)
                self.assertTrue(chip.display)
                app.target_state.writing = False
                app.refresh_indicators()
                self.assertFalse(chip.display)
        _run(scenario)

    def test_console_border_shows_uptime_while_on_clears_once_off(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                app.target_state.power_on_at = time.time() - 90
                app.screen._redraw_status()
                pane = app.screen.query_one("#console-pane")
                self.assertIn("up 1m30s", pane.border_subtitle)
                app.target_state.power_on_at = None
                app.screen._redraw_status()
                self.assertEqual(pane.border_subtitle, "")
        _run(scenario)

    # A bare set_interval tick has nothing upstream to catch a bad
    # render -- unguarded, that reaches Textual's own fatal-error
    # handler instead of a reportable message (a real crash this way,
    # from a colour pyte handed back that Rich's Style() rejected,
    # prompted this test).
    def test_a_redraw_crash_is_reported_not_fatal(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                from seine.tui import target as target_mod
                original = target_mod.render_target_status
                target_mod.render_target_status = lambda state: (_ for _ in ()).throw(
                    ValueError("boom"))
                try:
                    app.screen._redraw_status()
                finally:
                    target_mod.render_target_status = original
                status = app.screen.query_one("#status")
                self.assertIn("boom", status.renderable)
                self.assertTrue(status.has_class("error"))
                self.assertIsNone(app.screen._status_timer._task)
        _run(scenario)

    # No $MTDA_REMOTE (unlike every test above, via setUp) and nothing
    # connected yet -- on_mount() must not dial on its own.
    def test_no_default_agent_and_no_prior_client_does_not_auto_connect(self):
        os.environ.pop("MTDA_REMOTE", None)
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                await asyncio.sleep(0.1)  # nothing to poll for -- a dial never starts
                self.assertIsNone(getattr(app, "_target_client", None))
                self.assertIn("/target connect",
                             app.screen.query_one("#targetstatus").renderable.plain)
        _run(scenario)

    # '/target connect' works from any screen (switches to the target
    # screen as a side effect) and always dials, regardless of
    # $MTDA_REMOTE.
    def test_target_connect_command_dials_and_switches_screens(self):
        os.environ.pop("MTDA_REMOTE", None)
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                self.assertNotIsInstance(app.screen, self.TargetScreen)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/target connect"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.TargetScreen)
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                self.assertIsNotNone(app._target_client)
                self.assertEqual(app.target_state.agent, "Local")
        _run(scenario)

    def test_target_connect_with_an_agent_forwards_the_host(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/target connect 192.0.2.5:1234"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(app._target_client.host, "192.0.2.5:1234")
        _run(scenario)

    def test_target_disconnect_clears_the_client_and_shows_the_hint_again(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                for _ in range(50):
                    if getattr(app, "_target_client", None) is not None:
                        break
                    await asyncio.sleep(0.02)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/target disconnect"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsNone(app._target_client)
                self.assertIn("/target connect",
                             app.screen.query_one("#targetstatus").renderable.plain)
        _run(scenario)

# _key_to_bytes() is a pure function -- a plain SimpleNamespace stands
# in for textual.events.Key, which only ever needs .key/.character read.
class RawKeyMapping(avocado.Test):
    def setUp(self):
        with _tui_required(self):
            from seine.tui.target_screen import _key_to_bytes
        self._key_to_bytes = _key_to_bytes

    def _event(self, key, character=None):
        return types.SimpleNamespace(key=key, character=character)

    def test_escape_is_the_bios_key(self):
        self.assertEqual(self._key_to_bytes(self._event("escape")), b"\x1b")

    def test_named_keys_map_to_their_ansi_sequence(self):
        self.assertEqual(self._key_to_bytes(self._event("up")), b"\x1b[A")
        self.assertEqual(self._key_to_bytes(self._event("f5")), b"\x1b[15~")

    def test_ctrl_letter_maps_to_its_control_code(self):
        # Ctrl-C is 0x03, same as a real terminal.
        self.assertEqual(self._key_to_bytes(self._event("ctrl+c")), bytes([3]))

    def test_a_plain_character_is_encoded_as_is(self):
        self.assertEqual(self._key_to_bytes(self._event("a", character="a")), b"a")

    # Tab keeps switching panes app-wide -- raw mode does not steal it.
    def test_tab_is_not_mapped(self):
        self.assertIsNone(self._key_to_bytes(self._event("tab")))

    def test_an_unmapped_control_key_with_no_character_is_none(self):
        self.assertIsNone(self._key_to_bytes(self._event("shift+tab")))

class ConsolePaneRawMode(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.target_screen import ConsolePane
        self.SeineApp = SeineApp
        self.ConsolePane = ConsolePane

        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        mtda_pkg = types.ModuleType("mtda")
        mtda_client_mod = types.SimpleNamespace(Client=FakeClient)
        mtda_pkg.client = mtda_client_mod
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_client_mod

        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        # See TargetScreenIntegration.setUp()'s own comment -- same
        # real-SeineApp, same reason.
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")

    def tearDown(self):
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client
        os.environ.pop("SEINE_HISTORY_FILE", None)

    # No confirmation: focusing the pane is already deliberate, and
    # confirming every keystroke individually would be unusable.
    def test_focusing_the_pane_enables_raw_mode_immediately(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                self.assertTrue(pane.raw_mode)
        _run(scenario)

    # A transient reminder, not a permanent banner -- gone again after
    # the couple of seconds it's given.
    def test_focusing_shows_a_transient_warning_then_clears_it(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                status = app.screen.query_one("#status")
                self.assertIn("raw keystrokes", status.renderable)
                self.assertTrue(status.has_class("warning"))
                await asyncio.sleep(2.6)
                self.assertEqual(status.renderable, "")
                self.assertFalse(status.has_class("warning"))
        _run(scenario)

    def test_escape_reaches_the_target_once_raw_mode_is_on(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                await pilot.press("escape")
                for _ in range(50):
                    if ("console_send", b"\x1b", True) in app._target_client.calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn(("console_send", b"\x1b", True), app._target_client.calls)
        _run(scenario)

    def test_blurring_turns_raw_mode_off(self):
        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                app.show("target")
                await pilot.pause()
                pane = app.screen.query_one(self.ConsolePane)
                pane.focus()
                await pilot.pause()
                app.screen.query_one("#prompt").focus()
                await pilot.pause()
                self.assertFalse(pane.raw_mode)
        _run(scenario)
