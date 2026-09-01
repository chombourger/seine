#!/usr/bin/env python3

import avocado
import contextlib
import os
import sys
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-ai-tests-"))
os.chdir(tempfile.mkdtemp(prefix="seine-ai-tests-cwd-"))

from tests.native_image import native_image

NATIVE_IMAGE = native_image()

# The three env vars ('SEINE_LLM_MODEL' etc.) are process-global state,
# same as any other 'SEINE_*' override elsewhere in this suite -- popped
# in every 'setUp()' below, not just where a test sets its own, so a
# leftover from one test can never leak into the next one run in the
# same process.
LLM_ENV = ["SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"]

def _clear_llm_env():
    for name in LLM_ENV:
        os.environ.pop(name, None)

# Duplicated rather than imported from tests/tui/ai.py -- test files
# stay self-contained here, none of them import each other.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# One theme out of what was originally a single 'ToolTable' class in
# tests/tui/ai.py -- split apart (2026-09-01) once it had grown to
# cover every AI tool at once. This file: the workbench tools (gist,
# source, bash, side-load/side-unload).
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
                "  - name: install utilities\n"
                "    apt:\n"
                "      name:\n"
                "      - attr\n"
                "      - iputils-ping\n"
                "      state: present\n"
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

    # No 'files=[...]' -- gist tools don't need (or read) an active
    # spec, unlike spec-create/side-load; this is the point (a gist
    # lives outside any one project).
    def test_gist_list_with_nothing_yet(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["gist-list"].run(app, {})
        self.assertIn("no gists yet", text)

    def test_gist_create_then_list_then_show(self):
        app = self.SeineApp()
        args = {"name": "a-kernel", "description": "page-ref debugging",
                "content": "packages:\n- x\n"}
        preview = self.ai.TOOLS["gist-create"].preview(app, args)
        self.assertTrue(preview.ok)
        self.assertIn("+# page-ref debugging", preview.message)

        result = self.ai.TOOLS["gist-create"].run(app, args)
        self.assertIn("a-kernel.yaml", result)

        listing = self.ai.TOOLS["gist-list"].run(app, {})
        self.assertIn("a-kernel", listing)
        self.assertIn("page-ref debugging", listing)
        self.assertIn("a-kernel.yaml", listing)  # the absolute path

        shown = self.ai.TOOLS["gist-show"].run(app, {"name": "a-kernel"})
        self.assertEqual(shown, "# page-ref debugging\npackages:\n- x\n")

    def test_gist_create_refuses_an_existing_name(self):
        app = self.SeineApp()
        args = {"name": "dup", "description": "d", "content": "a: 1\n"}
        self.ai.TOOLS["gist-create"].run(app, args)
        preview = self.ai.TOOLS["gist-create"].preview(app, args)
        self.assertFalse(preview.ok)
        self.assertIn("already exists", preview.message)

    def test_gist_create_refuses_a_bad_name(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["gist-create"].preview(
            app, {"name": "Not Kebab Case", "description": "d", "content": "a: 1\n"})
        self.assertFalse(preview.ok)

    def test_gist_create_content_not_valid_yaml(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["gist-create"].preview(
            app, {"name": "bad-yaml", "description": "d", "content": "[unterminated"})
        self.assertFalse(preview.ok)
        self.assertIn("not valid YAML", preview.message)

    def test_gist_show_of_a_missing_gist(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["gist-show"].run(app, {"name": "nope"})
        self.assertIn("could not read", text)

    def test_gist_delete_preview_then_run(self):
        app = self.SeineApp()
        self.ai.TOOLS["gist-create"].run(
            app, {"name": "throwaway", "description": "d", "content": "a: 1\n"})
        preview = self.ai.TOOLS["gist-delete"].preview(app, {"name": "throwaway"})
        self.assertTrue(preview.ok)
        self.assertIn("-a: 1", preview.message)

        result = self.ai.TOOLS["gist-delete"].run(app, {"name": "throwaway"})
        self.assertIn("deleted throwaway", result)
        listing = self.ai.TOOLS["gist-list"].run(app, {})
        self.assertIn("no gists yet", listing)

    def test_gist_delete_of_a_missing_gist(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["gist-delete"].preview(app, {"name": "nope"})
        self.assertFalse(preview.ok)

    # Stands in for SourceBootstrap so source-pull is tested without a
    # container -- same swap-in-place idiom tests/bootstrap/sources.py's own
    # FakeSourceBootstrap uses, kept separate since test files here stay
    # self-contained.
    def _fake_source_pull(self, dirname="bash-5.14"):
        from seine import sources
        class Fake:
            def __init__(self, distro, options):
                pass
            def create(self):
                return self
            def exec(self, args, volumes=None, workdir=None):
                os.makedirs(os.path.join(workdir, dirname))
                return 0, ""
        saved = sources.SourceBootstrap
        sources.SourceBootstrap = Fake
        self.addCleanup(setattr, sources, "SourceBootstrap", saved)

    def test_source_list_with_nothing_yet(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["source-list"].run(app, {})
        self.assertIn("no sources pulled yet", text)

    def test_source_pull_needs_an_active_spec(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["source-pull"].preview(app, {"package": "bash"})
        self.assertFalse(preview.ok)
        self.assertIn("no single active specification", preview.message)

    def test_source_pull_then_list_then_rm(self):
        self._fake_source_pull()
        app = self.SeineApp(files=[NATIVE_IMAGE])

        preview = self.ai.TOOLS["source-pull"].preview(app, {"package": "bash"})
        self.assertTrue(preview.ok)
        self.assertIn("bash", preview.message)

        result = self.ai.TOOLS["source-pull"].run(app, {"package": "bash"})
        self.assertIn("bash-5.14", result)

        listing = self.ai.TOOLS["source-list"].run(app, {})
        self.assertIn("bash", listing)
        self.assertIn("bash-5.14", listing)

        rm_preview = self.ai.TOOLS["source-rm"].preview(app, {"name": "bash"})
        self.assertTrue(rm_preview.ok)
        self.assertIn("bash-5.14", rm_preview.message)

        rm_result = self.ai.TOOLS["source-rm"].run(app, {"name": "bash"})
        self.assertIn("removed bash", rm_result)
        self.assertIn("no sources pulled yet",
                      self.ai.TOOLS["source-list"].run(app, {}))

    def test_source_pull_refuses_an_already_pulled_name(self):
        self._fake_source_pull()
        app = self.SeineApp(files=[NATIVE_IMAGE])
        self.ai.TOOLS["source-pull"].run(app, {"package": "bash"})
        preview = self.ai.TOOLS["source-pull"].preview(app, {"package": "bash"})
        self.assertFalse(preview.ok)
        self.assertIn("already pulled", preview.message)

    def test_source_rm_of_a_missing_name(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["source-rm"].preview(app, {"name": "nope"})
        self.assertFalse(preview.ok)

    # Patches the classes directly, same reasoning tests/bootstrap/sources.py's
    # own Bash class gives -- both are reached through the class, never
    # an instance.
    def _fake_bash(self, output="", returncode=0):
        from seine import sources
        from seine.utils import ContainerEngine
        saved_create = sources.HostBootstrap.create
        saved_run = ContainerEngine.run_captured
        sources.HostBootstrap.create = lambda self: self
        ContainerEngine.run_captured = staticmethod(lambda cmd: (returncode, output))
        def restore():
            sources.HostBootstrap.create = saved_create
            ContainerEngine.run_captured = saved_run
        self.addCleanup(restore)

    def test_bash_needs_an_active_spec(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["bash"].run(app, {"command": "true"})
        self.assertIn("no single active specification", text)

    def test_bash_runs_and_returns_the_output(self):
        self._fake_bash("hello\n")
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["bash"].run(app, {"command": "echo hello"})
        self.assertEqual(text, "hello")

    def test_bash_reports_a_bad_cwd_rather_than_crash(self):
        self._fake_bash()
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["bash"].run(
            app, {"command": "true", "cwd": "does-not-exist"})
        self.assertIn("could not run", text)

    def _side_load_fragment(self, content="playbook:\n- name: extra play\n  tasks: []\n",
                         name="extra-fragment"):
        path = os.path.join(self.workdir, "%s.yaml" % name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_side_load_needs_fragment(self):
        app = self.SeineApp(files=[self._minimal_spec()])
        text = self.ai.TOOLS["side-load"].run(app, {})
        self.assertIn("needs 'fragment'", text)

    def test_side_load_preview_needs_an_active_spec(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["side-load"].preview(app, {"fragment": "whatever.yaml"})
        self.assertFalse(preview.ok)
        self.assertIn("no active specification", preview.message)

    def test_side_load_preview_refuses_a_fragment_that_fails_to_load(self):
        app = self.SeineApp(files=[self._minimal_spec()])
        preview = self.ai.TOOLS["side-load"].preview(
            app, {"fragment": "/does/not/exist.yaml"})
        self.assertFalse(preview.ok)

    # 'diff()' (seine/build.py) always hands back the whole spec, unmarked
    # context lines included -- never blank even with nothing to say -- so
    # "would this change anything" has to look for an actual '+'/'-' mark.
    def test_side_load_preview_says_no_op_when_nothing_would_change(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        noop = self._side_load_fragment("distribution:\n  architecture: amd64\n")
        preview = self.ai.TOOLS["side-load"].preview(app, {"fragment": noop})
        self.assertFalse(preview.ok)
        self.assertIn("change nothing", preview.message)

    # A dry run -- computed against a scratch 'BuildCmd', never touching
    # 'app.context' itself, so a person can review several candidate
    # fragments without any of them actually taking effect until 'run'.
    def test_side_load_preview_shows_what_merging_would_change(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        fragment = self._side_load_fragment()
        preview = self.ai.TOOLS["side-load"].preview(app, {"fragment": fragment})
        self.assertTrue(preview.ok)
        self.assertIn("extra play", preview.message)
        self.assertEqual(len(app.context.builds[0].spec["playbook"]), 1)

    def test_side_unload_needs_fragment(self):
        app = self.SeineApp(files=[self._minimal_spec()])
        text = self.ai.TOOLS["side-unload"].run(app, {})
        self.assertIn("needs 'fragment'", text)

    def test_side_unload_preview_refuses_a_fragment_that_isnt_loaded(self):
        app = self.SeineApp(files=[self._minimal_spec()])
        preview = self.ai.TOOLS["side-unload"].preview(
            app, {"fragment": "/does/not/exist.yaml"})
        self.assertFalse(preview.ok)
        self.assertIn("isn't currently loaded", preview.message)

    # Side-loads a fragment for real (via Context directly -- see the
    # threading note above), then previews unloading it through the AI
    # tool -- proves the preview reflects the *current* group list (with
    # the fragment in it), not the one side-loading started from.
    def test_side_unload_preview_shows_what_reverting_would_change(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        fragment = self._side_load_fragment()
        app.context.side_load(fragment)
        self.assertEqual(len(app.context.builds[0].spec["playbook"]), 2)
        preview = self.ai.TOOLS["side-unload"].preview(app, {"fragment": fragment})
        self.assertTrue(preview.ok)
        self.assertIn("extra play", preview.message)

    # 'run()' on 'side-load'/'side-unload' crosses back to the UI thread
    # via 'call_from_thread', which needs a real running App -- these two
    # exercise the underlying 'Context' methods directly instead (the
    # AI-tool wrapper's own threading and diff-generation are covered by
    # 'TheLoop's async gated-tool tests below).
    def test_side_unload_drops_the_fragment_back_out(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        fragment = self._side_load_fragment()
        app.context.side_load(fragment)
        app.context.side_unload(fragment)
        self.assertEqual(len(app.context.builds[0].spec["playbook"]), 1)

    # No LIFO assumption: three fragments side-loaded in order, unloaded
    # out of order (last, then first, leaving the middle one) -- the
    # group's file list is what side_unload() actually edits, not a
    # stack, so unloading works on whichever name is asked for.
    def test_side_unload_order_is_not_assumed_to_be_lifo(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        s1 = self._side_load_fragment("playbook:\n- name: play one\n  tasks: []\n", name="s1")
        s2 = self._side_load_fragment("playbook:\n- name: play two\n  tasks: []\n", name="s2")
        s3 = self._side_load_fragment("playbook:\n- name: play three\n  tasks: []\n", name="s3")
        for fragment in (s1, s2, s3):
            app.context.side_load(fragment)
        self.assertEqual(app.context.groups[0][-3:], [s1, s2, s3])

        app.context.side_unload(s3)
        self.assertEqual(app.context.groups[0][-2:], [s1, s2])

        app.context.side_unload(s1)
        self.assertEqual(app.context.groups[0][-1:], [s2])
        plays = {p["name"] for p in app.context.builds[0].spec["playbook"]}
        self.assertIn("play two", plays)
        self.assertNotIn("play one", plays)
        self.assertNotIn("play three", plays)

