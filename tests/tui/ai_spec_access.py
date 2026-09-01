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
REBUILD_BUSYBOX = os.path.join(path_to_sources, "examples", "rebuild-busybox", "main.yaml")

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
# cover every AI tool at once. This file: the read-only tools that let
# the AI look at a spec, an unloaded sibling, or the docs.
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

    def test_spec_files_lists_what_was_loaded(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        text = self.ai.TOOLS["spec-files"].run(app, {})
        for loaded in build.loaded_files:
            self.assertIn(loaded, text)
        self.assertIn(os.path.realpath(NATIVE_IMAGE), text)

    # 'examples/common/' has more '*.yaml' than 'pc-image' actually
    # 'requires:' (arm64.yaml, rpi4-image.yaml, ... -- other boards'
    # own fragments) -- real, not a synthetic fixture, proof this finds
    # something genuinely useful rather than only passing on a toy case.
    def test_spec_files_lists_unloaded_siblings_separately(self):
        app = self.SeineApp(files=[PC_IMAGE])
        build = app.context.builds[0]
        text = self.ai.TOOLS["spec-files"].run(app, {})
        self.assertIn("not loaded", text)
        arm64 = os.path.realpath(os.path.join(
            os.path.dirname(PC_IMAGE), "..", "common", "arm64.yaml"))
        self.assertNotIn(arm64, build.loaded_files)
        self.assertIn(arm64, text)
        # A loaded file is never also listed as a sibling -- the two
        # sections are a partition, not an overlapping pair of views.
        loaded_section, _, sibling_section = text.partition("not loaded")
        for loaded in build.loaded_files:
            self.assertNotIn(loaded, sibling_section)

    # linux-6.18/ isn't a directory any loaded file lives in -- it's a
    # sibling of pc-image/ and common/ under examples/. The AI missed
    # this fragment for exactly that reason before this fix.
    def test_spec_files_lists_yaml_from_cousin_directories_too(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        text = self.ai.TOOLS["spec-files"].run(app, {})
        kernel = os.path.realpath(os.path.join(
            os.path.dirname(NATIVE_IMAGE), "..", "linux-6.18", "kernel.yml"))
        self.assertNotIn(kernel, build.loaded_files)
        self.assertIn(kernel, text)

    # A single loaded directory must not climb to its parent -- only a
    # fork across 2+ loaded directories triggers the wider search, so
    # an unrelated directory (home dir, test tempdir) isn't swept.
    def test_spec_files_does_not_climb_above_a_single_loaded_directory(self):
        main = self._minimal_spec()
        stray = os.path.join(self.workdir, "..", "stray.yaml")
        with open(stray, "w") as f:
            f.write("distribution:\n  release: bookworm\n")
        try:
            app = self.SeineApp(files=[main])
            text = self.ai.TOOLS["spec-files"].run(app, {})
            self.assertNotIn("not loaded", text)
        finally:
            os.remove(stray)

    def test_spec_files_omits_the_sibling_section_when_theres_nothing_to_show(self):
        main = self._minimal_spec()
        app = self.SeineApp(files=[main])
        text = self.ai.TOOLS["spec-files"].run(app, {})
        self.assertNotIn("not loaded", text)

    def test_read_returns_a_loaded_files_own_text(self):
        app = self.SeineApp(files=[PC_IMAGE])
        text = self.ai.TOOLS["read"].run(app, {"path": PC_IMAGE})
        self.assertIn("install utilities", text)

    def test_read_refuses_a_path_that_was_not_loaded(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["read"].run(app, {"path": "/etc/shadow"})
        self.assertIn("not one of this build's own loaded files", text)

    # read's boundary is wider than spec-update's: a file spec-files
    # lists as an unloaded sibling -- never part of this build -- is
    # still readable, so the AI can inspect it before suggesting
    # 'requires:' rather than guessing at its contents.
    def test_read_works_on_an_unloaded_sibling_spec_files_listed(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        kernel = os.path.realpath(os.path.join(
            os.path.dirname(NATIVE_IMAGE), "..", "linux-6.18", "kernel.yml"))
        text = self.ai.TOOLS["read"].run(app, {"path": kernel})
        self.assertIn("cdn.kernel.org", text)

    # read's boundary reaches one hop further than a spec file's own
    # text: a local file this build's 'packages:' section itself names
    # -- here, rebuild-busybox's own patch -- the same file a real
    # build already reads to compile it. Real fixture, not synthetic,
    # same reasoning the sibling-file tests already use.
    def test_read_opens_a_patch_a_loaded_package_references(self):
        app = self.SeineApp(files=[REBUILD_BUSYBOX])
        patch = os.path.join(os.path.dirname(REBUILD_BUSYBOX),
                             "patches", "0001-mark-the-banner-as-rebuilt.patch")
        text = self.ai.TOOLS["read"].run(app, {"path": patch})
        self.assertIn("mark the banner as rebuilt", text)

    # A real patch file, just not one *this* build's 'packages:' names
    # -- bcachefs's own, from a wholly different example. Being a real,
    # readable file on disk is not enough; it has to actually be
    # referenced by a package this build is asking for.
    def test_read_refuses_a_local_file_no_loaded_package_references(self):
        app = self.SeineApp(files=[REBUILD_BUSYBOX])
        other = os.path.join(path_to_sources, "examples", "bcachefs",
                             "patches", "0001-build-the-module-from-a-checkout.patch")
        text = self.ai.TOOLS["read"].run(app, {"path": other})
        self.assertIn("is not one of this build's own loaded files", text)

    # No 'files=[...]' -- reading a pulled source needs no active
    # specification, same as source-list.
    def test_read_reaches_a_file_under_the_workbench_with_no_active_spec(self):
        from seine.utils import ContainerEngine
        path = os.path.join(ContainerEngine.workbench(), "bash-5.14", "debian", "control")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("Source: bash\n")
        app = self.SeineApp()
        text = self.ai.TOOLS["read"].run(app, {"path": path})
        self.assertEqual(text, "Source: bash\n")

    def test_read_of_a_missing_workbench_file_is_an_error_not_a_crash(self):
        from seine.utils import ContainerEngine
        path = os.path.join(ContainerEngine.workbench(), "nope", "control")
        app = self.SeineApp()
        text = self.ai.TOOLS["read"].run(app, {"path": path})
        self.assertIn("could not read", text)

    # The active build's own SBOM -- not any SBOM (sbom-diff takes an
    # explicit path for that) -- so a version looked up here is
    # guaranteed to be this build's own, not an unrelated file that
    # happens to sit on disk.
    def test_read_reaches_the_active_builds_own_sbom(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        build.options["sbom"] = True
        from seine.sbom import SBOM
        sbom_path = SBOM(build.spec["distribution"], build.options)._output_file(
            build.image._output) + ".spdx.json"
        os.makedirs(os.path.dirname(sbom_path), exist_ok=True)
        with open(sbom_path, "w") as f:
            f.write('{"packages": []}')
        text = self.ai.TOOLS["read"].run(app, {"path": sbom_path})
        self.assertEqual(text, '{"packages": []}')

    def test_read_does_not_reach_an_sbom_when_none_would_be_produced(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        from seine.sbom import SBOM
        sbom_path = SBOM(build.spec["distribution"], dict(build.options, sbom=True))\
            ._output_file(build.image._output) + ".spdx.json"
        os.makedirs(os.path.dirname(sbom_path), exist_ok=True)
        with open(sbom_path, "w") as f:
            f.write('{"packages": []}')
        # build.options["sbom"] is still False here -- the file exists
        # on disk (maybe from an earlier build) but this build would not
        # produce it, so it is not reachable through this build's read.
        text = self.ai.TOOLS["read"].run(app, {"path": sbom_path})
        self.assertIn("is not one of this build's own loaded files", text)

    # 'defaults.packages:' entries are descriptions, not requests
    # ([DEFAULTS-VS-PACKAGES]) -- a patch named only there must not
    # become readable just because the section is loaded. Synthetic
    # fixture: no real example pairs 'defaults.packages:' with
    # 'patches:' to test this against directly.
    def test_referenced_files_excludes_defaults_packages(self):
        path = os.path.join(self.workdir, "dormant-patch.yaml")
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "  release: bookworm\n"
                "  architecture: amd64\n"
                "defaults:\n"
                "  packages:\n"
                "  - source: apt://linux\n"
                "    patches:\n"
                "    - patches/hypothetical.patch\n"
                "    extends:\n"
                "      kernel:\n"
                "        flavour: amd64\n"
                "playbook: []\n"
                "image:\n"
                "  filename: test.img\n"
                "  table: gpt\n"
                "  size: 128MiB\n"
                "  partitions:\n"
                "  - label: system\n"
                "    type: ext2\n"
                "    size: 128MiB\n"
                "    where: /\n")
        app = self.SeineApp(files=[path])
        build = app.context.builds[0]
        self.assertEqual(build.referenced_files(), set())

    def test_spec_dump_returns_the_merged_spec_not_one_file(self):
        app = self.SeineApp(files=[PC_IMAGE])
        build = app.context.builds[0]
        text = self.ai.TOOLS["spec-dump"].run(app, {})
        # 'distribution' only appears whole once main.yaml's own and its
        # requires:'d fragments (arm64.yaml, trixie.yaml, ...) are
        # actually merged -- proof this is the resolved tree, not any
        # one loaded file's own text (read's job).
        self.assertIn("architecture: amd64", text)
        self.assertIn("release: bookworm", text)
        # The header names the total so the model knows when it has
        # seen everything without counting lines itself.
        total = len(build.dump(build.spec).splitlines())
        self.assertIn("of %d" % total, text)

    def test_spec_dump_returns_a_requested_line_range(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        full = build.dump(build.spec).splitlines()
        text = self.ai.TOOLS["spec-dump"].run(app, {"start": 2, "end": 3})
        self.assertIn("lines 2-3 of %d" % len(full), text)
        body = text.split("\n\n", 1)[1]
        self.assertEqual(body, "\n".join(full[1:3]))

    def test_spec_dump_says_when_start_is_past_the_end(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["spec-dump"].run(app, {"start": 999999})
        self.assertIn("past the end", text)

    # A range wider than the chunk cap is clamped rather than handed
    # back whole -- the whole reason for chunking is to bound one
    # reply's size regardless of what the model asks for.
    def test_spec_dump_clamps_a_range_wider_than_the_chunk_cap(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["spec-dump"].run(
            app, {"start": 1, "end": 1000000})
        header = text.splitlines()[0]
        shown = header.split("lines ")[1].split(" of")[0]
        start, end = (int(n) for n in shown.split("-"))
        self.assertLessEqual(end - start + 1, self.ai.SPEC_DUMP_CHUNK_LINES)

    # Not spec-scoped -- no active build needed, same as doctor/cache.
    def test_docs_lists_available_files_with_no_name(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {})
        self.assertIn("specification.md", text)
        self.assertIn("kernels.md", text)

    def test_docs_returns_a_chunk_of_a_real_file(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {"name": "specification.md"})
        self.assertTrue(text.startswith("lines 1-"))
        self.assertIn(" of ", text.splitlines()[0])
        self.assertIn("## Specification files", text)

    def test_docs_refuses_an_unknown_name(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {"name": "no-such-file.md"})
        self.assertIn("is not one of this seine's own docs", text)

    # A real file, just not one docs/ itself holds directly -- escaping
    # into seine's own data/ (a secret-bearing file, in principle) or
    # down into docs/images/ must both be refused the same way an
    # unknown name is, not treated as a legal 'docs/*.md'.
    def test_docs_refuses_escaping_the_docs_directory(self):
        app = self.SeineApp()
        for name in ["../seine/data/system_prompt.txt", "images/tui-demo.gif",
                    "../system_prompt.txt"]:
            text = self.ai.TOOLS["docs"].run(app, {"name": name})
            self.assertIn("is not one of this seine's own docs", text)

    def test_docs_clamps_a_range_wider_than_the_chunk_cap(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(
            app, {"name": "specification.md", "start": 1, "end": 1000000})
        header = text.splitlines()[0]
        shown = header.split("lines ")[1].split(" of")[0]
        start, end = (int(n) for n in shown.split("-"))
        self.assertLessEqual(end - start + 1, self.ai.SPEC_DUMP_CHUNK_LINES)

    # The installed location wins over the checkout's own docs/ when
    # both exist, matching a real install. SYSTEM_PROMPT_FILE is
    # redirected into self.workdir rather than the real seine/data/
    # tree -- shared state a prior run once polluted for the whole suite.
    def test_docs_prefers_the_installed_copy_when_present(self):
        fake_system_prompt = os.path.join(self.workdir, "data", "system_prompt.txt")
        installed = os.path.join(self.workdir, "data", "docs")
        os.makedirs(installed)
        with open(os.path.join(installed, "only-here.md"), "w") as f:
            f.write("installed copy\n")
        real_system_prompt_file = self.ai.SYSTEM_PROMPT_FILE
        self.ai.SYSTEM_PROMPT_FILE = fake_system_prompt
        self.addCleanup(setattr, self.ai, "SYSTEM_PROMPT_FILE", real_system_prompt_file)

        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {})
        self.assertIn("only-here.md", text)
        self.assertNotIn("specification.md", text)

    # No installed docs/ next to SYSTEM_PROMPT_FILE (the ordinary case)
    # falls back to the repository root's own docs/ -- every test
    # above already exercises this implicitly; named explicitly so the
    # fallback path has its own test, not just incidental coverage.
    # Same isolated-workdir redirection as the test above, for the
    # same reason -- only 'data/docs' is left uncreated here.
    def test_docs_falls_back_to_the_checkout_when_not_installed(self):
        fake_system_prompt = os.path.join(self.workdir, "data", "system_prompt.txt")
        real_system_prompt_file = self.ai.SYSTEM_PROMPT_FILE
        self.ai.SYSTEM_PROMPT_FILE = fake_system_prompt
        self.addCleanup(setattr, self.ai, "SYSTEM_PROMPT_FILE", real_system_prompt_file)

        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {})
        self.assertIn("specification.md", text)

    # 'docs' also serves the prompt's own cluster files (always
    # present, unlike docs/*.md) -- with docs/ unavailable, the tool
    # still has something to offer rather than going silent.
    def test_docs_falls_back_to_prompt_clusters_when_docs_dir_is_absent(self):
        real_docs_dir = self.ai._docs_dir
        self.ai._docs_dir = lambda: None
        self.addCleanup(setattr, self.ai, "_docs_dir", real_docs_dir)

        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {})
        self.assertIn("gists.txt", text)
        self.assertNotIn("specification.md", text)

    def test_docs_reads_a_prompt_cluster_file_by_name(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {"name": "gists.txt"})
        self.assertTrue(text.startswith("lines 1-"))
        self.assertIn("[GISTS]", text)

    def test_docs_reads_the_sources_cluster_file(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {"name": "sources.txt"})
        self.assertIn("[SOURCES]", text)

    # Lives in run-test's own always-sent description, not only
    # testing.txt, since a "run tests" request has no reason to fetch it.
    def test_run_test_description_warns_it_does_not_write_the_image(self):
        self.assertIn("mtda-write-image", self.ai.TOOLS["run-test"].description)

    def test_docs_says_when_neither_location_has_it(self):
        real_docs_dir = self.ai._docs_dir
        real_prompt_dir = self.ai.PROMPT_DOCS_DIR
        self.ai._docs_dir = lambda: None
        self.ai.PROMPT_DOCS_DIR = os.path.join(self.workdir, "no-such-prompt-dir")
        self.addCleanup(setattr, self.ai, "_docs_dir", real_docs_dir)
        self.addCleanup(setattr, self.ai, "PROMPT_DOCS_DIR", real_prompt_dir)

        app = self.SeineApp()
        text = self.ai.TOOLS["docs"].run(app, {})
        self.assertIn("no documentation available", text)

