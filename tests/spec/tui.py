#!/usr/bin/env python3

import asyncio
import avocado
import contextlib
import json
import os
import sys
import tempfile
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-tui-tests-"))
# Every SeineApp()/BuildCmd() below reads settings.py -- pointed at an
# empty, per-run directory so a real settings.json can never leak in.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="seine-tui-tests-config-")
# None of this file is about the AI chat (that's tests/spec/ai.py's
# job) -- popped, not just left unset, so a real endpoint exported in
# the shell running the whole suite can't silently route a bare-text
# test here into seine.tui.ai.ask() instead of the plain CommandError
# it's actually testing for.
for _var in ("SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"):
    os.environ.pop(_var, None)

# 'History' (seine/tui/history.py) is deliberately cwd-relative
# ('./.seine/history.json') -- every test in this file constructs a real
# 'SeineApp', so without this the whole suite would write a '.seine/'
# into the checkout avocado was run from. Everything above is already an
# absolute path, so moving the process elsewhere changes nothing else.
os.chdir(tempfile.mkdtemp(prefix="seine-tui-tests-cwd-"))

# Static keeps update()'s text in a private attribute: '_content' in
# Debian trixie's python3-textual (2.1.2), name-mangled '__content' in
# newer pip releases. Checking both here once beats the whole suite
# silently reading back "".
def _content(widget):
    if hasattr(widget, "_content"):
        return widget._content
    return getattr(widget, "_Static__content", "")

PC_IMAGE = os.path.join(path_to_sources, "examples", "pc-image", "main.yaml")

def _run(scenario):
    asyncio.run(scenario())

def _run_value(coroutine):
    return asyncio.run(coroutine)

