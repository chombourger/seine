#!/usr/bin/env python3

import asyncio
import avocado
import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-ai-tests-"))
os.chdir(tempfile.mkdtemp(prefix="seine-ai-tests-cwd-"))

from tests.native_image import native_image

NATIVE_IMAGE = native_image()
REBUILD_BUSYBOX = os.path.join(path_to_sources, "examples", "rebuild-busybox", "main.yaml")

# Same helper, same reason, as 'tests/tui/tui.py' 's own -- not
# imported from there (test files stay self-contained here, none of
# them import each other): 'Static' keeps what 'update()' gave it in a
# private attribute, '_content' on the python3-textual Debian trixie
# actually ships, '__content' (name-mangled) on newer pip releases.
def _content(widget):
    if hasattr(widget, "_content"):
        return widget._content
    return getattr(widget, "_Static__content", "")

# '#chatlog' mounts one widget per row now (Static for everything but a
# finished assistant reply, which is a real 'Markdown' widget) rather
# than writing lines into a 'RichLog' -- this flattens it back into the
# same (text, style) pairs the old 'RichLog.lines' gave tests, so the
# assertions below didn't have to change shape, just how they're built.
# A 'Markdown' row's style always reads as None: nothing here needs to
# tell a rendered reply's own inline styles apart, only find it by text.
def _chat_rows(log):
    from textual.widgets import Markdown
    rows = []
    for widget in log.children:
        if isinstance(widget, Markdown):
            rows.append((widget._markdown or "", None))
        else:
            content = _content(widget)
            rows.append((str(content), getattr(content, "style", None)))
    return rows

# The three env vars ('SEINE_LLM_MODEL' etc.) are process-global state,
# same as any other 'SEINE_*' override elsewhere in this suite -- popped
# in every 'setUp()' below, not just where a test sets its own, so a
# leftover from one test can never leak into the next one run in the
# same process.
LLM_ENV = ["SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"]

def _clear_llm_env():
    for name in LLM_ENV:
        os.environ.pop(name, None)

def _run(scenario):
    asyncio.run(scenario())

# Duplicated rather than imported from tests/tui/tui.py -- the same
# "test files stay self-contained" reason given at the top of this file.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

class Configuration(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
        self.ai = ai
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        _clear_llm_env()

    def test_not_configured_with_nothing_set(self):
        self.assertFalse(self.ai.configured())

    def test_settings_json_configures_it(self):
        from seine import settings
        current = settings.load()
        current["llm_model"] = "openai/from-settings"
        settings.save(current)
        self.assertTrue(self.ai.configured())
        model, api_base, api_key = self.ai._resolved()
        self.assertEqual(model, "openai/from-settings")

    # SEINE_LLM_MODEL alone configures it, no settings.json needed -- a
    # test, or a quick one-off run, can point this at a real endpoint.
    def test_env_var_alone_configures_it(self):
        os.environ["SEINE_LLM_MODEL"] = "openai/from-env"
        self.assertTrue(self.ai.configured())

    def test_env_var_overrides_settings_json(self):
        from seine import settings
        current = settings.load()
        current["llm_model"] = "openai/from-settings"
        current["llm_api_base"] = "http://from-settings/v1"
        settings.save(current)
        os.environ["SEINE_LLM_MODEL"] = "openai/from-env"
        os.environ["SEINE_LLM_API_BASE"] = "http://from-env/v1"
        model, api_base, api_key = self.ai._resolved()
        self.assertEqual(model, "openai/from-env")
        self.assertEqual(api_base, "http://from-env/v1")

    # No settings.json field for the key at all -- it is the only source.
    def test_api_key_comes_from_the_environment_only(self):
        os.environ["SEINE_LLM_API_KEY"] = "secret"
        _, _, api_key = self.ai._resolved()
        self.assertEqual(api_key, "secret")

# The maintainer-only editing guidance at the top of
# seine/data/system_prompt.txt (the tag convention, why the marker
# exists) costs tokens on every single turn if it ever reaches the
# wire -- '_system_prompt()' is what keeps it off.
class SystemPromptPrelude(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
        self.ai = ai

    def test_the_editing_guidance_above_the_marker_is_not_sent(self):
        sent = self.ai._system_prompt()
        with open(self.ai.SYSTEM_PROMPT_FILE) as f:
            whole = f.read()
        prelude, _, _ = whole.partition("\n---\n")
        self.assertNotIn("editing guidance", sent)
        self.assertNotIn(prelude.strip(), sent)

    # What actually reaches the model: the rules, tagged, and the one
    # line telling it not to repeat a tag to the person it's talking
    # with -- the operationally relevant half of the convention.
    def test_the_rules_and_the_no_leak_reminder_are_sent(self):
        sent = self.ai._system_prompt()
        self.assertIn("[SCOPE]", sent)
        self.assertIn("never mention one to the person", sent)

    # [GATED] names every gated tool literally -- a new one added to
    # TOOLS without a matching update here would leave the model with no
    # warning that it needs approval before calling it.
    def test_every_gated_tool_is_named_in_the_gated_rule(self):
        sent = self.ai._system_prompt()
        gated = [name for name, tool in self.ai.TOOLS.items() if tool.gated]
        for name in gated:
            self.assertIn("'%s'" % name, sent)

    # A file edited without the marker (someone dropped it by mistake)
    # degrades to sending the whole file rather than silently sending
    # nothing -- a broken split must not leave the AI chat mute.
    def test_a_missing_marker_falls_back_to_the_whole_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False) as f:
            f.write("no marker in this file at all\n")
            path = f.name
        try:
            original = self.ai.SYSTEM_PROMPT_FILE
            self.ai.SYSTEM_PROMPT_FILE = path
            self.assertEqual(self.ai._system_prompt(),
                             "no marker in this file at all\n")
        finally:
            self.ai.SYSTEM_PROMPT_FILE = original
            os.unlink(path)

class ToolTable(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
            from seine.tui.app import SeineApp
        self.ai = ai
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_GISTS_DIR"] = os.path.join(self.workdir, "gists")
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "workbench")
        _clear_llm_env()

    def test_every_tool_has_a_matching_schema(self):
        names = set(self.ai.TOOLS)
        schema_names = {s["function"]["name"] for s in self.ai.TOOL_SCHEMAS}
        self.assertEqual(names, schema_names)

    # The only tools allowed to write -- every other tool has to stay
    # read-only, checked here so a new tool trips this if it forgets to
    # say so.
    def test_only_the_named_actions_are_gated(self):
        gated = {name for name, tool in self.ai.TOOLS.items() if tool.gated}
        self.assertEqual(gated, {"start-build", "cancel-build",
                                 "start-vendor", "cancel-vendor",
                                 "spec-update", "spec-create",
                                 "side-load", "side-unload",
                                 "gist-create", "gist-delete",
                                 "source-pull", "source-rm", "issues-scan",
                                 "mtda-power", "mtda-usb", "mtda-storage", "mtda-write-image",
                                 "mtda-snapshot", "mtda-rollback",
                                 "mtda-console-send", "mtda-console-run", "run-test"})

    def test_read_only_tools_work_with_no_active_spec(self):
        app = self.SeineApp()
        for name in ["overview", "plan", "packages", "analyze", "artifacts",
                    "cache", "vendor", "vendor-why", "doctor", "installed-packages",
                    "issues", "build-status",
                    "task-log", "spec-files", "read", "spec-dump", "docs", "spec-query",
                    "gist-list", "gist-show", "source-list",
                    "mtda-status", "mtda-console-read", "mtda-console-wait",
                    "test-result", "test-validate"]:
            text = self.ai.TOOLS[name].run(app, {})
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 0)

