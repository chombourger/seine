#!/usr/bin/env python3

import avocado
import contextlib
import os
import sys
import tempfile
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-ai-audit-tests-"))
os.chdir(tempfile.mkdtemp(prefix="seine-ai-audit-tests-cwd-"))

@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# _dispatch() is exercised directly here, not through a real ConfirmAction
# modal (see tests/tui/ai.py for that end-to-end path) -- 'ai.confirm' is
# monkeypatched instead, since audit()'s own hook lives in _dispatch(),
# after confirm() has already answered, not inside the modal itself.
class AuditTrail(avocado.Test):
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
        os.environ["SEINE_AUDIT_DIR"] = os.path.join(self.workdir, "audit")
        for name in ["SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"]:
            os.environ.pop(name, None)
        self._orig_confirm = ai.confirm

    def tearDown(self):
        self.ai.confirm = self._orig_confirm
        self.ai.TOOLS.pop("fake-tool", None)

    def _fake_call(self, arguments="{}"):
        return types.SimpleNamespace(
            function=types.SimpleNamespace(name="fake-tool", arguments=arguments))

    def _register_fake_tool(self, result="did it"):
        tool = self.ai.Tool("fake-tool", "a test-only gated tool",
                            self.ai._no_args(), True, lambda app, args: result)
        self.ai.TOOLS["fake-tool"] = tool
        return tool

    def test_audit_log_with_nothing_recorded_yet(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["audit-log"].run(app, {})
        self.assertIn("no gated tool calls recorded", text)

    def test_approved_gated_call_is_audited(self):
        self._register_fake_tool(result="did it")
        self.ai.confirm = lambda app, tool, arguments, preview: True
        app = self.SeineApp()
        result = self.ai._dispatch(app, self._fake_call())
        self.assertEqual(result, "did it")
        text = self.ai.TOOLS["audit-log"].run(app, {})
        self.assertIn("fake-tool", text)
        self.assertIn("approved", text)
        self.assertIn("did it", text)

    def test_denied_gated_call_is_audited_too(self):
        self._register_fake_tool()
        self.ai.confirm = lambda app, tool, arguments, preview: False
        app = self.SeineApp()
        result = self.ai._dispatch(app, self._fake_call())
        self.assertEqual(result, "denied by user")
        text = self.ai.TOOLS["audit-log"].run(app, {})
        self.assertIn("fake-tool", text)
        self.assertIn("denied", text)

    # Ungated calls never go near confirm()/audit() at all -- only a
    # real action (approved or refused) is worth a trail entry.
    def test_ungated_calls_are_not_audited(self):
        app = self.SeineApp()
        self.ai._dispatch(app, types.SimpleNamespace(
            function=types.SimpleNamespace(name="overview", arguments="{}")))
        text = self.ai.TOOLS["audit-log"].run(app, {})
        self.assertIn("no gated tool calls recorded", text)

    def test_audit_log_caps_at_the_newest_entries(self):
        self._register_fake_tool()
        self.ai.confirm = lambda app, tool, arguments, preview: True
        app = self.SeineApp()
        total = self.ai.AUDIT_LOG_MAX_ROWS + 5
        for i in range(total):
            self.ai._dispatch(app, self._fake_call(arguments='{"n": %d}' % i))
        text = self.ai.TOOLS["audit-log"].run(app, {})
        lines = text.splitlines()
        self.assertEqual(len(lines), self.ai.AUDIT_LOG_MAX_ROWS)
        # The oldest 5 were dropped, the newest kept, oldest-first among those.
        self.assertIn("n=%d" % (total - 1), lines[-1])
        self.assertNotIn("n=0 ", text)

    def test_audit_file_lands_under_the_configured_audit_dir(self):
        from seine.utils import ContainerEngine
        self._register_fake_tool()
        self.ai.confirm = lambda app, tool, arguments, preview: True
        app = self.SeineApp()
        self.ai._dispatch(app, self._fake_call())
        path = ContainerEngine.audit()
        self.assertTrue(path.startswith(os.environ["SEINE_AUDIT_DIR"]))
        self.assertTrue(os.path.isfile(path))