# Every setUp() below needs this: a name bound inside the 'with' is
# still there after it, unless the import raised -- in which case
# self.cancel() has already aborted the test before anything after
# the block runs.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# Commands: the registry the prompt, Tab completion and the command
# palette all read.
class CommandRegistry(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import commands
        self.commands = commands

    def test_split_is_shell_like(self):
        self.assertEqual(self.commands.split('use "a b.yaml" c.yaml'),
                         ["use", "a b.yaml", "c.yaml"])

    def test_split_rejects_an_unterminated_quote(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.split, 'use "unterminated')

    def test_unknown_command_is_a_command_error(self):
        self.assertRaises(self.commands.CommandError,
                          self.commands.dispatch, object(), "/bogus")

    # A command starts with '/' -- 'build', not '/build', is not one,
    # on purpose: it leaves plain text free for something else later
    # (asking an LLM a question, say) without the two ever colliding.
    def test_a_line_without_a_leading_slash_is_not_a_command(self):
        try:
            self.commands.dispatch(object(), "build")
            self.fail("a line with no leading '/' ran as a command")
        except self.commands.CommandError as e:
            self.assertIn("'/'", str(e))

    def test_q_is_a_quit_alias(self):
        self.assertIs(self.commands.REGISTRY["q"], self.commands.REGISTRY["quit"])

    # '@' marks a path for the prompt's own filesystem completion
    # (seine/tui/paths.py) -- a command handler never sees it.
    def test_at_is_stripped_from_arguments_before_a_command_runs(self):
        original = self.commands.REGISTRY["use"]
        self.addCleanup(self.commands.REGISTRY.__setitem__, "use", original)
        seen = {}
        self.commands.REGISTRY["use"] = original._replace(
            run=lambda app, argv: seen.setdefault("argv", argv))

        self.commands.dispatch(object(), "/use @examples/pc-image/main.yaml plain")
        self.assertEqual(seen["argv"], ["examples/pc-image/main.yaml", "plain"])

# Context: what 'use' sets, and what Overview/Plan act on.
class ActiveSpecification(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
        self.Context = Context

    def test_unused_context_is_inactive(self):
        context = self.Context()
        self.assertFalse(context.active)
        self.assertIsNone(context.label())

    def test_use_loads_and_labels_a_real_spec(self):
        context = self.Context()
        context.use([PC_IMAGE])
        self.assertTrue(context.active)
        self.assertEqual(context.label(), "pc-image")
        self.assertEqual(context.groups, [[PC_IMAGE]])

    def test_use_rejects_a_missing_file_and_leaves_context_inactive(self):
        context = self.Context()
        self.assertRaises((OSError, ValueError),
                          context.use, ["/does/not/exist.yaml"])
        self.assertFalse(context.active)

# What the Overview/Plan screens show, without a Textual App around it.
class Rendering(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.render import render_overview, render_plan
        self.Context = Context
        self.render_overview = render_overview
        self.render_plan = render_plan

    def test_no_active_spec_says_so(self):
        self.assertIn("use", self.render_overview(self.Context()))
        self.assertIn("use", self.render_plan(self.Context()))

    def test_overview_over_a_real_spec(self):
        context = self.Context()
        context.use([PC_IMAGE])
        text = self.render_overview(context)
        self.assertIn("pc-image", text)
        self.assertIn("would write:", text)

    # Same wording 'Image.plan()' itself prints -- a renderer over it, not
    # a second computation of what a build would do.
    def test_plan_matches_image_plan(self):
        context = self.Context()
        context.use([PC_IMAGE])
        text = self.render_plan(context)
        self.assertIn("would build", text)
        self.assertIn("steps:", text)

# What the last build actually wrote: a stat-based listing of
# 'ContainerEngine.deploy_root()/<release>/', nothing tracked separately.
class ArtifactsRendering(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.render import render_artifacts
        self.Context = Context
        self.render_artifacts = render_artifacts

    def test_no_active_spec_says_so(self):
        self.assertIn("use", self.render_artifacts(self.Context()))

    def test_an_empty_deploy_directory_says_so(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "deploy")
        context = self.Context()
        context.use([PC_IMAGE])
        text = self.render_artifacts(context)
        self.assertIn("nothing built here yet", text)

    def test_lists_what_is_actually_there(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        deploy = os.path.join(self.workdir, "deploy")
        os.environ["SEINE_DEPLOY_DIR"] = deploy
        context = self.Context()
        context.use([PC_IMAGE])
        release = context.builds[0].spec["distribution"]["release"]
        os.makedirs(os.path.join(deploy, release), exist_ok=True)
        with open(os.path.join(deploy, release, "pc-image.img"), "wb") as f:
            f.write(b"x" * 2048)
        text = self.render_artifacts(context)
        self.assertIn("pc-image.img", text)
        self.assertIn("2.0KiB", text)

BUSYBOX_REBUILD = os.path.join(path_to_sources, "examples", "rebuild-busybox", "main.yaml")

# Two data sources shown as one screen: the 'packages:' section (real
# today, zero new engine work) and the last SBOM, if one exists.
class PackagesRendering(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.render import render_packages
        self.Context = Context
        self.render_packages = render_packages
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "deploy")

    def test_no_active_spec_says_so(self):
        self.assertIn("use", self.render_packages(self.Context()))

    def test_a_spec_with_no_packages_section_says_none(self):
        context = self.Context()
        context.use([PC_IMAGE])
        text = self.render_packages(context)
        self.assertIn("REBUILT FROM SOURCE", text)
        self.assertIn("none", text)

    def test_a_rebuilt_package_and_its_origin(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        text = self.render_packages(context)
        self.assertIn("busybox", text)
        self.assertIn("apt://busybox", text)
        self.assertIn("declared by:", text)
        self.assertIn("busybox.yaml", text)

    def test_no_sbom_says_so(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        text = self.render_packages(context)
        self.assertIn("no SBOM for this build", text)

    def test_an_sbom_is_listed(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        output = context.builds[0].image._output
        sbom_path = output[:-len(".img")] + "-sbom.spdx.json" \
                   if output.endswith(".img") else output + "-sbom.spdx.json"
        os.makedirs(os.path.dirname(sbom_path), exist_ok=True)
        with open(sbom_path, "w") as f:
            json.dump({"packages": [{"name": "base-files", "versionInfo": "12.4"},
                                    {"name": "glibc", "versionInfo": "2.36"}]}, f)
        text = self.render_packages(context)
        self.assertIn("2 packages", text)
        self.assertIn("base-files", text)
        self.assertIn("12.4", text)

# Scanned via settings.sbom2cve_program (a real, tiny external program,
# not a container stand-in) rather than pulling debsbom's own image --
# same "exercise the real subprocess call" choice tests/spec/secscan.py's
# own ACustomProgramReplacesTheContainer makes.
def _fake_scanner(workdir, findings):
    import stat
    path = os.path.join(workdir, "fake-scanner")
    # One print() per finding -- debsbom's own '-f json' is JSON-lines,
    # not a single document, so the fake has to match that shape too.
    body = "".join("print(%r)\n" % json.dumps(f) for f in findings)
    with open(path, "w") as f:
        f.write("#!/usr/bin/env python3\n" + body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path

class IssuesRendering(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.render import render_issues_stats, render_issues_table
        self.Context = Context
        self.render_issues_table = render_issues_table
        self.render_issues_stats = render_issues_stats
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_DEPLOY_DIR"] = os.path.join(self.workdir, "deploy")

    def _configure_scanner(self, findings):
        from seine import settings
        current = settings.load()
        current["sbom2cve_program"] = _fake_scanner(self.workdir, findings)
        settings.save(current)

    def _write_sbom(self, context):
        output = context.builds[0].image._output
        path = output[:-len(".img")] + "-sbom.spdx.json" \
              if output.endswith(".img") else output + "-sbom.spdx.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{}")

    def test_no_active_spec_says_so(self):
        self.assertIn("use", self.render_issues_table(self.Context()))
        self.assertIn("use", self.render_issues_stats(self.Context()))

    def test_no_sbom_says_so(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        self.assertIn("no SBOM for this build", self.render_issues_table(context))
        self.assertIn("no SBOM for this build", self.render_issues_stats(context))

    def test_findings_are_listed_with_package_urgency_and_status(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        self._write_sbom(context)
        self._configure_scanner([
            {"package": "busybox@1.36", "vulnerability":
                {"id": "CVE-2024-0001", "status": "open", "urgency": "high"}},
        ])
        text = self.render_issues_table(context)
        self.assertIn("CVE-2024-0001", text)
        self.assertIn("busybox", text)
        self.assertIn("high", text)
        self.assertIn("open", text)

    def test_no_findings_says_so(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        self._write_sbom(context)
        self._configure_scanner([])
        self.assertIn("no known CVEs found", self.render_issues_table(context))

    def test_filter_and_min_urgency_narrow_the_table(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        self._write_sbom(context)
        self._configure_scanner([
            {"package": "busybox@1.36", "vulnerability":
                {"id": "CVE-1", "status": "open", "urgency": "high"}},
            {"package": "vim@9.0", "vulnerability":
                {"id": "CVE-2", "status": "open", "urgency": "low"}},
        ])
        text = self.render_issues_table(context, package="busybox")
        self.assertIn("CVE-1", text)
        self.assertNotIn("CVE-2", text)

        text = self.render_issues_table(context, min_urgency="high")
        self.assertIn("CVE-1", text)
        self.assertNotIn("CVE-2", text)

    def test_stats_show_totals_and_top_packages(self):
        context = self.Context()
        context.use([BUSYBOX_REBUILD])
        self._write_sbom(context)
        self._configure_scanner([
            {"package": "busybox@1.36", "vulnerability":
                {"id": "CVE-1", "status": "open", "urgency": "high"}},
            {"package": "busybox@1.36", "vulnerability":
                {"id": "CVE-2", "status": "open", "urgency": "low"}},
        ])
        text = self.render_issues_stats(context)
        self.assertIn("2 findings", text)
        self.assertIn("2 unique CVEs", text)
        self.assertIn("1 packages affected", text)
        self.assertIn("busybox", text)

# 'analyze'/'cache'/'doctor' are all renderers over an engine function
# that already prints ('analyze.blame()', 'CacheCmd.info()',
# 'doctor.render()') -- captured, not reimplemented.
class AnalyzeCacheDoctorRendering(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.render import render_analyze, render_cache, render_doctor
        self.Context = Context
        self.render_analyze = render_analyze
        self.render_cache = render_cache
        self.render_doctor = render_doctor
        os.environ["SEINE_CACHE_DIR"] = self.workdir

    def test_analyze_no_active_spec_says_so(self):
        self.assertIn("use", self.render_analyze(self.Context()))

    def test_analyze_with_no_recorded_run_says_so(self):
        context = self.Context()
        context.use([PC_IMAGE])
        text = self.render_analyze(context)
        self.assertIn("no recorded run", text)

    def test_analyze_reads_back_a_real_record(self):
        from seine import analyze, tasks
        context = self.Context()
        context.use([PC_IMAGE])
        digest = analyze.spec_digest(context.builds[0].spec)
        step = tasks.Task("rootfs", lambda: None)
        step.started, step.ended, step.failed = 0.0, 30.0, False
        analyze.record([step], digest, jobs=1, ok=True)
        text = self.render_analyze(context)
        self.assertIn("rootfs", text)
        self.assertIn("30s", text)

    def test_cache_lists_every_kind(self):
        text = self.render_cache()
        for kind in ["downloads", "packages", "chroots", "bootstraps"]:
            self.assertIn(kind, text)

    def test_doctor_lists_every_group(self):
        text = self.render_doctor()
        for group in ["Container engine", "Imaging", "Ansible", "Storage"]:
            self.assertIn(group, text)

# Not spec-scoped either, and reads 'seine/settings.py' straight --
# nothing here goes through 'Context'.
class SettingsRendering(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.render import render_settings
        self.render_settings = render_settings
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_nothing_set_says_default(self):
        text = self.render_settings()
        self.assertIn("jobs             1 (default)", text)
        self.assertIn("theme            dark (default)", text)
        self.assertIn("llm_model        (unset)", text)
        self.assertIn("llm_api_base     (unset)", text)

    def test_a_configured_value(self):
        from seine import settings
        current = settings.load()
        current["jobs"] = 4
        current["theme"] = "dark"
        current["llm_model"] = "openai/some-model"
        settings.save(current)
        text = self.render_settings()
        self.assertIn("jobs             4", text)
        self.assertIn("theme            dark", text)
        self.assertIn("llm_model        openai/some-model", text)

# Ghost-text completion in the prompt: command names, then a command's
# own flags.
class Completion(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.base import CommandSuggester
        self.suggester = CommandSuggester()

    def test_completes_a_command_name(self):
        self.assertEqual(_run_value(self.suggester.get_suggestion("/pl")), "/plan")

    def test_completes_a_commands_own_long_option(self):
        self.assertEqual(_run_value(self.suggester.get_suggestion("/plan --j")),
                         "/plan --jobs=")

    def test_no_suggestion_for_an_unknown_command(self):
        self.assertIsNone(_run_value(self.suggester.get_suggestion("/bogus")))

    def test_no_suggestion_for_an_empty_prompt(self):
        self.assertIsNone(_run_value(self.suggester.get_suggestion("")))

    # Text with no leading '/' is never a command -- no suggestion for
    # it either, on purpose: it leaves plain text free for something
    # else later without a ghost-text command suggestion ever appearing
    # on it.
    def test_no_suggestion_without_a_leading_slash(self):
        self.assertIsNone(_run_value(self.suggester.get_suggestion("plan")))

# The '@fragment' filesystem-completion pane: a real 'App', real key
# presses, a real (small, fixture) directory tree -- driven the same way
# a person would drive it, not through 'PathCompletions'/'Prompt' in
# isolation.
class PathCompletionUI(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.base import PathCompletions
        self.SeineApp = SeineApp
        self.PathCompletions = PathCompletions

        self._cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, self._cwd)
        os.makedirs("stuff/deep")
        # A real, minimal, loadable spec -- 'one.yaml' is used as a
        # '/use' target below, and '/use' really loads what it is given.
        with open("stuff/one.yaml", "w") as f:
            f.write("""
image:
    filename: simple.img
    partitions:
        - label: rootfs
          where: /
""")
        with open("stuff/two.yaml", "w"):
            pass

    async def _type(self, pilot, text):
        for ch in text:
            await pilot.press(ch)
        await pilot.pause()

    def test_typing_at_opens_the_pane(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.click("#prompt")
                pane = app.screen.query_one("#completions", self.PathCompletions)
                self.assertFalse(pane.display)
                await self._type(pilot, "/use @stu")
                self.assertTrue(pane.display)
                self.assertEqual([str(o.prompt) for o in pane._options], ["stuff/"])
        _run(scenario)

    def test_an_exact_match_does_not_leave_the_pane_open(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.click("#prompt")
                pane = app.screen.query_one("#completions", self.PathCompletions)
                await self._type(pilot, "/use @stuff/one.yaml")
                self.assertFalse(pane.display,
                                 "a fully-typed, exact match should not dangle "
                                 "in front of Enter")
        _run(scenario)

    def test_enter_accepts_the_highlighted_entry_and_adds_a_space(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                pane = app.screen.query_one("#completions", self.PathCompletions)
                await pilot.click("#prompt")
                await self._type(pilot, "/use @stu")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(prompt.value, "/use @stuff/ ")
                self.assertFalse(pane.display)
                self.assertEqual(prompt.cursor_position, len(prompt.value))
        _run(scenario)

    # Up/Down move the pane's own selection while it is open -- not
    # history -- and do not change which widget has focus.
    def test_up_down_move_the_pane_not_the_history(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                pane = app.screen.query_one("#completions", self.PathCompletions)
                await pilot.click("#prompt")
                await self._type(pilot, "/use @stuff/")
                self.assertEqual(pane.highlighted, 0)
                self.assertTrue(prompt.has_focus)
                await pilot.press("down")
                self.assertEqual(pane.highlighted, 1)
                self.assertTrue(prompt.has_focus,
                                "focus must stay on the prompt")
                await pilot.press("up")
                self.assertEqual(pane.highlighted, 0)
        _run(scenario)

    # Live bug: 'action_submit()' re-syncs the pane against the current
    # value before reading the selection -- with the fragment unchanged
    # since the last keystroke (only Up/Down happened), that re-sync
    # must not rebuild the pane and reset 'highlighted' back to 0,
    # accepting the first entry instead of the one arrowed onto.
    def test_enter_accepts_the_arrowed_to_entry_not_the_first(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                pane = app.screen.query_one("#completions", self.PathCompletions)
                await pilot.click("#prompt")
                await self._type(pilot, "/use @stuff/")
                await pilot.press("down")
                self.assertEqual(pane.highlighted, 1)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(prompt.value, "/use @stuff/one.yaml ")
        _run(scenario)

    # '/nope' throughout this file: a deliberately unknown command, so
    # these prompt/history/completion tests exercise CommandError ->
    # #status without a real command's own side effects getting in the way.
    def test_enter_with_the_pane_closed_still_submits(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.click("#prompt")
                await self._type(pilot, "/nope")
                await pilot.press("enter")
                await pilot.pause()
                status = app.screen.query_one("#status")
                self.assertTrue(_content(status))
        _run(scenario)

    # The history keeps the '@' exactly as typed -- what a command
    # actually saw is commands.py's business (CommandRegistry, above),
    # not the prompt's.
    def test_history_keeps_the_at_sign(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await pilot.click("#prompt")
                await self._type(pilot, "/use @stuff/one.yaml")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.history.lines[-1]["line"], "/use @stuff/one.yaml")
        _run(scenario)

    # Real typing can have Enter arrive a keystroke ahead of the pane --
    # accepting must degrade to "nothing to accept, submit instead"
    # rather than crash, whatever state the pane is really in.
    def test_a_stale_pane_falls_through_to_a_real_submit_not_a_crash(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                pane = app.screen.query_one("#completions", self.PathCompletions)
                await pilot.click("#prompt")
                await self._type(pilot, "/nope")

                for exc in (IndexError("stale"), RuntimeError("whatever"),
                           AttributeError("also whatever")):
                    pane.show(["stuff/"])  # a pane the last keystroke never asked for
                    original = type(pane).get_option_at_index
                    def boom(self, index, exc=exc):
                        raise exc
                    type(pane).get_option_at_index = boom
                    try:
                        accepted = prompt._accept_completion()
                    finally:
                        type(pane).get_option_at_index = original
                    self.assertFalse(accepted, "for %r" % exc)
                    self.assertFalse(pane.display, "for %r" % exc)

                await pilot.press("enter")
                await pilot.pause()
                status = app.screen.query_one("#status")
                self.assertTrue(_content(status),
                                "the fallen-through Enter should still submit /nope")
        _run(scenario)

# The application: screens, the prompt, history and the '!' escape.
class App(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import (AnalyzeScreen, ArtifactsScreen, CacheScreen,
                                       DiffScreen, DoctorScreen, IssuesScreen,
                                       OverviewScreen, PackagesScreen, PlanScreen,
                                       SeineApp)
        self.AnalyzeScreen = AnalyzeScreen
        self.ArtifactsScreen = ArtifactsScreen
        self.CacheScreen = CacheScreen
        self.DiffScreen = DiffScreen
        self.DoctorScreen = DoctorScreen
        self.IssuesScreen = IssuesScreen
        self.OverviewScreen = OverviewScreen
        self.PackagesScreen = PackagesScreen
        self.PlanScreen = PlanScreen
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        # Per test, not just per file (the module-level default above is
        # shared by the whole run) -- the '/set'/startup-commands tests
        # below persist real settings, which must not leak into an
        # earlier-defined test that runs after them.
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_startup_with_a_spec_lands_on_overview(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test():
                self.assertIsNone(app._startup_error)
                self.assertTrue(app.context.active)
                self.assertIsInstance(app.screen, self.OverviewScreen)
        _run(scenario)

    def test_startup_with_a_bad_spec_shows_the_error_not_a_crash(self):
        async def scenario():
            app = self.SeineApp(files=["/does/not/exist.yaml"])
            async with app.run_test():
                self.assertIsNotNone(app._startup_error)
                self.assertFalse(app.context.active)
        _run(scenario)

    # No files at all (bare 'seine tui') -- not the same case as a bad
    # one above: nothing was given to fail, so there is no startup error
    # to show on Overview either. Opens on Doctor instead: what the
    # machine itself can build at all is the more useful first thing to
    # see with nothing yet to '/use'.
    def test_startup_with_no_spec_lands_on_doctor(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test():
                self.assertIsNone(app._startup_error)
                self.assertFalse(app.context.active)
                self.assertIsInstance(app.screen, self.DoctorScreen)
        _run(scenario)

    def test_artifacts_command_switches_screen(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/artifacts"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.ArtifactsScreen)
        _run(scenario)

    def test_packages_command_switches_screen(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/packages"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.PackagesScreen)
        _run(scenario)

    def test_analyze_command_switches_screen(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/analyze"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.AnalyzeScreen)
        _run(scenario)

    # Not spec-scoped -- reachable with no active specification at all.
    def test_cache_and_doctor_need_no_active_spec(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/cache"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.CacheScreen)

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/doctor"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.DoctorScreen)
        _run(scenario)

    # A command has to start with '/' -- typing one without it is not
    # silently ignored, and not run either.
    def test_a_command_without_a_leading_slash_is_refused(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "plan"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.OverviewScreen)
                status = app.screen.query_one("#status")
                self.assertIn("'/'", _content(status))
        _run(scenario)

    # The command palette (Ctrl+P) fills the prompt with a leading '/'
    # too -- what it fills in has to be runnable as typed.
    def test_palette_fills_the_prompt_with_a_leading_slash(self):
        from seine.tui.app import RegistryProvider
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test():
                provider = RegistryProvider(app.screen)
                provider._fill("plan")()
                prompt = app.screen.query_one("#prompt")
                self.assertEqual(prompt.value, "/plan ")
        _run(scenario)

    # 'SeineApp()' with no files: opens on Doctor, not Overview -- the bad
    # '/diff' still fails the same way, on whichever screen was already
    # open.
    def test_diff_needs_exactly_two_sbom_files(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/diff one.json"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.DoctorScreen)
                status = app.screen.query_one("#status")
                self.assertIn("exactly two",
                              _content(status))
        _run(scenario)

    def test_diff_shows_a_real_sbom_diff(self):
        async def scenario():
            old = os.path.join(self.workdir, "old.spdx.json")
            new = os.path.join(self.workdir, "new.spdx.json")
            with open(old, "w") as f:
                f.write('{"packages": [{"name": "telnet", "versionInfo": "1"}]}')
            with open(new, "w") as f:
                f.write('{"packages": [{"name": "openssh-server", "versionInfo": "2"}]}')
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/diff %s %s" % (old, new)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.DiffScreen)
                self.assertIn("openssh-server", app.diff_text)
        _run(scenario)

    def test_issues_needs_an_active_spec(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/issues"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.DoctorScreen)
                self.assertIn("no active specification", _content(app.screen.query_one("#status")))
        _run(scenario)

    def test_issues_command_switches_screen_and_scans(self):
        async def scenario():
            app = self.SeineApp(files=[BUSYBOX_REBUILD])
            build = app.context.builds[0]
            output = build.image._output
            sbom_path = output[:-len(".img")] + "-sbom.spdx.json" \
                       if output.endswith(".img") else output + "-sbom.spdx.json"
            os.makedirs(os.path.dirname(sbom_path), exist_ok=True)
            with open(sbom_path, "w") as f:
                f.write("{}")
            from seine import settings
            current = settings.load()
            current["sbom2cve_program"] = _fake_scanner(self.workdir, [
                {"package": "busybox@1.36", "vulnerability":
                    {"id": "CVE-2024-0001", "status": "open", "urgency": "high"}}])
            settings.save(current)

            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/issues --min-urgency=high"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.IssuesScreen)
                self.assertIn("CVE-2024-0001", _content(app.screen.query_one("#issuestable")))
                self.assertIn("1 findings", _content(app.screen.query_one("#issuesstats")))
        _run(scenario)

    # The table is the reason to be on this screen -- it must end up
    # the widest pane, with the spec tree narrowed well below its usual
    # 2fr (app.py's own global rule, right for a screen with one plain-
    # text body pane, wrong once a second pane joins it here).
    def test_issues_table_is_the_widest_pane(self):
        async def scenario():
            app = self.SeineApp(files=[BUSYBOX_REBUILD])
            async with app.run_test(size=(150, 40)) as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/issues"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.IssuesScreen)
                spectree = app.screen.query_one("#spectree").size.width
                table = app.screen.query_one("#issuestable-pane").size.width
                stats = app.screen.query_one("#issuesstats-pane").size.width
                self.assertGreater(table, stats)
                self.assertGreater(stats, spectree)
        _run(scenario)

    def test_issues_bad_option_is_refused(self):
        async def scenario():
            app = self.SeineApp(files=[BUSYBOX_REBUILD])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/issues --not-a-real-option"
                await pilot.press("enter")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, self.IssuesScreen)
        _run(scenario)

    def test_plan_command_switches_screen_and_back(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/plan"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.PlanScreen)

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/overview"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.OverviewScreen)
        _run(scenario)

    def test_unknown_command_is_shown_inline_not_raised(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/bogus"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.OverviewScreen)
        _run(scenario)

    # An error message is never something this code wrote for Rich markup
    # to parse -- a file path, an argv list, a 'CalledProcessError' can
    # all contain a bare '[' and must still render, not raise a
    # 'MarkupError' from inside 'say()' itself.
    def test_an_error_containing_brackets_still_renders(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test():
                message = ("CalledProcessError: Command "
                          "'['podman', '--root', 'x']' returned non-zero "
                          "exit status 25.")
                app.screen.say(message, error=True)
                status = app.screen.query_one("#status")
                self.assertEqual(_content(status) or None, message)
                self.assertIn("error", status.classes)
        _run(scenario)

    def test_history_recalls_previous_commands_on_up(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/nope"
                await pilot.press("enter")
                await pilot.pause()
                prompt = app.screen.query_one("#prompt")
                await pilot.press("up")
                self.assertEqual(prompt.value, "/nope")
        _run(scenario)

    # Recalled and resubmitted unchanged: not a second entry. Recalled
    # and then edited: is -- it is new text, indistinguishable from
    # having typed it from scratch.
    def test_an_unmodified_recall_does_not_duplicate_in_history(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/nope"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual([e["line"] for e in app.history.lines], ["/nope"])

                prompt = app.screen.query_one("#prompt")
                await pilot.press("up")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual([e["line"] for e in app.history.lines], ["/nope"])

                prompt = app.screen.query_one("#prompt")
                await pilot.press("up")
                await pilot.press("x")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual([e["line"] for e in app.history.lines], ["/nope", "/nopex"])
        _run(scenario)

    # '!<command>' hands the real terminal to a real shell via
    # 'App.suspend()' -- both are faked here rather than actually
    # suspending the test's own terminal.
    def test_bang_runs_a_real_shell_command_via_suspend(self):
        async def scenario():
            import seine.tui.app as tui_app
            app = self.SeineApp(files=[PC_IMAGE])
            calls = []

            @contextlib.contextmanager
            def fake_suspend():
                calls.append("suspended")
                yield

            class FakeResult:
                returncode = 0

            def fake_run(argv):
                calls.append(argv)
                return FakeResult()

            app.suspend = fake_suspend
            real_run = tui_app.subprocess.run
            tui_app.subprocess.run = fake_run
            try:
                async with app.run_test() as pilot:
                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "!echo hi"
                    await pilot.press("enter")
                    await pilot.pause()
            finally:
                tui_app.subprocess.run = real_run

            self.assertEqual(calls[0], "suspended")
            self.assertEqual(calls[1][-1], "echo hi")
        _run(scenario)

    def test_set_jobs_persists_and_is_validated(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set jobs not-a-number"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("expects a number", _content(app.screen.query_one("#status")))

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set jobs 4"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["jobs"], 4)
        _run(scenario)

    # A bad theme name is a 'CommandError', not 'App.theme' raising
    # 'InvalidThemeError' straight out of the prompt -- and a good one
    # is applied immediately, not only on the next startup.
    def test_set_theme_is_validated_and_applied_live(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set theme gruvbox"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("'dark' or 'light'", _content(app.screen.query_one("#status")))

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set theme dark"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["theme"], "dark")
                self.assertEqual(app.theme, "textual-dark")
        _run(scenario)

    def test_set_sbom2cve_program_persists(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set sbom2cve_program /usr/local/bin/my-scanner"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["sbom2cve_program"],
                                 "/usr/local/bin/my-scanner")
        _run(scenario)

    # Validated the same way jobs/theme are: a bad value is a
    # CommandError, not a crash the next time History reads it back
    # (seine.tui.history.parse_prune_after()).
    def test_set_history_pruning_persists_and_is_validated(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set history_pruning not-a-duration"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("history_pruning expects",
                             _content(app.screen.query_one("#status")))

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/set history_pruning 15d"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["history_pruning"], "15d")
        _run(scenario)

    # Deferred to after the initial screen mounts (a startup command can
    # switch screens), so this waits one pump of the event loop rather
    # than asserting the instant 'SeineApp()' returns.
    def test_startup_commands_run_once_after_the_first_screen_mounts(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["startup_commands"] = ["/doctor"]
            settings.save(current)
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, self.DoctorScreen)
        _run(scenario)

# The Settings screen (seine/tui/settings.py): a modal, the same shape
# as '/help' -- opened over whatever screen was already there, closed
# back to it. 'jobs'/'theme' are covered by 'App''s own '/set' tests
# above; this is the one thing the screen actually edits itself.
class SettingsScreenIntegration(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import OverviewScreen, SeineApp
            from seine.tui.settings import GeneralSettings, SettingsScreen, StartupCommands
        self.GeneralSettings = GeneralSettings
        self.OverviewScreen = OverviewScreen
        self.SeineApp = SeineApp
        self.SettingsScreen = SettingsScreen
        self.StartupCommands = StartupCommands
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        # Per test, not just per file -- every test here reads/writes
        # real settings, which must not leak between them.
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    async def _open(self, pilot, app):
        prompt = app.screen.query_one("#prompt")
        prompt.value = "/settings"
        await pilot.press("enter")
        await pilot.pause()

    # A real keypress, not '.focus()' called directly -- what moves
    # focus here is the same 'Tab' every other screen already uses to
    # switch panes ('Screen.BINDINGS' 's own 'tab' -> 'app.focus_next',
    # nothing this screen adds itself), and this is the one thing worth
    # exercising as a real keypress rather than a shortcut.
    async def _focus_startup(self, pilot, app):
        await pilot.press("tab")
        await pilot.pause()
        self.assertTrue(app.screen.query_one(self.StartupCommands).has_focus)

    def test_opens_over_whatever_screen_was_there_and_escape_returns(self):
        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                self.assertIsInstance(app.screen, self.SettingsScreen)
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.OverviewScreen)
        _run(scenario)

    def test_general_settings_has_focus_on_open(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                general = app.screen.query_one(self.GeneralSettings)
                self.assertTrue(general.has_focus)
                self.assertEqual(general.highlighted, 0)
        _run(scenario)

    # The bug this fixes: Tab moved focus fine but neither list had a
    # focus-border, so a person aiming Enter at 'theme' could silently
    # hit the startup list instead. Checked via has_focus, both directions.
    def test_tab_moves_focus_between_the_two_lists_and_back(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                general = app.screen.query_one(self.GeneralSettings)
                startup = app.screen.query_one(self.StartupCommands)
                self.assertTrue(general.has_focus)
                await self._focus_startup(pilot, app)
                self.assertFalse(general.has_focus)
                await pilot.press("tab")
                await pilot.pause()
                self.assertTrue(general.has_focus)
                self.assertFalse(startup.has_focus)
        _run(scenario)

    def test_editing_jobs(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("enter")  # 'jobs' is the first, already-highlighted row
                await pilot.pause()
                self.assertEqual(_content(app.screen.query_one("#editlabel")), "jobs")
                editrow = app.screen.query_one("#editrow")
                self.assertEqual(editrow.value, "")
                editrow.value = "4"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["jobs"], 4)
        _run(scenario)

    # An invalid value leaves '#editrow' open with what was typed still
    # in it, rather than reverting it -- there is nowhere else on this
    # modal to say why it didn't take (no '#status', unlike a real
    # screen), so the hint line says it instead.
    def test_editing_jobs_with_a_bad_value_keeps_the_editor_open(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("enter")
                editrow = app.screen.query_one("#editrow")
                editrow.value = "not-a-number"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIn("expects a number", _content(app.screen.query_one("#settingshint")))
                self.assertTrue(editrow.display)
                self.assertIsNone(settings.load()["jobs"])
        _run(scenario)

    def test_editing_jobs_prefills_the_current_value(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["jobs"] = 3
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#editrow").value, "3")
        _run(scenario)

    # 'Down' to 'theme', the second general row: a closed choice opens
    # '#themepicker', not '#editrow' -- 'Enter' on 'dark'/'light' commits
    # straight away, there is no separate "confirm" step and no value
    # it could ever post that needs validating.
    def test_editing_theme(self):
        async def scenario():
            from seine import settings
            from seine.tui.settings import ThemePicker
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(_content(app.screen.query_one("#editlabel")), "theme")
                picker = app.screen.query_one(ThemePicker)
                self.assertTrue(picker.display)
                self.assertFalse(app.screen.query_one("#editrow").display)
                picker.highlighted = 0  # 'dark'
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["theme"], "dark")
                self.assertEqual(app.theme, "textual-dark")
        _run(scenario)

    # Reopening the picker highlights whatever is already set, not
    # always the first option.
    def test_editing_theme_highlights_the_current_value(self):
        async def scenario():
            from seine import settings
            from seine.tui.settings import ThemePicker
            current = settings.load()
            current["theme"] = "light"
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("down")
                await pilot.press("enter")
                await pilot.pause()
                picker = app.screen.query_one(ThemePicker)
                self.assertEqual(str(picker.get_option_at_index(picker.highlighted).prompt),
                                 "light")
        _run(scenario)

    # 'Del' on a general row clears it back to unset -- same word, same
    # place it reaches on the startup-commands list below.
    def test_del_clears_a_general_setting(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["jobs"] = 4
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("delete")
                await pilot.pause()
                self.assertIsNone(settings.load()["jobs"])
        _run(scenario)

    def test_del_clears_theme(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["theme"] = "dark"
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await pilot.press("down")
                await pilot.press("delete")
                await pilot.pause()
                self.assertIsNone(settings.load()["theme"])
        _run(scenario)

    # Nothing but the trailing "add" row when no startup commands are
    # set -- Enter on it opens '#editrow' empty, not pre-filled with a
    # real row's text.
    def test_nothing_set_is_just_the_add_row(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                startup = app.screen.query_one(self.StartupCommands)
                self.assertEqual(startup.option_count, 1)
                await self._focus_startup(pilot, app)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(_content(app.screen.query_one("#editlabel")),
                                 "new startup command")
                editrow = app.screen.query_one("#editrow")
                self.assertEqual(editrow.value, "")
                self.assertTrue(editrow.display)
        _run(scenario)

    def test_adding_a_command_via_the_placeholder_row(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                await pilot.press("enter")
                await pilot.pause()
                editrow = app.screen.query_one("#editrow")
                editrow.value = "/plan"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["startup_commands"], ["/plan"])
                startup = app.screen.query_one(self.StartupCommands)
                # The one real row plus the placeholder, still there
                # after adding to it.
                self.assertEqual(startup.option_count, 2)
        _run(scenario)

    # Empty text submitted on the placeholder is a no-op, not an empty
    # command silently added.
    def test_submitting_the_placeholder_empty_adds_nothing(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                await pilot.press("enter")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["startup_commands"], [])
        _run(scenario)

    def test_editing_an_existing_row(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["startup_commands"] = ["/plan"]
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                startup = app.screen.query_one(self.StartupCommands)
                startup.highlighted = 0
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(_content(app.screen.query_one("#editlabel")),
                                 "startup command")
                editrow = app.screen.query_one("#editrow")
                self.assertEqual(editrow.value, "/plan")
                editrow.value = "/doctor"
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["startup_commands"], ["/doctor"])
        _run(scenario)

    # Escape while editing cancels that one edit -- back to the list,
    # the row unchanged -- not the whole screen.
    def test_escape_while_editing_cancels_not_closes(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["startup_commands"] = ["/plan"]
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                startup = app.screen.query_one(self.StartupCommands)
                startup.highlighted = 0
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.SettingsScreen)
                self.assertEqual(settings.load()["startup_commands"], ["/plan"])
        _run(scenario)

    # Submitting a real row empty clears it -- the slow path to the
    # same place 'Del' reaches directly, below.
    def test_clearing_a_row_by_editing_it_empty(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["startup_commands"] = ["/plan", "/cache"]
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                startup = app.screen.query_one(self.StartupCommands)
                startup.highlighted = 0
                await pilot.press("enter")
                await pilot.pause()
                editrow = app.screen.query_one("#editrow")
                editrow.value = ""
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(settings.load()["startup_commands"], ["/cache"])
        _run(scenario)

    def test_del_clears_the_highlighted_row(self):
        async def scenario():
            from seine import settings
            current = settings.load()
            current["startup_commands"] = ["/plan", "/cache"]
            settings.save(current)
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                startup = app.screen.query_one(self.StartupCommands)
                startup.highlighted = 1
                await pilot.press("delete")
                await pilot.pause()
                self.assertEqual(settings.load()["startup_commands"], ["/plan"])
        _run(scenario)

    # The placeholder row is not a real entry -- 'Del' on it does
    # nothing, not an 'IndexError'.
    def test_del_on_the_placeholder_row_is_a_no_op(self):
        async def scenario():
            from seine import settings
            app = self.SeineApp()
            async with app.run_test() as pilot:
                await self._open(pilot, app)
                await self._focus_startup(pilot, app)
                startup = app.screen.query_one(self.StartupCommands)
                startup.highlighted = 0
                await pilot.press("delete")
                await pilot.pause()
                self.assertEqual(settings.load()["startup_commands"], [])
        _run(scenario)

# The Reporter that crosses from a build's worker thread back to the UI
# thread. A fake app whose 'call_from_thread' just calls straight through
# is enough to prove *what* gets forwarded; the "really is another
# thread" part is exercised for real in 'BuildScreen'/'BuildState' below.
class ReporterForwarding(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.reporter import TextualReporter
        self.TextualReporter = TextualReporter

    class FakeApp:
        def call_from_thread(self, fn, *args):
            fn(*args)

    class FakeSink:
        def __init__(self):
            self.calls = []
        def task_started(self, name):
            self.calls.append(("started", name))
        def task_finished(self, name, failed=False):
            self.calls.append(("finished", name, failed))
        def say(self, text):
            self.calls.append(("say", text))
        def sampled(self, sample):
            self.calls.append(("sampled", sample))

    def test_each_call_is_forwarded_through_the_app(self):
        sink = self.FakeSink()
        reporter = self.TextualReporter(self.FakeApp(), sink)
        reporter.started("rootfs")
        reporter.finished("rootfs", failed=True)
        reporter.say("interrupted")
        reporter.sampled({"t": 0, "load": 1.0, "cpu": 0.5})
        self.assertEqual(sink.calls, [
            ("started", "rootfs"),
            ("finished", "rootfs", True),
            ("say", "interrupted"),
            ("sampled", {"t": 0, "load": 1.0, "cpu": 0.5}),
        ])

    # From a real background thread, not just a fake app: what matters
    # for 'App.call_from_thread' is being called off the event-loop
    # thread, which a plain function call in the test above is not.
    def test_forwarding_works_from_a_real_thread(self):
        import threading
        sink = self.FakeSink()
        reporter = self.TextualReporter(self.FakeApp(), sink)
        thread = threading.Thread(target=reporter.started, args=("packages",))
        thread.start()
        thread.join()
        self.assertEqual(sink.calls, [("started", "packages")])

class Tailing(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.build import Tail
        self.Tail = Tail

    def test_no_file_yet_is_empty_not_an_error(self):
        tail = self.Tail()
        tail.switch(os.path.join(self.workdir, "nope.log"))
        self.assertEqual(tail.read_new(), "")

    def test_reads_only_what_grew(self):
        path = os.path.join(self.workdir, "rootfs.log")
        with open(path, "w") as f:
            f.write("line one\n")
        tail = self.Tail()
        tail.switch(path)
        self.assertEqual(tail.read_new(), "line one\n")
        self.assertEqual(tail.read_new(), "")
        with open(path, "a") as f:
            f.write("line two\n")
        self.assertEqual(tail.read_new(), "line two\n")

    # A fresh 'Tail' for a widget that just remounted: it has nothing on
    # screen yet, so it needs the whole file again, not the delta since
    # whatever a *previous* widget last read.
    def test_switching_path_starts_over(self):
        first = os.path.join(self.workdir, "a.log")
        second = os.path.join(self.workdir, "b.log")
        with open(first, "w") as f:
            f.write("from a\n")
        with open(second, "w") as f:
            f.write("from b\n")
        tail = self.Tail()
        tail.switch(first)
        self.assertEqual(tail.read_new(), "from a\n")
        tail.switch(second)
        self.assertEqual(tail.read_new(), "from b\n")
        tail.switch(first)
        self.assertEqual(tail.read_new(), "from a\n")

    def test_a_truncated_file_is_read_from_the_start_again(self):
        path = os.path.join(self.workdir, "rootfs.log")
        with open(path, "w") as f:
            f.write("first attempt\n")
        tail = self.Tail()
        tail.switch(path)
        self.assertEqual(tail.read_new(), "first attempt\n")
        with open(path, "w") as f:
            f.write("retried\n")
        self.assertEqual(tail.read_new(), "retried\n")

# What the Build screen renders, kept apart from any widget -- the same
# split as 'Rendering' (render.py) above.
class BuildStateBehaviour(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.build import BuildState
        self.BuildState = BuildState
        from seine.tui.context import Context
        context = Context()
        context.use([PC_IMAGE])
        self.build = context.builds[0]

    def test_reset_lists_every_step_as_pending(self):
        state = self.BuildState()
        state.reset(self.build)
        self.assertGreater(len(state.order), 0)
        self.assertTrue(all(row["state"] == "pending"
                           for row in state.rows.values()))
        self.assertIn("○", state.render())

    def test_a_step_moves_from_running_to_done(self):
        state = self.BuildState()
        state.reset(self.build)
        name = state.order[0]
        state.task_started(name)
        self.assertEqual(state.rows[name]["state"], "running")
        self.assertEqual(state.current, name)
        self.assertIn("●", state.render())
        state.task_finished(name, failed=False)
        self.assertEqual(state.rows[name]["state"], "done")
        self.assertIsNone(state.current)
        self.assertIsNotNone(state.rows[name]["elapsed"])
        self.assertIn("✔", state.render())

    def test_a_failed_step_is_marked_failed(self):
        state = self.BuildState()
        state.reset(self.build)
        name = state.order[0]
        state.task_started(name)
        state.task_finished(name, failed=True)
        self.assertEqual(state.rows[name]["state"], "failed")
        self.assertIn("✘", state.render())

    def test_say_and_sampled_set_the_message(self):
        state = self.BuildState()
        state.reset(self.build)
        state.say("interrupted: waiting for 2 task(s)")
        self.assertEqual(state.message, "interrupted: waiting for 2 task(s)")
        state.sampled({"load": 1.23, "cpu": 0.5})
        self.assertIn("1.23", state.message)
        self.assertIn("50%", state.message)

    def test_logs_reads_off_the_image_being_built(self):
        state = self.BuildState()
        state.reset(self.build)
        self.assertIsNone(state.logs)
        self.build.image.logs = "/tmp/wherever"
        self.assertEqual(state.logs, "/tmp/wherever")

    # notify_ai is how ai.py's 'start-build' tool marks a build as its
    # own (seine/tui/ai.py's _start_ai_build sets it right after this
    # same reset() would have cleared it) -- reset() must not leave a
    # previous build's flag bleeding into the next one, whoever starts it.
    def test_notify_ai_defaults_false_and_reset_clears_it(self):
        state = self.BuildState()
        self.assertFalse(state.notify_ai)
        state.reset(self.build)
        self.assertFalse(state.notify_ai)
        state.notify_ai = True
        state.reset(self.build)
        self.assertFalse(state.notify_ai)

    def test_finished_ok_and_finished_failed(self):
        ok = self.BuildState()
        ok.reset(self.build)
        ok.finished_ok()
        self.assertTrue(ok.done)
        self.assertFalse(ok.error)

        failed = self.BuildState()
        failed.reset(self.build)
        failed.finished_failed("packages failed")
        self.assertTrue(failed.done)
        self.assertTrue(failed.error)
        self.assertEqual(failed.message, "packages failed")

    def test_nothing_reset_yet_says_so(self):
        state = self.BuildState()
        self.assertEqual(state.render(), "no steps -- '/use SPEC' first\n")
        self.assertFalse(state.running)

# 'resolve()'/'render()' are plain Python -- no guestfs, no App -- kept
# apart from the real appliance the same way 'BuildState' is kept apart
# from a real build.
class FilesystemStateBehaviour(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.filesystem import FilesystemState, resolve
        self.FilesystemState = FilesystemState
        self.resolve = resolve

    def test_resolve_absolute_replaces_the_path(self):
        self.assertEqual(self.resolve("/etc", "/var/log"), "/var/log")

    def test_resolve_a_bare_name_descends(self):
        self.assertEqual(self.resolve("/etc", "systemd"), "/etc/systemd")

    def test_resolve_dotdot_goes_up(self):
        self.assertEqual(self.resolve("/etc/systemd", ".."), "/etc")

    def test_resolve_dotdot_at_the_root_stays_at_the_root(self):
        self.assertEqual(self.resolve("/", ".."), "/")

    def test_resolve_empty_stays_put(self):
        self.assertEqual(self.resolve("/etc", ""), "/etc")

    def test_nothing_reset_yet_says_so(self):
        state = self.FilesystemState()
        self.assertIn("use", state.render())

    def test_render_marks_directories_and_symlinks(self):
        state = self.FilesystemState()
        state.build = object()  # anything not-None
        state.loaded("/etc", [("systemd", "d", 4096, None),
                              ("hostname", "r", 9, None),
                              ("link", "l", None, "/etc/hostname")])
        text = state.render()
        self.assertIn("systemd/", text)
        self.assertIn("hostname", text)
        self.assertIn("link -> /etc/hostname", text)

    def test_render_shows_an_error(self):
        state = self.FilesystemState()
        state.build = object()
        state.failed("no such file")
        self.assertIn("no such file", state.render())

    def test_on_change_fires_after_loaded_and_failed(self):
        state = self.FilesystemState()
        state.build = object()
        calls = []
        state.on_change = lambda: calls.append(True)
        state.loaded("/", [])
        state.failed("boom")
        self.assertEqual(len(calls), 2)

# One real image, read through the actual TUI wiring ('filesystem'/'cd'
# commands -> 'browse()' -> a worker thread -> 'Inspector'). Cancels
# rather than fails if the appliance cannot launch here, same as
# tests/spec/inspect.py.
class FilesystemScreenIntegration(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
            from seine.tui.filesystem import FilesystemScreen
        try:
            import guestfs
        except ImportError as e:
            self.cancel("python3-guestfs is missing: %s" % e)
        self.SeineApp = SeineApp
        self.FilesystemScreen = FilesystemScreen

        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["SEINE_BUILD_DIR"] = os.path.join(self.workdir, "build")
        self.spec = os.path.join(self.workdir, "spec.yaml")
        with open(self.spec, "w") as f:
            f.write("""
distribution:
    release: trixie
    architecture: amd64
    source: debian
    uri: http://ftp.debian.org/debian
image:
    filename: fs-test.img
    table: gpt
    partitions:
        - label: rootfs
          where: /
""")
        image = os.path.join(os.environ["SEINE_BUILD_DIR"], "deploy",
                             "trixie", "fs-test.img")
        os.makedirs(os.path.dirname(image), exist_ok=True)
        with open(image, "wb") as f:
            f.truncate(200 * 1024 * 1024)
        try:
            g = guestfs.GuestFS(python_return_dict=True)
            g.add_drive_opts(image, format="raw", readonly=False)
            g.launch()
        except RuntimeError as e:
            self.cancel("guestfs could not launch its appliance: %s" % e)
        g.part_init("/dev/sda", "gpt")
        g.part_add("/dev/sda", "primary", 2048, 200 * 1024 * 1024 // 512 - 2048)
        g.mkfs("ext4", "/dev/sda1")
        g.mount("/dev/sda1", "/")
        g.mkdir("/etc")
        g.write("/etc/hostname", b"fs-test\n")
        g.umount("/")
        g.close()

    def test_browsing_a_real_image(self):
        async def scenario():
            app = self.SeineApp(files=[self.spec])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/filesystem"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.FilesystemScreen)
                for _ in range(200):
                    if not app.fs_state.loading:
                        break
                    await asyncio.sleep(0.05)
                self.assertIsNone(app.fs_state.error)
                self.assertIn("etc", {e[0] for e in app.fs_state.entries})

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/cd etc"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(200):
                    if not app.fs_state.loading:
                        break
                    await asyncio.sleep(0.05)
                self.assertEqual(app.fs_state.path, "/etc")
                self.assertIn("hostname", {e[0] for e in app.fs_state.entries})
        _run(scenario)

# End to end through the App: a fake, fast 'Image.build' stands in for a
# real one (no podman here either), driving the same 'start_build()' the
# 'build' command calls.
class BuildScreenIntegration(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.image import Image
            from seine.tui.app import DoctorScreen, OverviewScreen, SeineApp
            from seine.tui.build import BuildScreen
        self.Image = Image
        self.DoctorScreen = DoctorScreen
        self.OverviewScreen = OverviewScreen
        self.SeineApp = SeineApp
        self.BuildScreen = BuildScreen
        self.real_build = Image.build
        self.addCleanup(setattr, Image, "build", self.real_build)
        from seine import tasks
        self.addCleanup(tasks._interrupted.clear)
        # BuildCmd.__init__ reads settings.py for its jobs default --
        # isolated the same way every other class here already is.
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_build_command_runs_to_completion(self):
        import time as clock
        from seine import tasks

        def fast_build(image, reporter=None):
            for step in tasks.ordered(image.tasks())[:3]:
                reporter.started(step.name)
                clock.sleep(0.02)
                reporter.finished(step.name, failed=False)
            reporter.say("almost done")
        self.Image.build = fast_build

        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.BuildScreen)
                for _ in range(50):
                    if not app.build_state.running:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(app.build_state.done)
                self.assertFalse(app.build_state.error)
                self.assertEqual(app.build_state.message, "build finished")
        _run(scenario)

    # A TUI /build always writes an SBOM, unlike the plain CLI's
    # --sbom-only default -- ai.py's tools have nothing to read otherwise.
    def test_build_defaults_to_writing_an_sbom(self):
        def fast_build(image, reporter=None):
            for step in tasks.ordered(image.tasks())[:1]:
                reporter.started(step.name)
                reporter.finished(step.name, failed=False)
        from seine import tasks
        self.Image.build = fast_build

        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                self.assertFalse(app.context.builds[0].options["sbom"])
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(app.context.builds[0].options["sbom"])
        _run(scenario)

    def test_a_failure_is_shown_not_raised(self):
        import time as clock
        from seine import tasks

        def failing_build(image, reporter=None):
            steps = tasks.ordered(image.tasks())
            reporter.started(steps[0].name)
            reporter.finished(steps[0].name, failed=False)
            reporter.started(steps[1].name)
            reporter.finished(steps[1].name, failed=True)
            raise tasks.Failed([(steps[1].name, RuntimeError("boom"))],
                               [s.name for s in steps[2:]])
        self.Image.build = failing_build

        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(50):
                    if not app.build_state.running:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(app.build_state.done)
                self.assertTrue(app.build_state.error)
                self.assertIn("boom", app.build_state.message)
        _run(scenario)

    # Gated on a real threading.Event rather than sleeps -- a loaded
    # machine made a sleep-timed version of this test flaky under load.
    def test_navigating_away_does_not_cancel_the_build(self):
        import threading
        from seine import tasks

        proceed = threading.Event()

        def gated_build(image, reporter=None):
            for step in tasks.ordered(image.tasks()):
                reporter.started(step.name)
                proceed.wait()
                proceed.clear()
                reporter.finished(step.name, failed=False)
        self.Image.build = gated_build

        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(200):
                    if app.build_state.current is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(app.build_state.current)
                self.assertTrue(app.build_state.running)

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/overview"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.OverviewScreen)
                # Still blocked mid-step: nothing about leaving the
                # screen touched the worker.
                self.assertTrue(app.build_state.running)

                # 'build' again just goes to look at it -- not an error.
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.BuildScreen)
                status = app.screen.query_one("#status")
                self.assertNotIn("already running",
                                 _content(status))

                # Let every remaining step through, then let it finish.
                for _ in range(200):
                    if not app.build_state.running:
                        break
                    proceed.set()
                    await asyncio.sleep(0.01)
                self.assertTrue(app.build_state.done)
        _run(scenario)

    # BaseScreen's own tick, not only BuildScreen's -- gated the same way
    # as the test above, since this has to check a step genuinely still
    # running, not whenever a sleep happens to have elapsed.
    def test_spectree_highlights_the_running_step_on_any_screen(self):
        import threading
        from seine import tasks
        from seine.tui.spectree import SpecTree

        proceed = threading.Event()

        def gated_build(image, reporter=None):
            for step in tasks.ordered(image.tasks()):
                reporter.started(step.name)
                proceed.wait()
                proceed.clear()
                reporter.finished(step.name, failed=False)
        self.Image.build = gated_build

        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                # In a finally: run_test()'s teardown joins the worker
                # thread, and a failed assertion would otherwise leave it
                # blocked on proceed.wait() forever.
                try:
                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/build"
                    await pilot.press("enter")
                    await pilot.pause()
                    for _ in range(200):
                        if app.build_state.current is not None:
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(app.build_state.running)

                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/overview"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, self.OverviewScreen)
                    tree = app.screen.query_one(SpecTree)
                    for _ in range(100):
                        if tree.active_keys():
                            break
                        await asyncio.sleep(0.02)
                        await pilot.pause()
                    self.assertTrue(tree.active_keys())
                finally:
                    for _ in range(200):
                        if not app.build_state.running:
                            break
                        proceed.set()
                        await asyncio.sleep(0.01)
                self.assertTrue(app.build_state.done)
        _run(scenario)

    # Gated the same way, and for the same reason: cancelling has to land
    # while a step is deliberately still running, not whenever a sleep
    # happens to have elapsed.
    def test_cancel_calls_tasks_interrupt(self):
        import threading
        from seine import tasks

        proceed = threading.Event()

        def gated_build(image, reporter=None):
            steps = tasks.ordered(image.tasks())
            for step in steps:
                if tasks._interrupted.is_set():
                    raise tasks.Interrupted(
                        [s.name for s in steps if s.name != step.name])
                reporter.started(step.name)
                proceed.wait()
                proceed.clear()
                reporter.finished(step.name, failed=False)
        self.Image.build = gated_build

        async def scenario():
            app = self.SeineApp(files=[PC_IMAGE])
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                for _ in range(200):
                    if app.build_state.current is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(app.build_state.current)

                prompt = app.screen.query_one("#prompt")
                prompt.value = "/cancel"
                await pilot.press("enter")
                await pilot.pause()
                # 'interrupt()' only stops *new* steps starting -- the one
                # already blocked has to be let through before the build
                # can see the interrupt and stop for real.
                proceed.set()

                for _ in range(200):
                    if not app.build_state.running:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(app.build_state.done)
                self.assertTrue(app.build_state.error)
                self.assertIn("interrupted", app.build_state.message)

                # And a second 'cancel' with nothing running is refused.
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/cancel"
                await pilot.press("enter")
                await pilot.pause()
                status = app.screen.query_one("#status")
                self.assertIn("no build is running",
                              _content(status))
        _run(scenario)

    # 'build' with no active context at all is refused before touching
    # anything -- not a stack trace, not a half-started worker.
    # 'SeineApp()' with no files: opens on Doctor, not Overview -- '/build'
    # still fails the same way, on whichever screen was already open.
    def test_build_with_no_active_spec_is_refused(self):
        async def scenario():
            app = self.SeineApp()
            async with app.run_test() as pilot:
                prompt = app.screen.query_one("#prompt")
                prompt.value = "/build"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, self.DoctorScreen)
                status = app.screen.query_one("#status")
                self.assertIn("no active specification",
                              _content(status))
        _run(scenario)

# A real traceback, its frame identity fabricated via CodeType.replace()
# (co_name/co_filename are otherwise read-only) -- proves the check
# without needing a live MarkdownFence race to trigger it
# (Textualize/textual#5525, closed without merging).
def _traceback_from(co_name, co_filename):
    def _inner():
        raise RuntimeError("boom")
    code = _inner.__code__.replace(co_name=co_name, co_filename=co_filename)
    fn = types.FunctionType(code, globals())
    try:
        fn()
    except RuntimeError as e:
        return e.__traceback__

class MarkdownRethemeRaceDetection(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import _is_markdown_retheme_race
        self._is_markdown_retheme_race = _is_markdown_retheme_race

    def test_matches_the_known_race(self):
        from textual.css.query import NoMatches
        error = NoMatches("boom")
        error.__traceback__ = _traceback_from(
            "_retheme", "/x/textual/widgets/_markdown.py")
        self.assertTrue(self._is_markdown_retheme_race(error))

    def test_a_different_function_in_the_same_file_does_not_match(self):
        from textual.css.query import NoMatches
        error = NoMatches("boom")
        error.__traceback__ = _traceback_from(
            "_on_mount", "/x/textual/widgets/_markdown.py")
        self.assertFalse(self._is_markdown_retheme_race(error))

    def test_retheme_in_a_different_file_does_not_match(self):
        from textual.css.query import NoMatches
        error = NoMatches("boom")
        error.__traceback__ = _traceback_from("_retheme", "/x/seine/tui/chat.py")
        self.assertFalse(self._is_markdown_retheme_race(error))

    # Only NoMatches is ever swallowed -- some other exception raised
    # from the very same frame is still a real bug, not this race.
    def test_a_different_exception_type_does_not_match(self):
        error = RuntimeError("boom")
        error.__traceback__ = _traceback_from(
            "_retheme", "/x/textual/widgets/_markdown.py")
        self.assertFalse(self._is_markdown_retheme_race(error))

class MarkdownRethemeRaceIsSwallowed(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.app import SeineApp
        self.SeineApp = SeineApp

    def test_the_known_race_does_not_reach_apps_own_handler(self):
        from textual.app import App
        from textual.css.query import NoMatches
        app = self.SeineApp()
        called = []
        real = App._handle_exception
        App._handle_exception = lambda self, error: called.append(error)
        try:
            error = NoMatches("boom")
            error.__traceback__ = _traceback_from(
                "_retheme", "/x/textual/widgets/_markdown.py")
            app._handle_exception(error)
        finally:
            App._handle_exception = real
        self.assertEqual(called, [])

    def test_any_other_exception_still_reaches_apps_own_handler(self):
        from textual.app import App
        app = self.SeineApp()
        called = []
        real = App._handle_exception
        App._handle_exception = lambda self, error: called.append(error)
        try:
            error = ValueError("a real bug")
            app._handle_exception(error)
        finally:
            App._handle_exception = real
        self.assertEqual(called, [error])

if __name__ == "__main__":
    avocado.main()