# Fakes the whole shape 'seine/tui/ai.py' reads off a real 'litellm'
# response/stream -- kept as small as the real thing actually needs, not
# a general mock. Two calls: the first always asks for one tool (given
# by the test), the second always answers with plain content -- enough
# to exercise the whole loop without a real endpoint (that part is
# 'RealEndpoint' below, opt-in).
def fake_litellm(tool_name=None, tool_arguments="{}", captured_messages=None):
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
        if captured_messages is not None:
            captured_messages.append(messages)
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

# A stand-in for mtda.client.Client -- same shape/role as tests/tui/
# target.py's own FakeClient, duplicated rather than imported (test
# files here stay self-contained, none import another).
class _FakeMtdaClient:
    def __init__(self, host=None):
        self.host = host
        self.calls = []
        self.agent = types.SimpleNamespace(remote=None)

    def start(self):
        pass

    def session(self):
        return "fake-session"

    def target_on(self):
        self.calls.append(("target_on",))

    def target_off(self):
        self.calls.append(("target_off",))

    def target_toggle(self):
        self.calls.append(("target_toggle",))

    def target_status(self):
        return "ON"

    def target_uptime(self):
        return 42

    def usb_on(self, ndx):
        self.calls.append(("usb_on", ndx))

    def usb_off(self, ndx):
        self.calls.append(("usb_off", ndx))

    def usb_toggle(self, ndx):
        self.calls.append(("usb_toggle", ndx))

    def usb_ports(self):
        return []

    def storage_write_image(self, path):
        self.calls.append(("storage_write_image", path))

    def storage_to_host(self):
        self.calls.append(("storage_to_host",))

    def storage_to_target(self):
        self.calls.append(("storage_to_target",))

    def storage_commit(self):
        self.calls.append(("storage_commit",))

    def storage_rollback(self):
        self.calls.append(("storage_rollback",))

    def storage_status(self):
        return ("idle", 0, 0)

    def console_send(self, data, raw=False):
        self.calls.append(("console_send", data, raw))

    def console_run(self, cmd):
        self.calls.append(("console_run", cmd))
        return "output"

    def console_dump(self):
        return "log so far"

    def console_head(self):
        return "first line"

    def console_tail(self):
        return "last line"

    def console_wait(self, what, timeout=None):
        self.calls.append(("console_wait", what, timeout))
        return "matched line"

# Only wiring is tested here (arguments -> the right target.py call,
# with the right shape of result) -- target.py's own suite already
# covers the underlying actions, mtda quirks, and the console pipeline
# in full.
class MtdaTools(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai, target
        self.ai = ai
        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)
        self.client = _FakeMtdaClient()
        mtda_pkg = types.ModuleType("mtda")
        mtda_pkg.client = types.SimpleNamespace(Client=lambda host=None: self.client)
        sys.modules["mtda"] = mtda_pkg
        sys.modules["mtda.client"] = mtda_pkg.client
        self.app = types.SimpleNamespace(
            _target_client=None,
            target_state=target.TargetState(),
            ai_state=types.SimpleNamespace(
                busy=False, messages=[], turn_started_at=None,
                changed=lambda: None),
            say=lambda *a, **k: None,
            # Runs 'fn' inline -- good enough for wiring tests, no real
            # Textual App needed. Except group 'ai': that's a real chat
            # turn (network and all), covered by 'TheLoop' below, so it
            # is left un-started here instead of actually firing.
            run_worker=lambda fn, **k: fn() if k.get("group") != "ai" else None,
            call_from_thread=lambda fn, *a: fn(*a))

    def tearDown(self):
        from seine.tui import target
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client

    def test_mtda_power_dispatches_to_the_matching_verb(self):
        self.assertEqual(self.ai.TOOLS["mtda-power"].run(self.app, {"state": "on"}),
                         "target on: done")
        self.assertEqual(self.client.calls, [("target_on",)])

    def test_mtda_power_rejects_a_bad_state_without_touching_the_client(self):
        text = self.ai.TOOLS["mtda-power"].run(self.app, {"state": "sideways"})
        self.assertIn("'state'", text)
        self.assertEqual(self.client.calls, [])

    def test_mtda_usb_passes_the_port_through(self):
        self.ai.TOOLS["mtda-usb"].run(self.app, {"port": "2", "state": "off"})
        self.assertEqual(self.client.calls, [("usb_off", 2)])

    def test_mtda_storage_dispatches_to_the_matching_side(self):
        self.assertEqual(self.ai.TOOLS["mtda-storage"].run(self.app, {"where": "host"}),
                         "storage host: done")
        self.assertEqual(self.client.calls, [("storage_to_host",)])

    def test_mtda_storage_rejects_a_bad_side_without_touching_the_client(self):
        text = self.ai.TOOLS["mtda-storage"].run(self.app, {"where": "sideways"})
        self.assertIn("'where'", text)
        self.assertEqual(self.client.calls, [])

    def test_mtda_write_image_then_attaches_storage_to_the_target(self):
        self.ai.TOOLS["mtda-write-image"].run(self.app, {"path": "/tmp/x.img"})
        self.assertEqual(self.client.calls,
                         [("storage_write_image", "/tmp/x.img"), ("storage_to_target",)])

    def test_mtda_snapshot_and_rollback(self):
        self.ai.TOOLS["mtda-snapshot"].run(self.app, {})
        self.ai.TOOLS["mtda-rollback"].run(self.app, {})
        self.assertEqual(self.client.calls, [("storage_commit",), ("storage_rollback",)])

    def test_mtda_console_send_and_run(self):
        self.ai.TOOLS["mtda-console-send"].run(self.app, {"data": "root\n"})
        text = self.ai.TOOLS["mtda-console-run"].run(self.app, {"command": "uname -a"})
        self.assertEqual(text, "output")
        self.assertEqual(self.client.calls,
                         [("console_send", "root\n", True), ("console_run", "uname -a")])

    def test_mtda_console_read_defaults_to_tail_and_dispatches_by_which(self):
        self.assertEqual(self.ai.TOOLS["mtda-console-read"].run(self.app, {}), "last line")
        self.assertEqual(self.ai.TOOLS["mtda-console-read"].run(
            self.app, {"which": "head"}), "first line")
        self.assertEqual(self.ai.TOOLS["mtda-console-read"].run(
            self.app, {"which": "dump"}), "log so far")

    # setUp's fake worker runs inline, so this covers the whole round
    # trip in one call: kick off, mtda call, then the follow-up turn.
    def test_mtda_console_wait_runs_in_the_background_then_notifies(self):
        text = self.ai.TOOLS["mtda-console-wait"].run(
            self.app, {"what": "login:", "timeout": 5})
        self.assertIn("waiting in the background for 'login:'", text)
        self.assertEqual(self.client.calls, [("console_wait", "login:", 5.0)])
        self.assertFalse(self.app.target_state.waiting)
        self.assertTrue(self.app.ai_state.busy)
        self.assertEqual(self.app.ai_state.messages[-1]["role"], "user")
        self.assertIn("matched line", self.app.ai_state.messages[-1]["content"])

    def test_mtda_console_wait_refuses_a_second_call_while_one_is_running(self):
        self.app.target_state.waiting = True
        self.app.target_state.wait_what = "login:"
        text = self.ai.TOOLS["mtda-console-wait"].run(self.app, {"what": "prompt:"})
        self.assertIn("already waiting", text)
        self.assertEqual(self.client.calls, [])

    def test_mtda_status_reports_all_four_fields(self):
        text = self.ai.TOOLS["mtda-status"].run(self.app, {})
        self.assertIn("power: ON", text)
        self.assertIn("uptime: 42s", text)

