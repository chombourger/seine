#!/usr/bin/env python3

# The --interaction-socket path (seine/tui/app.py: _start_socket_server()
# and friends, seine/tui/ai.py: AIState._new_assistant_messages()/
# _mark_sent()) gets its own file rather than folding into tests/tui/
# tui.py -- that file is already 2000+ lines of screen-by-screen UI
# behaviour, and socket wire-protocol tests read very differently (raw
# AF_UNIX client sockets, background threads) from the Pilot-driven
# ones there.

import asyncio
import avocado
import contextlib
import json
import os
import socket
import stat
import sys
import tempfile
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-tui-socket-tests-"))
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-tui-socket-tests-config-")
# Same reasoning as tui.py's own: nothing here is about the AI chat
# actually talking to a server, so a real endpoint exported in the
# shell running the suite must not leak in.
for _var in ("SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"):
    os.environ.pop(_var, None)

# Same reasoning as tui.py's own: History is cwd-relative, and every
# test here builds a real SeineApp.
os.chdir(tempfile.mkdtemp(prefix="seine-tui-socket-tests-cwd-"))

from tests.native_image import native_image

NATIVE_IMAGE = native_image()

@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

def _run(scenario):
    asyncio.run(scenario())

# A short, unprefixed-by-workdir path -- AF_UNIX addresses are capped
# at ~108 bytes on Linux, and avocado's own per-test workdir can run
# longer than that on its own.
def _socket_path():
    return os.path.join(tempfile.mkdtemp(prefix="sock-"), "s")

