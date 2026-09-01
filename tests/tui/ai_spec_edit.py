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
PC_IMAGE = os.path.join(path_to_sources, "examples", "pc-image", "main.yaml")

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
# cover every AI tool at once. This file: the tools that query or edit
# a spec (spec-query, spec-update, spec-create) plus the indent-detection
# helper spec-update relies on.
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

    # spec-query, given an explicit 'path', shares read's wider
    # boundary -- the same unloaded sibling is queryable directly.
    def test_spec_query_works_on_an_unloaded_sibling_given_an_explicit_path(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        kernel = os.path.realpath(os.path.join(
            os.path.dirname(NATIVE_IMAGE), "..", "linux-6.18", "kernel.yml"))
        text = self.ai.TOOLS["spec-query"].run(
            app, {"path": kernel, "expression": "$..source"})
        self.assertIn("apt://linux", text)

    # But a blanket search ('path' omitted) still only walks
    # build.loaded_files -- it must not quietly widen to include
    # files nothing 'requires:', just because they happen to sit next
    # to a loaded one. A sibling with a distinctive key ('upstream:',
    # only linux-6.18/kernel.yml has it in these fixtures) must not
    # surface in a search that never named it.
    def test_spec_query_without_a_path_does_not_search_unloaded_siblings(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["spec-query"].run(
            app, {"expression": "$..upstream"})
        self.assertNotIn("cdn.kernel.org", text)

    # '$..name' -- every 'name:' key at any depth, the "where does X
    # appear" shape this tool exists for -- against the same file
    # 'test_read_returns_a_loaded_files_own_text' reads directly,
    # so the two tests agree on what that file actually contains.
    def test_spec_query_finds_matches_with_jsonpath(self):
        app = self.SeineApp(files=[PC_IMAGE])
        text = self.ai.TOOLS["spec-query"].run(
            app, {"path": PC_IMAGE, "expression": "$..name"})
        self.assertIn("install utilities", text)

    def test_spec_query_with_no_matches_says_so_not_nothing(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["spec-query"].run(
            app, {"path": NATIVE_IMAGE, "expression": "$.nothing_named_this"})
        self.assertTrue(text.startswith("no matches"))
        self.assertIn("$..apt", text)

    def test_spec_query_reports_a_bad_expression_not_a_crash(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["spec-query"].run(
            app, {"path": NATIVE_IMAGE, "expression": "not a jsonpath expression((("})
        self.assertIn("not a usable JSONPath expression", text)

    # 'path' left out -- searches every file 'spec-files' would list,
    # each match prefixed with which one, for "where is this" asked
    # before any one file is known to look in.
    def test_spec_query_with_no_path_searches_every_loaded_file(self):
        app = self.SeineApp(files=[PC_IMAGE])
        build = app.context.builds[0]
        text = self.ai.TOOLS["spec-query"].run(app, {"expression": "$..name"})
        self.assertIn("install utilities", text)
        self.assertIn("%s: " % os.path.realpath(PC_IMAGE), text)
        self.assertGreater(len(build.loaded_files), 1)  # 'requires' really did pull in more

    # A file that fails to parse on its own is skipped, not left to
    # abort the whole search -- only a named 'path' that isn't actually
    # loaded is worth stopping for. A copy of 'PC_IMAGE' 's own tree
    # (its 'requires' fragments included) under 'self.workdir', never
    # the real 'examples/' -- the file corrupted on purpose here must
    # land on the copy, not the source.
    def test_spec_query_skips_a_file_that_fails_to_parse_during_a_global_search(self):
        import shutil
        examples = os.path.join(path_to_sources, "examples")
        shutil.copytree(os.path.join(examples, "pc-image"),
                        os.path.join(self.workdir, "pc-image"))
        shutil.copytree(os.path.join(examples, "common"),
                        os.path.join(self.workdir, "common"))
        main = os.path.join(self.workdir, "pc-image", "main.yaml")
        app = self.SeineApp(files=[main])
        with open(main, "a") as f:
            f.write("  - not valid next to a mapping\n")
        text = self.ai.TOOLS["spec-query"].run(app, {"expression": "$..name"})
        self.assertNotIn("could not", text.lower())
        self.assertNotEqual(text, "no matches")

    # '_detect_indent()' on its own -- pinned directly, since it is a
    # text heuristic (not something 'ruamel.yaml' derives for us) and
    # easy to silently regress without a test that names exact numbers.
    def test_detect_indent_finds_nothing_in_a_flat_file(self):
        self.assertEqual(self.ai._detect_indent("a: 1\nb: 2\n"), {})

    def test_detect_indent_on_the_real_examples_style(self):
        text = ("requires:\n    - x\n"
               "playbook:\n    - name: y\n      tasks:\n           - a\n")
        indent = self.ai._detect_indent(text)
        self.assertEqual(indent["sequence"], 6)
        self.assertEqual(indent["offset"], 4)

    def test_detect_indent_finds_the_mapping_increment_too(self):
        text = "a:\n    b: 1\n    c: 2\n"
        self.assertEqual(self.ai._detect_indent(text)["mapping"], 4)

    # A file already at 'ruamel.yaml' 's own default (0-indent dash,
    # 2-space content) detects that same shape back -- applying it is a
    # no-op, nothing about the common case changes because of this.
    def test_detect_indent_on_already_default_style(self):
        text = "playbook:\n- name: y\n  tasks: []\n"
        indent = self.ai._detect_indent(text)
        self.assertEqual(indent.get("offset", 0), 0)
        self.assertEqual(indent.get("sequence", 2), 2)

    # A writable copy of the real example tree, same shape as
    # 'test_spec_query_skips_a_file_that_fails_to_parse_during_a_global_search'
    # above -- 'spec-update'/'spec-create' actually write, so every test
    # below has to run against a throwaway copy, never the real
    # 'examples/' this repo ships.
    def _copied_pc_image(self):
        import shutil
        examples = os.path.join(path_to_sources, "examples")
        shutil.copytree(os.path.join(examples, "pc-image"),
                        os.path.join(self.workdir, "pc-image"))
        shutil.copytree(os.path.join(examples, "common"),
                        os.path.join(self.workdir, "common"))
        return os.path.join(self.workdir, "pc-image", "main.yaml")

    # A small, cleanly-indented spec for the tests below that check a
    # diff is minimal -- examples/pc-image/main.yaml's own deeper,
    # inconsistent indent style is a separate case, its own test below.
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

    def test_spec_update_needs_path_at_value(self):
        app = self.SeineApp(files=[self._copied_pc_image()])
        text = self.ai.TOOLS["spec-update"].run(app, {})
        self.assertIn("needs 'path'", text)

    def test_spec_update_refuses_a_path_not_loaded(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": "/etc/shadow", "at": "$.playbook", "value": "[]"})
        self.assertIn("not one of this build's own loaded files", text)

    def test_spec_update_at_matching_zero_nodes(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": main, "at": "$.nothing_named_this", "value": "1"})
        self.assertIn("no node matches", text)
        self.assertIn("spec-query", text)

    def test_spec_update_at_matching_many_nodes(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": main, "at": "$..name", "value": "x"})
        self.assertIn("narrow it", text)

    def test_spec_update_value_not_valid_yaml(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": main, "at": "$.playbook[0].tasks[1].apt.name",
                 "value": "[unterminated"})
        self.assertIn("not valid YAML", text)

    def test_spec_update_append_needs_a_list(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": main, "at": "$.playbook[0].tasks[1].apt",
                 "value": "x", "mode": "append"})
        self.assertIn("does not address a list", text)

    def test_spec_update_bad_mode(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": main, "at": "$.playbook[0].tasks[1].apt.name",
                 "value": "[vim]", "mode": "sideways"})
        self.assertIn("'mode' must be", text)

    # 'value' has to match the target's own YAML *style* (block, here),
    # not just its semantic value -- 'ruamel.yaml' round-trips whichever
    # style 'value' itself was written in, so a flow-style '[vim]' would
    # genuinely change the file's rendering even though the list is the
    # same one item; that is a real edit, not a false "no effect".
    def test_spec_update_no_op_is_refused(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-update"].run(
            app, {"path": main, "at": "$.playbook[0].tasks[1].apt.name",
                 "value": "- vim\n"})
        self.assertIn("no effect", text)

    # 'preview' and 'run' agree on the same plan -- the diff a person
    # would review names the file and shows the real change, and 'run'
    # actually makes it (checked by re-reading the file, not by trusting
    # 'run' 's own return text). Cleanly-indented fixture -- the "only
    # the touched node changes" promise this whole design rests on only
    # holds when 'ruamel.yaml' 's own default indent style already
    # matches the file's (see the reflow test below for when it doesn't).
    def test_spec_update_preview_then_run_appends_one_item(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        args = {"path": main, "at": "$.playbook[0].tasks[1].apt.name",
               "value": "sudo", "mode": "append"}
        preview = self.ai.TOOLS["spec-update"].preview(app, args)
        self.assertTrue(preview.ok)
        self.assertIn(main, preview.message)
        self.assertIn("+      - sudo", preview.message)
        self.assertNotIn("-      - vim", preview.message)  # untouched, not removed+readded

        result = self.ai.TOOLS["spec-update"].run(app, args)
        self.assertEqual(result, "updated %s" % main)
        with open(main) as f:
            written = f.read()
        self.assertIn("- vim", written)
        self.assertIn("- sudo", written)
        # Untouched elsewhere -- the unrelated 'install utilities' task
        # survives verbatim, the whole point of 'ruamel.yaml' 's round
        # trip over a plain PyYAML one.
        self.assertIn("iputils-ping", written)

    def test_spec_update_set_mode_replaces_the_whole_node(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        args = {"path": main, "at": "$.playbook[0].tasks[1].apt.name",
               "value": "[vim, sudo]"}
        self.ai.TOOLS["spec-update"].run(app, args)
        with open(main) as f:
            written = f.read()
        self.assertIn("sudo", written)

    # A secret in the *old* line of the diff must never reach the model
    # or the confirm dialog -- same 'redact:' patterns 'dump_file()'
    # already applies to a read, applied here to the diff instead.
    def test_spec_update_diff_is_redacted(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        build = app.context.builds[0]
        build.spec.setdefault("redact", []).append("iputils-ping")
        args = {"path": main, "at": "$.playbook[0].tasks[0].apt.name",
               "value": "[attr, iputils-ping, extra]"}
        preview = self.ai.TOOLS["spec-update"].preview(app, args)
        self.assertTrue(preview.ok)
        self.assertNotIn("iputils-ping", preview.message)
        self.assertIn("<redacted:", preview.message)

    # A known caveat: a file indented differently from ruamel.yaml's own
    # default (examples/pc-image/main.yaml included) gets every sequence
    # line reflowed on the first edit, not just the touched node --
    # wider than the minimal diff the tool otherwise gives, but the edit
    # itself is still correct (checked below).
    #
    # _detect_indent() picks up main.yaml's 4-space list/mapping style
    # from its first occurrence, so requires:/playbook: survive verbatim.
    # 'tasks:', nested one level deeper, happens to use a different
    # increment (5 spaces, not 4) that YAML().indent() can't express per
    # nesting level, so that one block still reflows by a single space.
    def test_spec_update_only_reflows_the_locally_inconsistent_part(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        args = {"path": main, "at": "$.playbook[0].tasks[1].apt.name",
               "value": "sudo", "mode": "append"}
        preview = self.ai.TOOLS["spec-update"].preview(app, args)
        self.assertTrue(preview.ok)
        # 'requires:' -- a sibling section, several lines away from the
        # edit -- is untouched: no '-'/'+' mark on any of its lines.
        self.assertNotIn("-    - ../common/amd64", preview.message)
        self.assertNotIn("+- ../common/amd64", preview.message)
        self.assertIn("+                    - sudo", preview.message)

        self.ai.TOOLS["spec-update"].run(app, args)
        with open(main) as f:
            written = f.read()
        self.assertIn("- sudo", written)
        self.assertIn("    - ../common/amd64", written)  # 'requires:' untouched on disk too

    def test_spec_create_needs_path_and_content(self):
        app = self.SeineApp(files=[self._copied_pc_image()])
        text = self.ai.TOOLS["spec-create"].run(app, {})
        self.assertIn("needs 'path'", text)

    def test_spec_create_refuses_an_existing_path(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-create"].run(
            app, {"path": main, "content": "distribution: {}\n"})
        self.assertIn("already exists", text)

    def test_spec_create_refuses_an_unrelated_directory(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        elsewhere = os.path.join(self.workdir, "new.yaml")
        text = self.ai.TOOLS["spec-create"].run(
            app, {"path": elsewhere, "content": "distribution: {}\n"})
        self.assertIn("not next to any", text)

    def test_spec_create_content_not_valid_yaml(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        new_path = os.path.join(os.path.dirname(main), "extra.yaml")
        text = self.ai.TOOLS["spec-create"].run(
            app, {"path": new_path, "content": "[unterminated"})
        self.assertIn("not valid YAML", text)

    def test_spec_create_preview_then_run_writes_a_new_file(self):
        main = self._copied_pc_image()
        app = self.SeineApp(files=[main])
        new_path = os.path.join(os.path.dirname(main), "extra.yaml")
        content = "playbook:\n  - name: extra\n    tasks: []\n"
        args = {"path": new_path, "content": content}
        preview = self.ai.TOOLS["spec-create"].preview(app, args)
        self.assertTrue(preview.ok)
        self.assertIn(new_path, preview.message)
        self.assertIn("+playbook:", preview.message)

        result = self.ai.TOOLS["spec-create"].run(app, args)
        self.assertEqual(result, "wrote %s" % new_path)
        with open(new_path) as f:
            self.assertEqual(f.read(), content)