class TheLoop(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.chat import ChatScreen
        self.SeineApp = SeineApp
        self.ChatScreen = ChatScreen
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
        self.fail("ai_state never settled")

    # Same fixture as 'ToolTable._minimal_spec()' -- duplicated rather
    # than imported, the same "test files stay self-contained" reason
    # given at the top of this file, just applied class to class here
    # instead of file to file ('_settle' just above is the same shape).
    def _minimal_spec(self):
        path = os.path.join(self.workdir, "minimal.yaml")
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "  release: bookworm\n"
                "  architecture: amd64\n"
                "playbook:\n"
                "- name: some great packages\n"
                "  tasks:\n"
                "  - name: install vim\n"
                "    apt:\n"
                "      name:\n"
                "      - vim\n"
                "      state: present\n"
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

    # A read-only tool call, dispatched and looped back in with no
    # confirmation needed -- 'doctor' picked because it needs nothing
    # from the active spec.
    def test_a_read_only_tool_call_completes_the_conversation(self):
        sys.modules["litellm"] = fake_litellm(tool_name="doctor")
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is this machine ready?"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.ChatScreen)
                await self._settle(app, pilot)
                roles = [m["role"] for m in app.ai_state.messages]
                self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
                self.assertEqual(app.ai_state.messages[-1]["content"], "final answer")
                self.assertEqual(app.ai_state.prompt_tokens, 30)      # 10 + 20
                self.assertEqual(app.ai_state.completion_tokens, 13)  # 5 + 8
        _run(scenario)

    # '_live_status()' lands in the *system* message sent on the wire,
    # every turn -- not in 'app.ai_state.messages' (so it never shows in
    # '#chatlog'), and not only when a question happens to be about the
    # build. Checked against the real request 'litellm.completion()'
    # would receive, not just the function in isolation.
    def test_live_status_reaches_the_system_prompt_every_turn(self):
        captured = []
        sys.modules["litellm"] = fake_litellm(tool_name="doctor", captured_messages=captured)
        async def scenario():
            app = self.SeineApp()
            app.build_state.order = ["rootfs"]
            app.build_state.done = True
            app.build_state.error = False
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is this machine ready?"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                self.assertEqual(len(captured), 2)  # the tool call, then the final answer
                for messages in captured:
                    system = messages[0]["content"]
                    self.assertIn("currently: finished", system)
                # Never in the visible transcript.
                for message in app.ai_state.messages:
                    self.assertNotIn("currently: finished", str(message.get("content", "")))
        _run(scenario)

    # Colour, not a 'you:'/'seine:' prefix, tells the two apart -- dim
    # (plus a leading '| ') for what a person typed, plain for the
    # model's own words. A tool call's own raw result stays out of
    # '#chatlog' by default -- one collapsed row, an icon and the tool's
    # name -- only the question and the words actually meant to be read
    # show in full. Blank lines separate one turn from the next.
    def test_messages_are_colour_coded_and_blank_line_separated(self):
        sys.modules["litellm"] = fake_litellm(tool_name="doctor")
        async def scenario():
            from textual.containers import VerticalScroll
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is this machine ready?"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                log = app.screen.query_one("#chatlog", VerticalScroll)
                rows = _chat_rows(log)
                by_text = {text: style for text, style in rows}
                self.assertIn("dim", str(by_text["| is this machine ready?"]))
                self.assertNotIn("dim", str(by_text.get("final answer", "")))
                self.assertIn("▸ 🔧 doctor", by_text)
                tool_content = [m for m in app.ai_state.messages
                               if m["role"] == "tool"][0]["content"]
                self.assertNotIn("    " + tool_content.split("\n")[0], by_text)
                # A blank row between the question and the tool row, and
                # another between the tool row and the final answer --
                # but nothing trailing after the last group (no more
                # defensive blank line after every single write).
                texts = [text for text, _ in rows]
                self.assertEqual(texts[1], "")
                self.assertEqual(texts[texts.index("final answer") - 1], "")
                self.assertEqual(texts[-1], "final answer")
        _run(scenario)

    # A dispatched tool call with no matching 'role: tool' result yet
    # gets its own row. Built directly rather than through a real gated
    # call: the fake harness resolves a tool call synchronously, too
    # fast to ever observe this in-between state.
    def test_pending_tool_call_shows_its_own_row_in_the_log(self):
        async def scenario():
            from textual.containers import VerticalScroll
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                app.ai_state.messages = [
                    {"role": "user", "content": "add sudo"},
                    {"role": "assistant", "tool_calls": [
                        {"id": "call_1", "function": {"name": "spec-update", "arguments": "{}"}}]},
                ]
                app.ai_state.changed()
                await pilot.pause()
                log = app.screen.query_one("#chatlog", VerticalScroll)
                rows = _chat_rows(log)
                joined = "\n".join(text for text, _ in rows)
                self.assertIn("spec-update", joined)
                self.assertIn("Working…", joined)
                # No '@click' meta -- there's nothing to expand yet. A
                # plain-string style (e.g. "dim") never carries meta at
                # all, only a real 'Style(meta=...)' object can.
                for _, style in rows:
                    if hasattr(style, "meta"):
                        self.assertNotIn("@click", str(style.meta))
        _run(scenario)

    def test_draft_shows_a_trailing_cursor_and_reclaims_space_when_done(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                screen = app.screen
                draft = screen.query_one("#draft")
                self.assertFalse(draft.display)  # nothing streaming yet
                screen._on_delta("The answer")
                await pilot.pause()
                self.assertTrue(draft.display)
                self.assertTrue(str(draft.renderable).endswith("▌"))
                self.assertIn("The answer", str(draft.renderable))
                screen._on_delta_done()
                await pilot.pause()
                self.assertFalse(draft.display)  # reclaimed once the reply lands
        _run(scenario)

    # '#working' is no longer a widget of its own -- 'Working…' is
    # '#chatcol' 's own 'border_subtitle', costing no row either way.
    # Set while busy, cleared (not just blanked) once idle -- an empty
    # 'border_subtitle' is what makes the bottom border plain again.
    def test_working_status_lives_on_the_chatcol_border(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                screen = app.screen
                chatcol = screen.query_one("#chatcol")
                app.ai_state.busy = True
                app.ai_state.turn_started_at = time.time()
                screen._tick_working()
                await pilot.pause()
                self.assertIn("Working", chatcol.border_subtitle)
                app.ai_state.busy = False
                screen._tick_working()
                await pilot.pause()
                self.assertEqual(chatcol.border_subtitle, "")
        _run(scenario)

    # A real bug: RichLog paints $surface by default, #draft was left
    # transparent, so the screen's darker background showed through as a
    # separate pane. Pinned on the actual resolved colours -- #chatcol/
    # #chatlog/#draft all the same $background, and the border genuinely
    # distinct from it, not assumed close enough by eye.
    def test_chat_frame_has_one_consistent_background_and_a_visible_border(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                chatcol = app.screen.query_one("#chatcol")
                bg = chatcol.background_colors
                for wid in ["chatlog", "draft"]:
                    self.assertEqual(app.screen.query_one("#" + wid).background_colors, bg)
                border_color = chatcol.styles.border_top[1]
                # Distinct enough from the frame's own background to
                # actually read as a border, not asserted equal to one
                # specific value -- only that it isn't lost in it.
                self.assertGreater(abs(border_color.r - bg[1].r), 40)
        _run(scenario)

    # #chatrow (chat plus stats) matched itself, but #main (SpecTree/
    # StaticPane, shared by every screen) still painted Textual's widget
    # defaults, visibly lighter. Scoped to ChatScreen only -- the second
    # half of this test proves that scoping held.
    def test_chat_screens_top_half_matches_its_own_bottom_half(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                chat_bg = app.screen.query_one("#chatcol").background_colors
                for wid in ["main", "spectree", "cmd"]:
                    self.assertEqual(app.screen.query_one("#" + wid).background_colors, chat_bg)

                app.show("overview")
                await pilot.pause()
                # A screen other than chat is untouched -- 'SpecTree' 's
                # own default background, not forced to match anything.
                self.assertNotEqual(
                    app.screen.query_one("#spectree").background_colors, chat_bg)
        _run(scenario)

    # export_screenshot(), not widget.background_colors like the two
    # tests above: get_style_at() reported the scrollbar's cells as
    # matching $background when a real SVG export still showed a black
    # fill underneath -- background alone never reaches a ScrollView's
    # own scrollbar-* properties.
    def test_chatlogs_scrollbar_has_no_stray_black_fill(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                svg = app.export_screenshot()
                self.assertNotIn('fill="#000000"', svg)
        _run(scenario)

    # Clicking a collapsed tool row expands it in place -- the same
    # '@click' style-meta mechanism Textual's own markup links use, so
    # this drives it the same way a real click would: through
    # 'ChatScreen.action_toggle_tool', not by reaching into '_expanded'
    # directly.
    def test_clicking_a_tool_row_expands_and_collapses_its_result(self):
        sys.modules["litellm"] = fake_litellm(tool_name="doctor")
        async def scenario():
            from textual.containers import VerticalScroll
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is this machine ready?"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                screen = app.screen
                tool_message = [m for m in app.ai_state.messages
                                if m["role"] == "tool"][0]
                call_id = tool_message["tool_call_id"]
                first_line = "    " + tool_message["content"].split("\n")[0]
                log = screen.query_one("#chatlog", VerticalScroll)

                # The expanded result is one Static holding the whole
                # multi-line string, not split into one row per line the
                # way 'RichLog.write()' used to -- checked as a substring
                # of the joined transcript rather than exact row membership.
                def texts():
                    return [text for text, _ in _chat_rows(log)]

                def transcript():
                    return "\n".join(texts())

                self.assertNotIn(first_line, transcript())
                screen.action_toggle_tool(call_id)
                await pilot.pause()
                self.assertIn("▾ 🔧 doctor", texts())
                self.assertIn(first_line, transcript())
                screen.action_toggle_tool(call_id)
                await pilot.pause()
                self.assertIn("▸ 🔧 doctor", texts())
                self.assertNotIn(first_line, transcript())
        _run(scenario)

    # A real mouse click, not calling 'action_toggle_tool' directly like
    # the test above -- this is the one that actually exercises the
    # '@click' style-meta dispatch (a click landing on the row's own
    # 'Static' widget, not this 'Screen', resolves the action against
    # whichever of the two the meta string names).
    def test_clicking_a_tool_row_in_the_chatlog_expands_it(self):
        sys.modules["litellm"] = fake_litellm(tool_name="doctor")
        async def scenario():
            from textual.containers import VerticalScroll
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is this machine ready?"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                log = app.screen.query_one("#chatlog", VerticalScroll)
                row = next(widget for widget in log.children
                          if str(_content(widget)).endswith("🔧 doctor"))
                await pilot.click(row)
                await pilot.pause()
                texts = [text for text, _ in _chat_rows(log)]
                self.assertIn("▾ 🔧 doctor", texts)
        _run(scenario)

    # #draft reads as the next line of the same conversation, not a
    # status bar under it -- the border has to sit on #chatcol as a
    # whole, never on #chatlog alone, or it wouldn't read as one panel.
    def test_the_chat_panels_own_border_wraps_draft_too(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                screen = app.screen
                self.assertFalse(bool(screen.query_one("#chatlog").styles.border))
                self.assertTrue(bool(screen.query_one("#chatcol").styles.border))
        _run(scenario)

    # The working indicator ticks from the moment a question is asked;
    # once a token arrives, #draft shows the growing reply and the
    # border's subtitle keeps ticking beside it, not replaced by it.
    # Driven directly (_on_delta/_tick_working called by hand) rather
    # than a real streaming worker: a race between a gated fake stream
    # and this test's poll loop hung avocado often enough not to be
    # worth it -- the thread handoff itself is exercised elsewhere.
    def test_draft_and_working_indicator_both_show_while_busy(self):
        sys.modules["litellm"] = fake_litellm()
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                app.show("chat")
                await pilot.pause()
                screen = app.screen
                app.ai_state.busy = True
                app.ai_state.turn_started_at = time.time()
                screen._on_delta("partial")
                screen._tick_working()
                await pilot.pause()
                self.assertIn("partial", _content(screen.query_one("#draft")))
                self.assertIn("Working", screen.query_one("#chatcol").border_subtitle)
        _run(scenario)

    def test_reopening_chat_replays_the_transcript(self):
        sys.modules["litellm"] = fake_litellm(tool_name="doctor")
        async def scenario():
            from textual.containers import VerticalScroll
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "is this machine ready?"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/overview"
                await pilot.press("enter")
                await pilot.pause()
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/chat"
                await pilot.press("enter")
                await pilot.pause()
                log = app.screen.query_one("#chatlog", VerticalScroll)
                transcript = "\n".join(text for text, _ in _chat_rows(log))
                self.assertIn("is this machine ready?", transcript)
                self.assertIn("final answer", transcript)
        _run(scenario)

    # A gated tool opens 'ConfirmAction'; approving it runs the real
    # action ('cancel-build' picked here -- real, but side-effect-free
    # with nothing running, unlike 'start-build').
    def test_approving_a_gated_tool_runs_it(self):
        sys.modules["litellm"] = fake_litellm(tool_name="cancel-build")
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp()
            app.build_state.worker = types.SimpleNamespace(is_running=True)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "cancel the build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle(app, pilot)
                tool_result = [m for m in app.ai_state.messages if m["role"] == "tool"][0]
                self.assertEqual(tool_result["content"],
                                 "cancelling -- waiting for running steps to finish")
        _run(scenario)

    # 'mtda-power' is one of the gated tools with no 'preview' -- it
    # still falls back to confirm()'s own plain dump of 'arguments',
    # which a tool with a real 'preview' (cancel-build, since gaining
    # one) no longer goes through -- a future gated tool without a
    # preview that takes an argument worth showing must not ask a
    # person to approve it blind either.
    def test_a_gated_tools_own_arguments_are_shown_before_approval(self):
        sys.modules["litellm"] = fake_litellm(
            tool_name="mtda-power", tool_arguments='{"state": "on"}')
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "turn the target on"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                self.assertIn("state: on", _content(app.screen.query_one("#confirmargs")))
                await pilot.press("escape")
                await pilot.pause()
                await self._settle(app, pilot)
        _run(scenario)

    def test_denying_a_gated_tool_does_not_run_it(self):
        sys.modules["litellm"] = fake_litellm(tool_name="cancel-build")
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp()
            app.build_state.worker = types.SimpleNamespace(is_running=True)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "cancel the build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                await pilot.press("down")
                await pilot.press("enter")  # 'No'
                await pilot.pause()
                await self._settle(app, pilot)
                tool_result = [m for m in app.ai_state.messages if m["role"] == "tool"][0]
                self.assertEqual(tool_result["content"], "denied by user")
        _run(scenario)

    # 'Escape' denies too -- the same "back out, nothing happened"
    # 'HelpScreen'/'SettingsScreen' already give every other modal.
    def test_escape_denies_a_gated_tool(self):
        sys.modules["litellm"] = fake_litellm(tool_name="cancel-build")
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp()
            app.build_state.worker = types.SimpleNamespace(is_running=True)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "cancel the build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                await self._settle(app, pilot)
                tool_result = [m for m in app.ai_state.messages if m["role"] == "tool"][0]
                self.assertEqual(tool_result["content"], "denied by user")
        _run(scenario)

    # A real hang: quitting while ConfirmAction sat open left the AI
    # worker thread blocked forever on _confirm()'s bare event.wait() --
    # nothing was ever going to call resolved() once the app was gone.
    # Driven via app.exit() directly, since there's no #prompt to type
    # /quit into with a modal focused.
    def test_quitting_with_a_confirm_modal_open_does_not_hang(self):
        sys.modules["litellm"] = fake_litellm(tool_name="cancel-build")
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp()
            app.build_state.worker = types.SimpleNamespace(is_running=True)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "cancel the build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                app.exit()
                await asyncio.wait_for(pilot.pause(), timeout=5)
        _run(scenario)

    # 'spec-update' is the first gated tool with a 'preview' -- this is
    # the one place the whole wiring (tool.preview -> a redacted diff ->
    # 'ConfirmAction' rendering it coloured, with the file named) is
    # actually exercised end to end, not just the plain-text tool
    # functions in 'ToolTable' above.
    def test_gated_spec_update_shows_the_file_and_a_colored_diff(self):
        main = self._minimal_spec()
        args = json.dumps({"path": main, "at": "$.playbook[0].tasks[0].apt.name",
                           "value": "sudo", "mode": "append"})
        sys.modules["litellm"] = fake_litellm(tool_name="spec-update", tool_arguments=args)
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp(files=[main])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "add sudo"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                self.assertIn(main, _content(app.screen.query_one("#confirmfile")))
                diff = _content(app.screen.query_one("#confirmdiff"))
                self.assertIn("+      - sudo", diff)
                # Coloured, not plain text -- an added line carries the
                # rgb(38,97,0) background span 'ai._diff_text()' sets.
                added = [s for s in diff.spans if "266100" in (s.style or "")]
                self.assertTrue(added)
                await pilot.press("escape")
                await pilot.pause()
                await self._settle(app, pilot)
        _run(scenario)

    # Approving 'spec-update' has to reload the active spec from disk
    # and highlight what changed on the tree, the same mechanism
    # '/side-load' 's own highlight already uses -- checked by re-reading
    # 'app.context.builds[0].spec' (proof the reload actually ran, not
    # just the file on disk) and 'SpecTree._changed' (proof the tree
    # actually got the same treatment, not just a silent reload).
    def test_approving_spec_update_reloads_and_highlights_the_tree(self):
        main = self._minimal_spec()
        args = json.dumps({"path": main, "at": "$.playbook[0].tasks[0].apt.name",
                           "value": "sudo", "mode": "append"})
        sys.modules["litellm"] = fake_litellm(tool_name="spec-update", tool_arguments=args)
        async def scenario():
            from seine.tui.ai import ConfirmAction
            from seine.tui.spectree import SpecTree
            app = self.SeineApp(files=[main])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "add sudo"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle(app, pilot)
                self.assertIn("sudo", app.context.builds[0].spec["playbook"][0]
                             ["tasks"][0]["apt"]["name"])
                tree = app.screen.query_one(SpecTree)
                self.assertTrue(tree._changed)
        _run(scenario)

    # Approving 'side-load' has to actually run
    # 'app.context.side_load()' -- checked by re-reading
    # 'app.context.builds[0].spec' after approval, not by trusting the
    # tool's own return text, the same discipline
    # 'test_spec_update_preview_then_run_appends_one_item' (ToolTable)
    # already applies to a real file write.
    def test_gated_side_load_shows_the_diff_and_actually_loads_it(self):
        main = self._minimal_spec()
        fragment = os.path.join(self.workdir, "extra-fragment.yaml")
        with open(fragment, "w") as f:
            f.write("playbook:\n- name: extra play\n  tasks: []\n")
        args = json.dumps({"fragment": fragment})
        sys.modules["litellm"] = fake_litellm(tool_name="side-load", tool_arguments=args)
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp(files=[main])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "load the extra fragment"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                self.assertIn(fragment, _content(app.screen.query_one("#confirmfile")))
                diff = _content(app.screen.query_one("#confirmdiff"))
                self.assertIn("extra play", diff)
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle(app, pilot)
                self.assertEqual(len(app.context.builds[0].spec["playbook"]), 2)
        _run(scenario)

    # The AI-tool vendor of the side-load test just above -- approving
    # 'side-unload' has to actually run 'app.context.side_unload()', not
    # just say so. The fragment is side-loaded directly on 'app.context'
    # before the scenario starts (plain Python, no running app needed
    # for that part) so there is something real to unload.
    def test_gated_side_unload_shows_the_diff_and_actually_reverts_it(self):
        main = self._minimal_spec()
        fragment = os.path.join(self.workdir, "extra-fragment.yaml")
        with open(fragment, "w") as f:
            f.write("playbook:\n- name: extra play\n  tasks: []\n")
        args = json.dumps({"fragment": fragment})
        sys.modules["litellm"] = fake_litellm(tool_name="side-unload", tool_arguments=args)
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp(files=[main])
            app.context.side_load(fragment)
            self.assertEqual(len(app.context.builds[0].spec["playbook"]), 2)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "drop the extra fragment"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                self.assertIn(fragment, _content(app.screen.query_one("#confirmfile")))
                diff = _content(app.screen.query_one("#confirmdiff"))
                self.assertIn("extra play", diff)
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle(app, pilot)
                self.assertEqual(len(app.context.builds[0].spec["playbook"]), 1)
        _run(scenario)

    # The model never saw its own change before this: 'side-load' ran,
    # then it had to spend a spec-dump/spec-query call just to find out
    # what loading the fragment actually did. The tool's own result now
    # carries the same diff the confirm dialog already showed.
    def test_side_load_tool_result_includes_the_merge_diff(self):
        main = self._minimal_spec()
        fragment = os.path.join(self.workdir, "extra-fragment.yaml")
        with open(fragment, "w") as f:
            f.write("playbook:\n- name: extra play\n  tasks: []\n")
        args = json.dumps({"fragment": fragment})
        sys.modules["litellm"] = fake_litellm(tool_name="side-load", tool_arguments=args)
        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp(files=[main])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "load the extra fragment"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle(app, pilot)
                tool_result = [m for m in app.ai_state.messages if m["role"] == "tool"][0]
                self.assertIn("side-loaded %s" % fragment, tool_result["content"])
                self.assertIn("extra play", tool_result["content"])
        _run(scenario)

    # A person side-loads a fragment, then asks the model for an
    # unrelated change: the tree's highlight only ever shows the last
    # one, the same one-shot behaviour /side-load's own highlight has --
    # worth pinning down since it's easy to assume both changes stay lit.
    def test_side_load_highlight_is_superseded_by_a_later_spec_update(self):
        main = self._minimal_spec()
        fragment = os.path.join(self.workdir, "extra-fragment.yaml")
        with open(fragment, "w") as f:
            f.write("playbook:\n- name: extra play\n  tasks: []\n")
        from seine.tui.spectree import SpecTree

        async def scenario():
            from seine.tui.ai import ConfirmAction
            app = self.SeineApp(files=[main])
            async with app.run_test() as pilot:
                # First turn: side-load the fragment.
                sys.modules["litellm"] = fake_litellm(
                    tool_name="side-load", tool_arguments=json.dumps({"fragment": fragment}))
                prompt = app.screen.query_one("#prompt")
                prompt.value = "load the extra fragment"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)

                tree = app.screen.query_one(SpecTree)
                after_side_load = {n.data for n in tree._changed}
                self.assertIn("extra play", after_side_load)

                # Second turn: an unrelated 'spec-update'.
                args = json.dumps({"path": main, "at": "$.playbook[0].tasks[0].apt.name",
                                   "value": "sudo", "mode": "append"})
                sys.modules["litellm"] = fake_litellm(tool_name="spec-update", tool_arguments=args)
                prompt = app.screen.query_one("#prompt")
                prompt.value = "add sudo"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(100):
                    if isinstance(app.screen, ConfirmAction):
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmAction)
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)

                tree = app.screen.query_one(SpecTree)
                after_update = {n.data for n in tree._changed}
                self.assertIn("sudo", after_update)
                # The fragment is still loaded (this isn't about it being
                # reverted) -- only its *highlight* is gone.
                self.assertNotIn("extra play", after_update)
                self.assertEqual(len(app.context.builds[0].spec["playbook"]), 2)
        _run(scenario)

# start-vendor/cancel-vendor themselves, run for real inside a running
# app -- call_from_thread needs one, which is why the ToolTable class
# above only covers the negative/preview paths (no active spec, refused
# up front). VendorCmd._run() is faked out the same way
# VendorScreenIntegration in tests/tui/tui.py does it, no podman/
# network involved -- the gated-approval UI flow itself (ConfirmAction,
# a preview shown before running) is generic and already proven
# elsewhere; what's under test here is start-vendor's own wiring,
# including the notify_ai hand-off StartBuildNotifiesTheAI below covers
# for a build.
class StartVendorTool(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.vendor import VendorCmd
            from seine.tui.app import SeineApp
            from seine.tui.vendor import VendorScreen
        self.VendorCmd = VendorCmd
        self.SeineApp = SeineApp
        self.VendorScreen = VendorScreen
        self.real_run = VendorCmd._run
        self.addCleanup(setattr, VendorCmd, "_run", self.real_run)
        from seine import tasks
        self.addCleanup(tasks._interrupted.clear)
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_LLM_MODEL"] = "openai/fake"
        self._real_litellm = sys.modules.get("litellm")
        self.addCleanup(self._restore_litellm)
        self.fragment = os.path.join(self.workdir, "vendor-only.yaml")
        with open(self.fragment, "w") as f:
            f.write(
                "distribution:\n"
                "    release: bookworm\n"
                "    architecture: amd64\n"
                "    uri: http://example.com/debian\n"
                "vendor:\n"
                "    - name: openssl\n")

    def _restore_litellm(self):
        if self._real_litellm is not None:
            sys.modules["litellm"] = self._real_litellm
        else:
            sys.modules.pop("litellm", None)

    # Called on a real background thread, not straight from the test's
    # own coroutine: _tool_start_vendor() -> start_vendor() crosses back
    # through call_from_thread the same way a real tool call always
    # does, from ai._run()'s own worker thread -- Textual refuses that
    # call from the app's own thread, which the test coroutine is.
    def _call_tool(self, name, arguments):
        import threading as th
        result = {}
        def call():
            from seine.tui import ai
            result["text"] = ai.TOOLS[name].run(self._app, arguments)
        th.Thread(target=call).start()
        return result

    # Event-gated, same as StartBuildNotifiesTheAI's own _fast_build()
    # below -- lets the test see "started, notify_ai set, still running"
    # before letting the fake run finish and the notify_ai hand-off
    # fire for real (a second, real 'ai._run()' turn, hence
    # fake_litellm(tool_name=None): a plain answer, no further tool call).
    def test_start_vendor_runs_for_real_and_notifies_the_ai(self):
        import threading as th
        release = th.Event()
        def gated_run(cmd_self, distro, entries, exclude, wanted, refresh,
                     archs=None, extra_archs=(), display=None):
            display.started("resolve:bookworm")
            release.wait(timeout=5)
            display.finished("resolve:bookworm", failed=False)
            display.say("vendored 1 source package(s) for bookworm")
        self.VendorCmd._run = gated_run
        sys.modules["litellm"] = fake_litellm()

        async def scenario():
            app = self.SeineApp(files=[self.fragment])
            self._app = app
            async with app.run_test() as pilot:
                result = self._call_tool("start-vendor", {})
                for _ in range(50):
                    if "text" in result:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn("vendor started", result.get("text", ""))
                self.assertIsInstance(app.screen, self.VendorScreen)
                self.assertTrue(app.vendor_state.notify_ai)
                self.assertTrue(app.vendor_state.running)

                release.set()
                for _ in range(100):
                    if not app.ai_state.busy and not app.vendor_state.notify_ai:
                        break
                    await asyncio.sleep(0.02)
                    await pilot.pause()
                self.assertTrue(app.vendor_state.done)
                self.assertFalse(app.vendor_state.error)
                # Cleared by App._vendor_finished() once the hand-off
                # actually fired -- the one-shot guarantee itself.
                self.assertFalse(app.vendor_state.notify_ai)
                notice = next((m for m in app.ai_state.messages
                              if m.get("role") == "user"
                              and "(seine)" in (m.get("content") or "")), None)
                self.assertIsNotNone(notice)
                self.assertIn("start-vendor", notice["content"])
                self.assertIn("finished successfully", notice["content"])
        _run(scenario)

    def test_cancel_vendor_stops_a_real_run(self):
        import threading as th
        release = th.Event()
        def slow_run(cmd_self, distro, entries, exclude, wanted, refresh,
                    archs=None, extra_archs=(), display=None):
            from seine import tasks as tasks_mod
            display.started("resolve:bookworm")
            release.wait(timeout=5)
            if tasks_mod._interrupted.is_set():
                raise tasks_mod.Interrupted(["resolve:bookworm"])
            display.finished("resolve:bookworm", failed=False)
        self.VendorCmd._run = slow_run

        from seine.tui import ai
        async def scenario():
            app = self.SeineApp(files=[self.fragment])
            self._app = app
            async with app.run_test() as pilot:
                self._call_tool("start-vendor", {})
                for _ in range(50):
                    if app.vendor_state.current is not None:
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(ai.TOOLS["cancel-vendor"].run(app, {}),
                                 "cancelling -- waiting for running steps to finish")
                release.set()
                for _ in range(50):
                    if not app.vendor_state.running:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(app.vendor_state.done)
                self.assertTrue(app.vendor_state.error)
        _run(scenario)

# Whole-build completion, only for a build 'start-build' itself started,
# triggers an unprompted follow-up turn (BuildState.notify_ai wired
# through App._build_finished()). A fast fake 'Image.build' stands in,
# same shape as BuildScreenIntegration in tests/tui/tui.py -- no podman needed.
class StartBuildNotifiesTheAI(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.image import Image
            from seine.tui.app import OverviewScreen, SeineApp
            from seine.tui.ai import ConfirmAction
            from seine.tui.build import BuildScreen
            from seine.tui.chat import ChatScreen
        self.Image = Image
        self.SeineApp = SeineApp
        self.ConfirmAction = ConfirmAction
        self.BuildScreen = BuildScreen
        self.ChatScreen = ChatScreen
        self.OverviewScreen = OverviewScreen
        self.real_build = Image.build
        self.addCleanup(setattr, Image, "build", self.real_build)
        from seine import tasks
        self.addCleanup(tasks._interrupted.clear)
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

    # Gated by an Event the test sets, not a sleep -- the build worker
    # and the AI turn run independently, and a fixed sleep raced the two
    # under load. Only the test knows when the first turn has settled.
    def _fast_build(self, failing, release):
        from seine import tasks
        def run(image, reporter=None):
            steps = tasks.ordered(image.tasks())
            for step in steps[:1]:
                reporter.started(step.name)
                release.wait(timeout=5)
                reporter.finished(step.name, failed=failing)
            if failing:
                raise tasks.Failed([(steps[0].name, RuntimeError("boom"))],
                                   [s.name for s in steps[1:]])
        return run

    async def _settle_to(self, pilot, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.02)
            await pilot.pause()

    # Distinctive, and never something the first ("build my image")
    # user message itself would say -- found this way rather than by
    # snapshotting a message count/index, since the build (a separate
    # worker) and the turn it triggers can race ahead of any single
    # "not busy" checkpoint taken right after approving.
    @staticmethod
    def _notice(app):
        return next((m for m in app.ai_state.messages
                    if m.get("role") == "user"
                    and "(seine)" in (m.get("content") or "")), None)

    def test_success_gets_an_unprompted_follow_up_naming_the_outcome(self):
        release = threading.Event()
        self.Image.build = self._fast_build(failing=False, release=release)
        sys.modules["litellm"] = fake_litellm(tool_name="start-build")

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build my image"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                self.assertIsInstance(app.screen, self.ConfirmAction)
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                # The original turn ("build started" -> final text)
                # settles on its own; only once it has is the build
                # (parked on the Event) let through to finish.
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                # start-build's own app.show("build") -- untouched since,
                # so a finished build is expected to switch back.
                self.assertIsInstance(app.screen, self.BuildScreen)
                release.set()
                await self._settle_to(pilot, lambda: self._notice(app) is not None)
                await self._settle_to(pilot, lambda: not app.ai_state.busy)

                self.assertTrue(app.build_state.done)
                self.assertFalse(app.build_state.error)
                self.assertFalse(app.build_state.notify_ai)
                injected = self._notice(app)
                self.assertIsNotNone(injected)
                self.assertIn("start-build", injected["content"])
                self.assertIn("finished successfully", injected["content"])
                # Nothing navigated away from the Build screen the AI
                # itself opened, so the notified turn switches back to
                # show it, rather than leave the answer for the person
                # to notice on their own.
                self.assertIsInstance(app.screen, self.ChatScreen)
        _run(scenario)

    # /build always sets 'sbom': True; start-build claims to match it
    # but didn't, leaving nothing for read/installed-packages to serve.
    def test_start_build_asks_for_an_sbom_same_as_slash_build(self):
        release = threading.Event()
        self.Image.build = self._fast_build(failing=False, release=release)
        sys.modules["litellm"] = fake_litellm(tool_name="start-build")

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build my image"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                self.assertTrue(app.context.builds[0].options["sbom"])
                release.set()
                await self._settle_to(pilot, lambda: app.build_state.done)
        _run(scenario)

    # 'image.options' is the same dictionary object 'build.options' is
    # (Image is constructed with it, never a copy -- seine/build.py's own
    # 'parse()') -- so a fake that reads 'image.tasks()' rather than
    # replacing it proves 'packages_only' actually reached the real
    # option Image.tasks() itself branches on, not just that the tool
    # accepted the argument.
    def test_packages_only_stops_the_build_after_the_packages_section(self):
        steps = []
        apps = []
        def run(image, reporter=None):
            steps.extend(t.name for t in image.tasks())
        self.Image.build = run
        sys.modules["litellm"] = fake_litellm(
            tool_name="start-build", tool_arguments='{"packages_only": true}')

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            apps.append(app)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build just the packages"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                await self._settle_to(pilot, lambda: len(steps) > 0)
        _run(scenario)

        self.assertIn("packages", steps)
        # None of what only 'own_tasks()' adds -- proof this stopped at
        # the shared step list rather than merely reordering the full one.
        self.assertNotIn("rootfs", steps)
        self.assertNotIn("image", steps)

        # The *displayed* step list has to agree -- 'packages_only' was
        # set on build.options only inside the worker thread's run(),
        # after state.reset() had already computed 'order' from the
        # full, unrestricted task list; a person (or 'build-status')
        # would have seen 'rootfs'/'tarball'/... stuck pending forever,
        # steps this build was never going to reach.
        order = apps[0].build_state.order
        self.assertIn("packages", order)
        self.assertNotIn("rootfs", order)
        self.assertNotIn("image", order)

    # Same shape as the 'packages_only' test above, but for 'target':
    # proves the argument reaches the real 'target' option Image.tasks()
    # branches on, and that the *displayed* step list (computed by
    # state.reset(), before the worker thread runs) narrows to it too.
    def test_target_narrows_to_the_named_task_and_its_needs(self):
        steps = []
        apps = []
        def run(image, reporter=None):
            steps.extend(t.name for t in image.tasks())
        self.Image.build = run
        sys.modules["litellm"] = fake_litellm(
            tool_name="start-build",
            tool_arguments='{"target": "deploy:busybox"}')

        async def scenario():
            app = self.SeineApp(files=[REBUILD_BUSYBOX])
            apps.append(app)
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build just busybox"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                await self._settle_to(pilot, lambda: len(steps) > 0)
        _run(scenario)

        self.assertIn("deploy:busybox", steps)
        self.assertNotIn("rootfs", steps)
        self.assertNotIn("image", steps)

        order = apps[0].build_state.order
        self.assertIn("deploy:busybox", order)
        self.assertNotIn("rootfs", order)
        self.assertNotIn("image", order)

    # An unknown target has to be reported back through the tool result,
    # not crash the worker or leave the app stuck -- ancestors() itself
    # only validates when tasks() calls it with a known 'target' name.
    def test_an_unknown_target_is_reported_not_a_crash(self):
        sys.modules["litellm"] = fake_litellm(
            tool_name="start-build",
            tool_arguments='{"target": "no-such-task"}')

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build just the frobnicator"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                await pilot.press("enter")  # 'Yes' is highlighted first
                await pilot.pause()
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                self.assertFalse(app.build_state.running)
        _run(scenario)

    # Manually navigating off the Build screen -- to Overview here,
    # anything not Build makes the same point -- before the build
    # finishes is a deliberate choice: the notified turn still runs
    # (the model still needs to know), but it must not force the
    # screen back, overriding where the person just chose to go.
    def test_navigating_away_before_it_finishes_is_left_alone(self):
        release = threading.Event()
        self.Image.build = self._fast_build(failing=False, release=release)
        sys.modules["litellm"] = fake_litellm(tool_name="start-build")

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build my image"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                self.assertIsInstance(app.screen, self.BuildScreen)

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/overview"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.OverviewScreen)

                release.set()
                await self._settle_to(pilot, lambda: self._notice(app) is not None)
                await self._settle_to(pilot, lambda: not app.ai_state.busy)

                self.assertIsNotNone(self._notice(app))
                self.assertIsInstance(app.screen, self.OverviewScreen)
        _run(scenario)

    def test_failure_gets_an_unprompted_follow_up_naming_the_outcome(self):
        release = threading.Event()
        self.Image.build = self._fast_build(failing=True, release=release)
        sys.modules["litellm"] = fake_litellm(tool_name="start-build")

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "build my image"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(
                    pilot, lambda: isinstance(app.screen, self.ConfirmAction))
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(pilot, lambda: not app.ai_state.busy)
                release.set()
                await self._settle_to(pilot, lambda: self._notice(app) is not None)
                await self._settle_to(pilot, lambda: not app.ai_state.busy)

                self.assertTrue(app.build_state.error)
                injected = self._notice(app)
                self.assertIsNotNone(injected)
                self.assertIn("failed", injected["content"])
        _run(scenario)

    # A build started from the '/build' command (not the AI chat's own
    # 'start-build' tool) must never trigger this -- nothing in that
    # conversation ever consented to an unprompted turn.
    def test_a_build_started_outside_the_ai_chat_is_not_notified(self):
        # No AI turn to race here -- nothing gates the build.
        already_released = threading.Event()
        already_released.set()
        self.Image.build = self._fast_build(failing=False, release=already_released)

        async def scenario():
            app = self.SeineApp(files=[NATIVE_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle_to(pilot, lambda: app.build_state.done)
                self.assertTrue(app.build_state.done)
                self.assertFalse(app.build_state.notify_ai)
                self.assertEqual(app.ai_state.messages, [])
        _run(scenario)

# One JSON file per conversation, under 'SEINE_CHAT_DIR' -- written
# purely for a person (or a later, separate run pairing it with
# 'seine/data/system_prompt.txt') to read back, kept local, never sent
# anywhere itself.
class ChatTranscriptsArePersisted(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_LLM_MODEL"] = "openai/fake"
        os.environ["SEINE_CHAT_DIR"] = os.path.join(self.workdir, "chats")
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
        self.fail("ai_state never settled")

    def _chat_files(self):
        chats = os.environ["SEINE_CHAT_DIR"]
        return sorted(os.listdir(chats)) if os.path.isdir(chats) else []

    def test_a_conversation_writes_one_file_under_seine_chat_dir(self):
        sys.modules["litellm"] = fake_litellm()
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "hi there"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                files = self._chat_files()
                self.assertEqual(len(files), 1)
                with open(os.path.join(os.environ["SEINE_CHAT_DIR"], files[0])) as f:
                    record = json.load(f)
                self.assertEqual(record["model"], "openai/fake")
                self.assertEqual(record["messages"], app.ai_state.messages)
                self.assertIsInstance(record["started"], float)
        _run(scenario)

    # The same file, not a new one, as a conversation goes on -- content
    # rewritten whole each time, matching what 'settings.save()' already
    # does for its own one file.
    def test_a_second_question_updates_the_same_file_not_a_new_one(self):
        sys.modules["litellm"] = fake_litellm()
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "first"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                first_files = self._chat_files()

                prompt = app.screen.query_one("#prompt")
                prompt.value = "second"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                self.assertEqual(self._chat_files(), first_files)
                with open(os.path.join(os.environ["SEINE_CHAT_DIR"], first_files[0])) as f:
                    record = json.load(f)
                self.assertEqual(len(record["messages"]), 4)  # 2 questions, 2 answers
        _run(scenario)

    # 'reset-conversation' clears 'chat_file' along with everything else
    # -- the next question starts a conversation of its own, not one
    # that keeps overwriting what a forgotten one already wrote.
    def test_reset_conversation_starts_a_new_file(self):
        sys.modules["litellm"] = fake_litellm()
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "first"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                first_files = self._chat_files()
                self.assertEqual(len(first_files), 1)

                app.ai_state.reset()
                self.assertEqual(self._chat_files(), first_files)  # reset alone writes nothing

                prompt = app.screen.query_one("#prompt")
                prompt.value = "second"
                await pilot.press("enter")
                await pilot.pause()
                await self._settle(app, pilot)
                second_files = self._chat_files()
                self.assertEqual(len(second_files), 2)
                self.assertNotEqual(second_files, first_files + first_files)
        _run(scenario)

class Routing(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        _clear_llm_env()

    # Not configured: unprefixed text is refused exactly as it always
    # was -- the AI chat being off changes nothing about a typo'd command.
    def test_unconfigured_bare_text_is_the_same_old_error(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "hello"
                await pilot.press("enter")
                await pilot.pause()
                status = app.screen.query_one("#status")
                self.assertIn("'/'", _content(status))
        _run(scenario)

    def test_configured_bare_text_goes_to_chat(self):
        sys.modules["litellm"] = fake_litellm()
        self.addCleanup(sys.modules.pop, "litellm", None)
        os.environ["SEINE_LLM_MODEL"] = "openai/fake"
        async def scenario():
            from seine.tui.chat import ChatScreen
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "hello"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ChatScreen)
        _run(scenario)

# Opt-in, real endpoint: skipped unless 'SEINE_LLM_MODEL'/
# 'SEINE_LLM_API_BASE' are both actually set, the same "cancel, don't
# fail, when the real thing isn't there" shape 'AnSBOMIsActuallyBuilt'
# (tests/security/sbom.py) already uses for podman. Nothing here is asserted
# on the model's own wording -- only that a real round trip, tool call
# included, actually completes.
class RealEndpoint(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.chat import ChatScreen
        if not (os.environ.get("SEINE_LLM_MODEL") and os.environ.get("SEINE_LLM_API_BASE")):
            self.cancel("SEINE_LLM_MODEL/SEINE_LLM_API_BASE not set -- "
                       "no real endpoint to test against")
        self.SeineApp = SeineApp
        self.ChatScreen = ChatScreen
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_a_real_question_gets_a_real_answer(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = ("Call the doctor tool, then say in one short "
                                "sentence whether podman is ok.")
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(300):
                    if not app.ai_state.busy:
                        break
                    await asyncio.sleep(0.2)
                    await pilot.pause()
                self.assertFalse(app.ai_state.busy, "the real endpoint never answered")
                self.assertIsInstance(app.screen, self.ChatScreen)
                # Not "the model called a tool" -- a live model's own
                # judgement call, and asserting on it would make this
                # test flaky through no fault of seine's own. What is
                # seine's to prove is that a real round trip (network,
                # auth, streaming, token accounting) actually completes;
                # 'TheLoop' above already proves the tool-dispatch
                # mechanics deterministically, with a fake model.
                roles = [m["role"] for m in app.ai_state.messages]
                self.assertEqual(roles[0], "user")
                self.assertIn("assistant", roles)
                self.assertGreater(app.ai_state.prompt_tokens, 0)
                self.assertGreater(app.ai_state.completion_tokens, 0)
        _run(scenario)

if __name__ == "__main__":
    avocado.main()