# A thin wrapper around a raw AF_UNIX client socket that keeps its own
# read buffer across calls -- a bare socket.recv(4096) can return
# several newline-terminated messages in one chunk (two events fired
# back to back, e.g. '/build''s own "screen_changed" immediately
# followed by "build_finished"), and a version that started a fresh
# buffer on every call silently dropped whatever came after the first
# line in that chunk.
class _Client:
    def __init__(self, path, timeout=2.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self._buf = b""

    def send(self, message):
        self.sock.sendall((json.dumps(message) + "\n").encode())

    def send_raw(self, data):
        self.sock.sendall(data)

    # Reads one newline-terminated JSON message. Returns None on
    # EOF/timeout with nothing left buffered -- callers combine this
    # with _wait_until() when the message may not have been written yet.
    def recv(self):
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return json.loads(line.decode())

    # '/build' emits its own "screen_changed" (to "build") before
    # "build_finished" -- skips past any other event type to find the
    # one a test actually cares about.
    def recv_until(self, event_type):
        while True:
            msg = self.recv()
            if msg is None or msg.get("type") == event_type:
                return msg

    def close(self):
        self.sock.close()

def _connect(path, timeout=2.0):
    return _Client(path, timeout=timeout)

# Polls *condition* while pumping both the app's own asyncio loop
# (pilot.pause(), for anything scheduled via call_from_thread()) and
# wall-clock time (plain background threads -- the socket accept/client
# threads -- advance on their own).
async def _wait_until(pilot, condition, timeout=2.0, step=0.05):
    elapsed = 0.0
    while not condition():
        await pilot.pause()
        await asyncio.sleep(step)
        elapsed += step
        if elapsed > timeout:
            raise AssertionError("timed out waiting for condition")

# Socket lifecycle: created (or not) at the right time, replaces a
# stale file, silent when the flag is omitted.
class SocketServer(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
        self.SeineApp = SeineApp

    def test_socket_file_created_when_path_given(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test():
                self.assertTrue(os.path.exists(path))
                self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))
        _run(scenario)

    def test_no_socket_server_without_the_flag(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test():
                self.assertIsNone(getattr(app, "_socket_server", None))
                # _socket_send() must stay a no-op with nothing listening,
                # not raise for lack of a server.
                app._socket_send({"type": "ai_message", "content": "x"})
        _run(scenario)

    # _start_socket_server() unlinks whatever is at the path first -- a
    # leftover regular file (e.g. from a crashed previous run) must not
    # make bind() fail.
    def test_stale_socket_file_is_replaced(self):
        async def scenario():
            path = _socket_path()
            with open(path, "w") as f:
                f.write("stale")
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test():
                self.assertTrue(stat.S_ISSOCK(os.stat(path).st_mode))
        _run(scenario)

# The wire protocol: a connected client's "input"/"ai_input" messages
# actually reach the running app.
class SocketProtocol(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
            from seine.tui.app import SeineApp
            from seine.tui.base import Prompt
        self.ai = ai
        self.SeineApp = SeineApp
        self.Prompt = Prompt

    # "input" types into the real Prompt widget and submits it -- same
    # round trip tests/tui/tui.py's own App.test_an_unknown_slash_
    # command_is_shown_not_raised (prompt.value == "/nope" after Enter,
    # not cleared) already covers for a keyboard Enter.
    def test_input_message_types_and_submits(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                conn.send({"type": "input", "text": "/nope"})
                # Waiting on history (populated by dispatch, after Enter)
                # rather than prompt.value (already "/nope" the moment
                # typing finishes, before Enter is even sent) -- the
                # latter would pass before the submit round trip in
                # _handle_socket_message() has actually run.
                lines = lambda: [e["line"] for e in app.history.entries(("commands",))]
                await _wait_until(pilot, lambda: "/nope" in lines())
                # Enter was processed by the real Prompt (on_input_
                # submitted() clears it on any submit, same as a keyboard
                # Enter -- tests/tui/tui.py's own
                # test_history_recalls_previous_commands_on_up relies on
                # the same clear-then-recall-with-Up shape), not just
                # injected straight into history.
                prompt = app.screen.query_one("#prompt", self.Prompt)
                self.assertEqual(prompt.value, "")
        _run(scenario)

    def test_ai_input_forwards_to_ai_ask(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            calls = []
            real_ask = self.ai.ask
            self.ai.ask = lambda a, prompt: calls.append((a, prompt))
            self.addCleanup(setattr, self.ai, "ask", real_ask)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                conn.send({"type": "ai_input", "prompt": "hello there"})
                await _wait_until(pilot, lambda: len(calls) == 1)
                self.assertIs(calls[0][0], app)
                self.assertEqual(calls[0][1], "hello there")
        _run(scenario)

    # A garbage line must not kill the connection for the well-formed
    # messages that follow it.
    def test_malformed_json_line_is_skipped_not_fatal(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                conn.send_raw(b"not json\n")
                conn.send({"type": "input", "text": "/nope"})
                lines = lambda: [e["line"] for e in app.history.entries(("commands",))]
                await _wait_until(pilot, lambda: "/nope" in lines())
        _run(scenario)

    # An unrecognized "type" is ignored -- and does not wedge the
    # connection for whatever comes after it.
    def test_unknown_message_type_is_a_no_op(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                conn.send({"type": "future-feature"})
                conn.send({"type": "input", "text": "/nope"})
                lines = lambda: [e["line"] for e in app.history.entries(("commands",))]
                await _wait_until(pilot, lambda: "/nope" in lines())
        _run(scenario)

# AIState's half of the bridge: which messages _socket_send_ai_messages()
# picks up, and that a connected client actually receives them.
class SocketAIBridge(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
            from seine.tui.app import SeineApp
        self.ai = ai
        self.SeineApp = SeineApp

    def test_new_assistant_messages_excludes_user_and_tool_rows(self):
        state = self.ai.AIState()
        state.messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "content": "..."},
        ]
        got = state._new_assistant_messages()
        self.assertEqual([m["content"] for m in got], ["hello"])

    def test_mark_sent_advances_the_high_water_mark(self):
        state = self.ai.AIState()
        state.messages = [{"role": "assistant", "content": "one"}]
        state._mark_sent()
        state.messages.append({"role": "assistant", "content": "two"})
        got = state._new_assistant_messages()
        self.assertEqual([m["content"] for m in got], ["two"])

    def test_changed_streams_new_assistant_reply_to_a_connected_client(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                app.ai_state.messages.append({"role": "user", "content": "hi"})
                app.ai_state.messages.append({"role": "assistant", "content": "hello there"})
                app.ai_state.changed()
                msg = conn.recv()
                self.assertEqual(msg, {"type": "ai_message", "content": "hello there"})
        _run(scenario)

    # Regression check for the original commit: AIState.changed() used
    # to swallow every exception _socket_send_ai_messages() raised
    # (broad 'except Exception: pass'), which hid the fact that the
    # method it called did not exist as an attribute of SeineApp at
    # all. Now that it is a real bound method, changed() must work
    # with nothing to raise in the first place -- no socket, no
    # listeners, no messages.
    def test_changed_with_no_socket_does_not_raise(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test():
                app.ai_state.messages.append({"role": "assistant", "content": "hello"})
                app.ai_state.changed()
                self.assertEqual(app.ai_state._last_sent_index, 0)
        _run(scenario)

# App-level events other than the AI chat: a build finishing, and the
# TUI switching screens. Both reuse choke points tests/tui/tui.py
# already exercises for their non-socket behaviour (App._build_finished(),
# App.show()) -- only the socket side is new here.
class SocketAppEvents(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.image import Image
            from seine.tui.app import PlanScreen, SeineApp
        self.Image = Image
        self.PlanScreen = PlanScreen
        self.SeineApp = SeineApp
        self.real_build = Image.build
        self.addCleanup(setattr, Image, "build", self.real_build)
        from seine import tasks
        self.addCleanup(tasks._interrupted.clear)
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_build_finished_emits_an_event(self):
        from seine import tasks

        def fast_build(image, reporter=None):
            for step in tasks.ordered(image.tasks())[:1]:
                reporter.started(step.name)
                reporter.finished(step.name, failed=False)
        self.Image.build = fast_build

        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[NATIVE_IMAGE], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                for _ in range(50):
                    if not app.build_state.running:
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                msg = conn.recv_until("build_finished")
                self.assertEqual(msg, {"type": "build_finished", "error": False,
                                       "message": "build finished"})
        _run(scenario)

    def test_screen_changed_emits_an_event(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[NATIVE_IMAGE], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                app.show("plan")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.PlanScreen)
                msg = conn.recv()
                self.assertEqual(msg, {"type": "screen_changed", "screen": "plan"})
        _run(scenario)

    # show() only switches (and only the switch is worth an event) when
    # the target differs from the current screen -- same 'or refresh'
    # branch App.show() already had before any of this.
    def test_re_showing_the_same_screen_emits_nothing(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[NATIVE_IMAGE], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                app.show("overview")
                await pilot.pause()
                self.assertIsNone(conn.recv())
        _run(scenario)

# A minimal stand-in for mtda.client.Client -- same shape/role as
# tests/tui/target.py's own FakeClient and tests/tui/ai.py's
# _FakeMtdaClient, duplicated rather than imported (test files here
# stay self-contained). Only what target_connected/target_storage_on_
# host/target_storage_write_completed actually touch.
class _FakeMtdaClient:
    def __init__(self, host=None):
        self.host = host
        self.calls = []
        self.agent = types.SimpleNamespace(remote=None)

    def start(self):
        pass

    def session(self):
        return "fake-session"

    def storage_to_host(self):
        self.calls.append(("storage_to_host",))
        return True

    def storage_to_target(self):
        self.calls.append(("storage_to_target",))
        return True

    def storage_write_image(self, path):
        self.calls.append(("storage_write_image", path))

class SocketTargetEvents(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import target
            from seine.tui.app import SeineApp
        self.target = target
        self.SeineApp = SeineApp
        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        self.client = _FakeMtdaClient()
        mtda_pkg = types.ModuleType("mtda")
        mtda_pkg.client = types.SimpleNamespace(Client=lambda host=None: self.client)
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_pkg.client
        self.addCleanup(self._restore_mtda)

    def _restore_mtda(self):
        self.target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client

    def test_connect_emits_target_connected(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[NATIVE_IMAGE], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                self.target.connect(app)
                msg = conn.recv()
                self.assertEqual(msg, {"type": "target_connected", "agent": "Local"})
        _run(scenario)

    def test_storage_to_host_emits_an_event(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[NATIVE_IMAGE], interaction_socket=path)
            async with app.run_test() as pilot:
                self.target.connect(app)
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                self.target.storage_to_host(app)
                msg = conn.recv()
                self.assertEqual(msg, {"type": "target_storage_on_host"})
        _run(scenario)

    def test_write_image_emits_target_storage_write_completed(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[NATIVE_IMAGE], interaction_socket=path)
            async with app.run_test() as pilot:
                self.target.connect(app)
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                self.target.write_image(app, "/tmp/rootfs.img")
                msg = conn.recv()
                self.assertEqual(msg, {"type": "target_storage_write_completed",
                                       "path": "/tmp/rootfs.img"})
                self.assertIn(("storage_write_image", "/tmp/rootfs.img"), self.client.calls)
                self.assertIn(("storage_to_target",), self.client.calls)
        _run(scenario)

    # target.py's functions are duck-typed over 'app' (tests/tui/
    # target.py's own Actions class calls them against a bare
    # types.SimpleNamespace()) -- _socket_send() must stay a no-op
    # there instead of an AttributeError.
    def test_connect_does_not_raise_without_socket_send(self):
        self.target.connect(types.SimpleNamespace())

# A stand-in for litellm -- same shape/role as tests/tui/ai.py's own
# fake_litellm(), duplicated rather than imported (test files here stay
# self-contained). Only what confirm_shown/confirm_resolved/
# ai_turn_finished/spec_written actually need to drive: a first-turn
# tool call (or not), a final plain-text answer on the turn after.
def fake_litellm(tool_name=None, tool_arguments="{}"):
    class Delta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls
            self.reasoning_content = None

    class Choice:
        def __init__(self, delta):
            self.delta = delta

    class Chunk:
        def __init__(self, delta):
            self.choices = [Choice(delta)]

    class Function:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class ToolCall:
        def __init__(self, id, name, arguments):
            self.id = id
            self.function = Function(name, arguments)
            self.type = "function"

    class Message:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls
            self.role = "assistant"

        def model_dump(self, exclude_none=True):
            d = {"role": "assistant"}
            if self.content is not None:
                d["content"] = self.content
            if self.tool_calls:
                d["tool_calls"] = [{"id": tc.id, "type": "function",
                                    "function": {"name": tc.function.name,
                                                "arguments": tc.function.arguments}}
                                   for tc in self.tool_calls]
            return d

    class Usage:
        def __init__(self, p, c):
            self.prompt_tokens = p
            self.completion_tokens = c

    class Response:
        def __init__(self, message, usage):
            class C:
                pass
            c = C()
            c.message = message
            self.choices = [c]
            self.usage = usage

    calls = {"n": 0}

    def completion(model, api_base, api_key, messages, tools, tool_choice,
                   stream, stream_options, timeout):
        calls["n"] += 1
        if calls["n"] == 1 and tool_name is not None:
            return iter([Chunk(Delta(tool_calls=[ToolCall("call_1", tool_name, tool_arguments)]))])
        return iter([Chunk(Delta(content="final answer"))])

    def stream_chunk_builder(chunks, messages=None):
        for c in chunks:
            if c.choices[0].delta.tool_calls:
                return Response(Message(tool_calls=c.choices[0].delta.tool_calls), Usage(10, 5))
        text = "".join(c.choices[0].delta.content or "" for c in chunks)
        return Response(Message(content=text), Usage(20, 8))

    return types.SimpleNamespace(
        completion=completion,
        stream_chunk_builder=stream_chunk_builder,
        token_counter=lambda model, messages: 1,
        get_max_tokens=lambda model: (_ for _ in ()).throw(Exception("unmapped")),
        suppress_debug_info=False,
    )

LLM_ENV = ["SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"]

def _clear_llm_env():
    for name in LLM_ENV:
        os.environ.pop(name, None)

# The AI chat's own events: a gated tool's confirm dialog appearing/
# resolving (ai.py:confirm(), the single choke point for every gated
# tool -- AI or otherwise), a turn's real end (busy True->False in
# ai.py:_run()'s finally), and a spec-update/spec-create actually
# landing on disk.
class SocketAIEvents(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.ai import ConfirmAction
            from seine.tui.app import SeineApp
        self.ConfirmAction = ConfirmAction
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_LLM_MODEL"] = "openai/fake"
        self._real_litellm = sys.modules.get("litellm")
        self.addCleanup(self._restore_litellm)

    def _restore_litellm(self):
        if self._real_litellm is not None:
            sys.modules["litellm"] = self._real_litellm
        else:
            sys.modules.pop("litellm", None)
        _clear_llm_env()

    async def _settle(self, app, pilot, ticks=100):
        for _ in range(ticks):
            if not app.ai_state.busy:
                return
            await asyncio.sleep(0.02)
            await pilot.pause()
        raise AssertionError("ai_state never settled")

    async def _wait_for_confirm(self, app, pilot, ticks=100):
        for _ in range(ticks):
            if isinstance(app.screen, self.ConfirmAction):
                return
            await asyncio.sleep(0.02)
            await pilot.pause()
        raise AssertionError("ConfirmAction never appeared")

    def _minimal_spec(self):
        path = os.path.join(self.workdir, "minimal.yaml")
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "  release: bookworm\n"
                "  architecture: amd64\n"
                "image:\n"
                "  filename: test.img\n"
                "  table: gpt\n"
                "  size: 128MiB\n"
                "  partitions:\n"
                "  - label: system\n"
                "    type: ext2\n"
                "    size: 128MiB\n"
                "    where: /\n")
        return path

    # 'cancel-build' would be the plainest trip through confirm() (gated,
    # no preview) but its own _cancel_build_preview() refuses before
    # ConfirmAction ever opens when nothing is running -- confirm() is
    # never reached at all with no build in flight (a real, pre-existing
    # gap: tests/tui/ai.py's own TheLoop class has the exact same
    # "ConfirmAction never appeared" failure using this same setup,
    # unrelated to this work). 'spec-create' is gated with a preview
    # that succeeds against a real minimal spec, so it reliably reaches
    # ConfirmAction instead.
    def test_confirm_shown_then_resolved_on_approval(self):
        spec = self._minimal_spec()
        new_path = os.path.join(os.path.dirname(spec), "from_ai.yaml")
        arguments = {"path": new_path, "content": "extra: true\n"}
        sys.modules["litellm"] = fake_litellm(tool_name="spec-create",
                                              tool_arguments=json.dumps(arguments))
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[spec], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "add a new fragment file with 'extra: true'"
                await pilot.press("enter")
                await self._wait_for_confirm(app, pilot)
                shown = conn.recv_until("confirm_shown")
                self.assertEqual(shown["type"], "confirm_shown")
                self.assertEqual(shown["tool"], "spec-create")
                self.assertEqual(shown["arguments"], arguments)
                self.assertIsInstance(shown["preview"], str)  # a real diff, not None
                await pilot.press("enter")  # 'Yes' is highlighted first
                resolved = conn.recv_until("confirm_resolved")
                self.assertEqual(resolved, {"type": "confirm_resolved",
                                            "tool": "spec-create", "approved": True})
                await self._settle(app, pilot)
        _run(scenario)

    def test_confirm_resolved_reports_a_denial(self):
        spec = self._minimal_spec()
        new_path = os.path.join(os.path.dirname(spec), "from_ai.yaml")
        arguments = {"path": new_path, "content": "extra: true\n"}
        sys.modules["litellm"] = fake_litellm(tool_name="spec-create",
                                              tool_arguments=json.dumps(arguments))
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[spec], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "add a new fragment file with 'extra: true'"
                await pilot.press("enter")
                await self._wait_for_confirm(app, pilot)
                conn.recv_until("confirm_shown")
                await pilot.press("escape")
                resolved = conn.recv_until("confirm_resolved")
                self.assertEqual(resolved, {"type": "confirm_resolved",
                                            "tool": "spec-create", "approved": False})
                self.assertFalse(os.path.exists(new_path))
                await self._settle(app, pilot)
        _run(scenario)

    def test_ai_turn_finished_emits_an_event(self):
        sys.modules["litellm"] = fake_litellm()  # no tool call, plain answer
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is sudo installed?"
                await pilot.press("enter")
                await self._settle(app, pilot)
                msg = conn.recv_until("ai_turn_finished")
                self.assertEqual(msg, {"type": "ai_turn_finished"})
        _run(scenario)

    def test_spec_create_emits_spec_written_with_the_real_path(self):
        spec = self._minimal_spec()
        new_path = os.path.join(os.path.dirname(spec), "from_ai.yaml")
        arguments = json.dumps({"path": new_path, "content": "extra: true\n"})
        sys.modules["litellm"] = fake_litellm(tool_name="spec-create", tool_arguments=arguments)
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(files=[spec], interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "add a new fragment file with 'extra: true'"
                await pilot.press("enter")
                await self._wait_for_confirm(app, pilot)
                await pilot.press("enter")  # 'Yes' is highlighted first
                msg = conn.recv_until("spec_written")
                self.assertEqual(msg, {"type": "spec_written",
                                       "path": os.path.realpath(new_path)})
                self.assertTrue(os.path.exists(new_path))
                await self._settle(app, pilot)
        _run(scenario)

# TestState (seine/tui/testing.py) gained an 'on_finished' hook, same
# shape as BuildState's own -- driven directly against a fake
# SuiteResult rather than a real Robot Framework run, same "only the
# wiring is new here" reasoning tests/tui/ai.py's own MtdaTools class
# gives for testing against a fake mtda client instead of real hardware.
class SocketTestEvents(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
        self.SeineApp = SeineApp

    def test_finished_ok_emits_an_event(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                result = types.SimpleNamespace(ok=True, summary=lambda: "4 tests, 4 passed")
                app.test_state.finished_ok(result)
                msg = conn.recv()
                self.assertEqual(msg, {"type": "test_finished", "error": False,
                                       "message": "4 tests, 4 passed"})
        _run(scenario)

    def test_finished_failed_emits_an_event(self):
        async def scenario():
            path = _socket_path()
            app = self.SeineApp(interaction_socket=path)
            async with app.run_test() as pilot:
                conn = _connect(path)
                self.addCleanup(conn.close)
                await _wait_until(pilot, lambda: len(app._socket_clients) == 1)
                app.test_state.finished_failed("robotframework is not installed")
                msg = conn.recv()
                self.assertEqual(msg, {"type": "test_finished", "error": True,
                                       "message": "robotframework is not installed"})
        _run(scenario)
