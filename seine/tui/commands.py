# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# One command registry, read by the prompt, Tab completion and the
# command palette alike -- nothing about a command is special-cased in
# more than one of them.

import getopt
import inspect
import shlex
from typing import NamedTuple

from seine import tasks
from seine.build import BuildCmd
from seine.tui.context import Context

class CommandError(Exception):
    pass

class Command(NamedTuple):
    name: str
    run: object       # (app, argv: list[str]) -> None
    args: str = ""
    help: str = ""
    # Longer text for /help's Commands tab, from run.__doc__ via _doc()
    # below -- falls back to 'help' alone when empty.
    detail: str = ""
    # (flags, description) pairs for /help's OPTIONS section -- getopt
    # et al aren't self-describing, so written out once per command.
    options: tuple = ()

# Splits a docstring into a short /help list line and an optional
# detail paragraph, separated by a blank line.
def _doc(func):
    doc = inspect.cleandoc(func.__doc__ or "")
    if not doc:
        return "", ""
    head, _, rest = doc.partition("\n\n")
    return head.strip(), rest.strip()

def _use(app, argv):
    """set the active specification

    Sets the active specification -- one or more YAML files merged in
    order, and (optionally) split into separate build groups with '--',
    the same grouping 'seine build' takes on the real command line. Every
    other screen reads whatever '/use' last set.
    """
    if len(argv) == 0:
        raise CommandError("/use expects one or more specification files")
    try:
        app.context.use(argv)
    except (OSError, ValueError) as e:
        raise CommandError(str(e))
    app.say("using %s" % app.context.label())
    app.refresh_screens()

def _side_load(app, argv):
    """load one more fragment on top of the active spec, highlighting what it changed

    Loads one more file on top of the active specification and re-parses
    it, highlighting on the spec tree what that one fragment changed.
    Needs a single active group -- not '/use a -- b'.
    """
    if len(argv) != 1:
        raise CommandError("/side-load expects exactly one fragment file")
    try:
        app.context.side_load(argv[0])
    except (OSError, ValueError) as e:
        raise CommandError(str(e))
    app.say("side-loaded %s" % argv[0])
    app.refresh_screens()

def _side_unload(app, argv):
    """drop one side-loaded fragment back out, highlighting what it reverted

    The reverse of /side-load: drops one file back out of the active
    specification's file list and re-parses without it, highlighting on
    the spec tree what that reverted. Needs a single active group.
    """
    if len(argv) != 1:
        raise CommandError("/side-unload expects exactly one fragment file")
    try:
        app.context.side_unload(argv[0])
    except (OSError, ValueError) as e:
        raise CommandError(str(e))
    app.say("side-unloaded %s" % argv[0])
    app.refresh_screens()

def _validate(app, argv):
    """check a specification loads and parses, without using it

    Loads and parses one or more specification files and says whether
    they are valid -- no build, no container, no image touched. Useful to
    check a fragment compiles before wiring it into a real build.
    """
    if len(argv) == 0:
        raise CommandError("/validate expects one or more specification files")
    probe = Context()
    try:
        probe.use(argv)
    except (OSError, ValueError) as e:
        raise CommandError(str(e))
    app.say("%s: valid (%d group%s)"
            % (probe.label(), len(probe.builds),
               "" if len(probe.builds) == 1 else "s"))

# Shared by every command below that just switches to a screen over the
# active spec, optionally naming one first -- the same '/use' + active
# check either way, only the screen name differs. 'require_image' is for
# the one screen (plan, so far) that needs the active spec's own
# 'image:' section -- checked before switching, so a vendor-only spec
# never lands on a screen that would have nothing to show.
def _show_over_active_spec(app, argv, screen, require_image=False):
    if len(argv) > 0:
        _use(app, argv)
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    if require_image:
        _require_image(app)
    app.show(screen)

# Shared by '/plan' and '/build': both end up calling into Image, which
# needs the active spec's own 'image:' section (BuildCmd.parse() skips
# parsing one at all when it is missing -- see its own comment). A
# vendor-only specification is legitimately active in the TUI (vendor
# screen, vendor-graph/vendor-why) without ever having one.
def _require_image(app):
    if any("image" not in build.spec for build in app.context.builds):
        raise CommandError(
            "no 'image:' section in the active specification -- nothing "
            "to build or plan")

def _plan(app, argv):
    """say what a build would do, without doing any of it

    Shows what a build would actually do without doing any of it: the
    merged specification, diffed against the last real build of these
    files. Given SPEC arguments, first runs '/use SPEC...'.
    """
    _show_over_active_spec(app, argv, "plan", require_image=True)

def _overview(app, argv):
    """back to the overview screen"""
    app.show("overview")

def _artifacts(app, argv):
    """what the last build wrote under the deploy directory"""
    _show_over_active_spec(app, argv, "artifacts")

def _packages(app, argv):
    """rebuilt-from-source packages, and what the last SBOM found"""
    _show_over_active_spec(app, argv, "packages")

# Only -j/--jobs= today, not the whole of BuildCmd.LONG_OPTIONS -- the
# rest either means something else here, can't work the same way
# ('--help' calls sys.exit()), or isn't wired up yet. An unrecognised
# flag raises CommandError rather than silently becoming a filename.
def _build(app, argv):
    """build the active specification

    Builds the active specification. Typing '/build' again while one is
    already running just switches to watching it, rather than starting a
    second one. Needs exactly one active group -- multi-group
    specifications ('/use a -- b') aren't driven from here yet.
    """
    try:
        opts, args = getopt.getopt(argv, "j:", ["jobs="])
    except getopt.GetoptError as e:
        raise CommandError(str(e))
    jobs = None
    for o, a in opts:
        # Same validation 'BuildCmd.main()' applies to '-j'/'--jobs' on
        # the real CLI (seine/build.py) -- a value it would reject is
        # not one the TUI should quietly accept either.
        try:
            jobs = int(a)
        except ValueError:
            raise CommandError("--jobs expects a number")
        if jobs < 1:
            raise CommandError("--jobs shall be at least 1")
    if len(args) > 0:
        _use(app, args)
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    # A build already running: 'build' just goes to look at it -- typing
    # the same command again to check progress is not "start a second
    # one", and must not be refused as if it were.
    if app.build_state.running:
        app.show("build")
        return
    # seine.tasks.run() keeps its own progress in module-level globals
    # (interrupted/running/display), never designed for two independent
    # job graphs at once -- see start_vendor()'s own comment.
    if app.vendor_state.running:
        raise CommandError("a vendor is running -- wait for it to finish first")
    _require_image(app)
    if len(app.context.builds) != 1:
        raise CommandError(
            "build needs exactly one active group -- multi-group builds "
            "('/use a -- b') aren't driven from the TUI yet")
    build = app.context.builds[0]
    if jobs is not None:
        build.options["jobs"] = jobs
    # TUI builds always write an SBOM, unlike the plain CLI's --sbom-only
    # default -- ai.py's packages/installed-packages tools need one to read.
    build.options["sbom"] = True
    # Imported here, not at module level: breaks a real import cycle
    # (seine.tui.build -> seine.tui.base -> this module).
    from seine.tui.build import start_build
    try:
        start_build(app, app.build_state, build)
    except RuntimeError as e:
        raise CommandError(str(e))
    app.show("build")

# '/help' 's own OPTIONS section for '/build' -- see 'Command.options'.
_build_options = (
    ("-j N, --jobs=N", "Override the parallel job count for this run only."),
)

def _vendor(app, argv):
    """resolve and fetch the active specification's own 'vendor:' section

    Runs 'seine vendor' against the active specification: every source
    package its 'vendor:' section names, and its full build-dependency
    closure, fetched into a signed apt repository of its own -- one per
    suite. Given SPEC arguments, first runs '/use SPEC...'. Works on a
    specification with no 'image:' section too, the same as the plain
    CLI (vendoring is independent of building) -- unlike '/build', which
    needs one. '--suite'/'--refresh' aren't driven from here yet; use
    'seine vendor' directly for those.
    """
    if len(argv) > 0:
        _use(app, argv)
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    # Same "typing it again just looks" shortcut '/build' gives a
    # running build.
    if app.vendor_state.running:
        app.show("vendor")
        return
    if app.build_state.running:
        raise CommandError("a build is running -- wait for it to finish first")
    if len(app.context.builds) != 1:
        raise CommandError(
            "vendor needs exactly one active group -- multi-group builds "
            "('/use a -- b') aren't driven from the TUI yet")
    build = app.context.builds[0]
    # Imported here, not at module level: same import-cycle reason
    # '/build' imports start_build() from seine.tui.build locally.
    from seine.tui.vendor import prepare, start_vendor
    try:
        distro, entries, exclude, wanted, extra_archs = prepare(build)
    except ValueError as e:
        raise CommandError(str(e))
    try:
        start_vendor(app, app.vendor_state, distro, entries, exclude, wanted,
                     extra_archs=extra_archs)
    except RuntimeError as e:
        raise CommandError(str(e))
    app.show("vendor")

def _filesystem(app, argv):
    """browse a finished image, read-only

    Opens a read-only browser of the *last built* image for the active
    specification -- not the specification itself. Needs an image that
    has actually been built.
    """
    if len(argv) > 0:
        _use(app, argv)
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    if len(app.context.builds) != 1:
        raise CommandError(
            "filesystem needs exactly one active group -- '/use' a single spec")
    from seine.tui.filesystem import browse
    app.fs_state.reset(app.context.builds[0])
    app.show("filesystem")
    browse(app, app.fs_state, "/")

def _cd(app, argv):
    """change directory in the filesystem browser

    Changes directory inside the filesystem browser ('/filesystem'
    first). A bare '/cd' goes to '/'; '/cd ..' goes up. The same listing
    is also reachable with the keyboard -- Tab to it, arrows to move,
    Enter to open.
    """
    if app.fs_state.build is None:
        raise CommandError("open the filesystem first -- '/filesystem'")
    from seine.tui.filesystem import browse, resolve
    target = resolve(app.fs_state.path, argv[0] if argv else "/")
    app.show("filesystem")
    browse(app, app.fs_state, target)

def _analyze(app, argv):
    """where the time went in the last recorded build"""
    _show_over_active_spec(app, argv, "analyze")

def _cache(app, argv):
    """what seine has cached, and how much of it"""
    app.show("cache")

def _doctor(app, argv):
    """say whether this machine has what a build needs"""
    app.show("doctor")

def _chat(app, argv):
    """reopen the AI chat transcript

    Reopens the chat screen -- typing a question straight into any
    other screen's prompt already goes here automatically once
    'llm_model' is set ('/settings'); this is only for getting back to
    an existing transcript without asking anything new.
    """
    app.show("chat")

def _diff(app, argv):
    """diff two SBOMs, package by package

    Diffs two SBOMs package by package -- what was added and removed
    between them. A specification diff (not an SBOM diff) is already on
    the Plan screen ('/plan').
    """
    if len(argv) != 2:
        raise CommandError(
            "diff needs exactly two SBOM files: '/diff OLD.spdx.json NEW.spdx.json' "
            "-- a specification diff is already on the Plan screen ('/plan')")
    from seine.sbom_diff import diff_files
    try:
        app.diff_text = diff_files(argv[0], argv[1])
    except OSError as e:
        raise CommandError("couldn't open SBOM file: %s" % e)
    except ValueError as e:
        raise CommandError(str(e))
    app.show("diff")

def _issues(app, argv):
    """scan the active build's own SBOM for known CVEs

    Scans the active build's own SBOM (every TUI '/build' writes one; a
    plain 'seine build --sbom' also does) against Debian's own security
    tracker, or a configured 'sbom2cve_program' ('/set sbom2cve_program').
    '--filter=PKG' narrows to a package (a regex, case-insensitive);
    '--min-urgency=LEVEL' drops anything less severe than LEVEL (high,
    medium, low, unimportant, end-of-life, not-yet-assigned -- the
    default: everything); '--rescan' ignores a cached scan and runs a
    fresh one.
    """
    try:
        opts, args = getopt.getopt(argv, "", ["filter=", "min-urgency=", "rescan"])
    except getopt.GetoptError as e:
        raise CommandError(str(e))
    if args:
        raise CommandError("/issues takes no positional arguments -- "
                           "'--filter=PKG'/'--min-urgency=LEVEL'/'--rescan' only")
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    package = min_urgency = None
    rescan = False
    for o, a in opts:
        if o == "--filter":
            package = a
        elif o == "--min-urgency":
            min_urgency = a
        elif o == "--rescan":
            rescan = True
    app.issues_filter = package
    app.issues_min_urgency = min_urgency
    app.issues_rescan = rescan
    app.show("issues")

def _settings(app, argv):
    """open the settings screen"""
    from seine.tui.settings import SettingsScreen
    app.push_screen(SettingsScreen())

# Textual's own named themes (gruvbox, nord, ...) aren't seine's
# vocabulary -- dark/light is. settings.json always stores one of these
# two keys, never a Textual name.
THEMES = {"dark": "textual-dark", "light": "textual-light"}

# jobs/theme/sbom2cve_program/history_pruning only -- startup_commands
# is edited from /settings itself.
def _set(app, argv):
    """change one persisted setting: jobs, theme, sbom2cve_program, or history_pruning

    Changes one persisted setting and saves it straight away -- 'jobs'
    (an int >= 1, the default '/build'/'seine build' falls back to when
    no '--jobs' is given), 'theme' ('dark' or 'light', applied
    immediately, not just on the next startup), 'sbom2cve_program'
    (a program run as 'PROGRAM SBOM_PATH' by '/issues' and 'seine
    issues' in place of debsbom's own container, expected to write the
    same JSON-lines shape 'debsbom sec-scan -f json' does), or
    'history_pruning' (how long a prompt history line is kept -- 'Nd'
    for N days, or '0' to keep it forever; defaults to 30 days when
    never set).
    'startup_commands' is edited from '/settings' itself, not here.
    """
    if len(argv) != 2:
        raise CommandError("/set expects a key and a value: '/set jobs 4'")
    key, value = argv
    from seine import settings
    current = settings.load()
    if key == "jobs":
        try:
            jobs = int(value)
        except ValueError:
            raise CommandError("jobs expects a number")
        if jobs < 1:
            raise CommandError("jobs shall be at least 1")
        current["jobs"] = jobs
    elif key == "theme":
        if value not in THEMES:
            raise CommandError("theme is 'dark' or 'light', not '%s'" % value)
        current["theme"] = value
        app.theme = THEMES[value]
    elif key == "sbom2cve_program":
        current["sbom2cve_program"] = value
    elif key == "history_pruning":
        from seine.tui.history import parse_prune_after
        try:
            parse_prune_after(value)
        except ValueError as e:
            raise CommandError(str(e))
        current["history_pruning"] = value
    else:
        raise CommandError(
            "unknown setting '%s' -- jobs, theme, sbom2cve_program, or "
            "history_pruning" % key)
    settings.save(current)
    app.say("%s = %s" % (key, value))

def _cancel(app, argv):
    """stop a running build or vendor (same as Ctrl-C)"""
    # Whichever it is, there is only ever one seine.tasks.run() in
    # flight at a time (start_vendor()'s own comment) -- interrupt() is
    # the same global stop either way, so this need not ask which.
    if not app.build_state.running and not app.vendor_state.running:
        raise CommandError("no build or vendor is running")
    tasks.interrupt()
    app.say("cancelling -- waiting for running steps to finish")

def _target_status(app):
    from seine.tui import target
    try:
        state = target.status(app)
    except Exception as e:
        raise CommandError(str(e))
    app.say("power: %s -- uptime: %ss -- storage: %s -- usb: %s"
            % (state["power"], state["uptime"], state["storage"], state["usb"]))

def _target_console(app, argv):
    from seine.tui import target
    if not argv:
        raise CommandError("/target console needs send|run|dump|head|tail|wait")
    verb, rest = argv[0], argv[1:]
    if verb == "send":
        if len(rest) != 1:
            raise CommandError("/target console send needs STRING")
        data = rest[0]
        target.run_and_report(app, "target console send", lambda: target.console_send(app, data))
    elif verb == "run":
        if len(rest) != 1:
            raise CommandError("/target console run needs COMMAND")
        cmd = rest[0]
        target.run_and_report(app, "target console run", lambda: target.console_run(app, cmd))
    elif verb in ("dump", "head", "tail"):
        try:
            text = getattr(target, "console_" + verb)(app)
        except Exception as e:
            raise CommandError(str(e))
        app.say(text or "(console buffer is empty)")
    elif verb == "wait":
        if not rest:
            raise CommandError("/target console wait needs a STRING to wait for")
        what = rest[0]
        timeout = float(rest[1]) if len(rest) > 1 else None
        try:
            text = target.console_wait(app, what, timeout=timeout)
        except Exception as e:
            raise CommandError(str(e))
        app.say(text or "(timed out waiting for '%s')" % what)
    else:
        raise CommandError("unknown '/target console %s' -- '/help' lists them" % verb)

def _target(app, argv):
    """switch to the Remote Target screen, or drive one directly

    Power, storage, USB and console control for a physical target, over
    mtda (github.com/siemens/mtda). No confirmation on any of these --
    typing the command is the confirmation; only the AI's own tool
    calls for the same actions are gated. A bare '/target' just
    switches to the screen, with no automatic connect (unless one is
    already live, or $MTDA_REMOTE names a default agent) -- 'connect'
    below is how to actually reach one. Everything here also works
    typed from anywhere else. Nothing here works without mtda and pyte
    installed -- run '/doctor' to check.
    """
    from seine.tui import target
    if not target.available():
        raise CommandError("mtda or pyte not installed -- '/target' is disabled (run '/doctor' to check)")

    if not argv:
        app.show("target")
        return
    verb, rest = argv[0], argv[1:]

    if verb == "connect":
        if len(rest) > 1:
            raise CommandError("/target connect takes at most one AGENT")
        agent = rest[0] if rest else None
        app.show("target")
        app.screen.connect(agent, force=True)
    elif verb == "disconnect":
        if rest:
            raise CommandError("/target disconnect takes no arguments")
        target.disconnect(app)
        app.say("target: disconnected")
        if hasattr(app.screen, "_redraw_status"):
            app.screen._redraw_status()
    elif verb == "status":
        _target_status(app)
    elif verb in ("on", "off", "toggle"):
        target.run_and_report(app, "target %s" % verb, lambda: target.power(app, verb))
    elif verb == "usb":
        if len(rest) != 2 or rest[1] not in ("on", "off", "toggle"):
            raise CommandError("/target usb needs PORT on|off|toggle")
        port, state = rest
        target.run_and_report(app, "target usb %s" % state,
                              lambda: target.usb(app, port, state))
    elif verb == "storage":
        if len(rest) != 1 or rest[0] not in ("host", "target"):
            raise CommandError("/target storage needs host|target")
        where = rest[0]
        fn = target.storage_to_host if where == "host" else target.storage_to_target
        target.run_and_report(app, "target storage %s" % where, lambda: fn(app))
    elif verb == "write":
        if len(rest) != 1:
            raise CommandError("/target write needs IMAGE")
        path = rest[0]
        target.run_and_report(app, "target write", lambda: target.write_image(app, path))
    elif verb == "snapshot":
        target.run_and_report(app, "target snapshot", lambda: target.snapshot(app))
    elif verb == "rollback":
        target.run_and_report(app, "target rollback", lambda: target.rollback(app))
    elif verb == "console":
        _target_console(app, rest)
    else:
        raise CommandError("unknown /target verb '%s' -- '/help' lists them" % verb)

def _test(app, argv):
    """run the active specification's own tests against a real target

    Runs the active specification's own 'test:' section (see
    docs/testing.md) -- a specification carries its tests the same way
    it carries its packages/playbook/image, so there is nothing else to
    point this at. '--tags=TAG,...' runs only tests carrying at least
    one of them. Typing '/test' again while one is already running just
    switches to watching it. Cancelling a running test isn't wired up
    yet -- '/cancel' only stops a build.
    """
    from seine.testing import available
    if not available():
        raise CommandError(
            "robotframework is not installed -- 'seine test' is disabled "
            "(pip install seine[test], or the seine-test package)")
    try:
        opts, args = getopt.getopt(argv, "", ["tags="])
    except getopt.GetoptError as e:
        raise CommandError(str(e))
    tags = []
    for o, a in opts:
        if o == "--tags":
            tags += [t.strip() for t in a.split(",") if t.strip()]
    if len(args) > 0:
        _use(app, args)
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    if app.test_state.running:
        app.show("test")
        return
    if len(app.context.builds) != 1:
        raise CommandError(
            "test needs exactly one active group -- multi-group specs "
            "('/use a -- b') aren't driven from the TUI yet")
    build = app.context.builds[0]

    from seine.tui.testing import start_test
    try:
        start_test(app, app.test_state, build.loaded_files, spec=build.spec,
                  tags=tags or None)
    except RuntimeError as e:
        raise CommandError(str(e))
    app.show("test")

def _quit(app, argv):
    """leave the TUI"""
    app.exit()

def _help(app, argv):
    """keyboard shortcuts and every command"""
    # Imported here: seine.tui.help reads commands.REGISTRY, a cycle
    # broken the same way as in _build()/_filesystem().
    from seine.tui.help import HelpScreen
    app.push_screen(HelpScreen())

# 'name', 'run', 'args' only -- 'help'/'detail' are never typed out here,
# they come from each function's own docstring via '_doc()', right above
# its own implementation, not this far away from it.
REGISTRY = {
    c.name: c for c in [
        Command("use",      _use,      "SPEC... [-- SPEC...]...",  *_doc(_use)),
        Command("side-load",   _side_load,   "FRAGMENT.yaml",      *_doc(_side_load)),
        Command("side-unload", _side_unload, "FRAGMENT.yaml",      *_doc(_side_unload)),
        Command("plan",     _plan,     "[SPEC...]",                *_doc(_plan)),
        Command("validate", _validate, "SPEC...",                  *_doc(_validate)),
        Command("overview", _overview, "",                         *_doc(_overview)),
        Command("artifacts", _artifacts, "[SPEC...]",               *_doc(_artifacts)),
        Command("packages", _packages, "[SPEC...]",                *_doc(_packages)),
        Command("build",    _build,    "[--jobs=N] [SPEC...]",     *_doc(_build),
                options=_build_options),
        Command("vendor",   _vendor,   "[SPEC...]",                *_doc(_vendor)),
        Command("filesystem", _filesystem, "[SPEC...]",             *_doc(_filesystem)),
        Command("cd",       _cd,       "[PATH]",                   *_doc(_cd)),
        Command("cancel",   _cancel,   "",                         *_doc(_cancel)),
        Command("target",   _target,
                "[connect [AGENT]|disconnect|on|off|toggle|usb PORT V|"
                "storage host|target|write IMAGE|snapshot|rollback|"
                "console ...|status]",
                *_doc(_target)),
        Command("test",      _test,      "[--tags=TAG,...] [SPEC...]",
                *_doc(_test)),
        Command("analyze",  _analyze,  "[SPEC...]",                *_doc(_analyze)),
        Command("cache",    _cache,    "",                         *_doc(_cache)),
        Command("doctor",   _doctor,   "",                         *_doc(_doctor)),
        Command("chat",     _chat,     "",                         *_doc(_chat)),
        Command("diff",     _diff,     "OLD.spdx.json NEW.spdx.json", *_doc(_diff)),
        Command("issues",   _issues,   "[--filter=PKG] [--min-urgency=LEVEL] [--rescan]",
                *_doc(_issues)),
        Command("settings", _settings, "",                         *_doc(_settings)),
        Command("set",      _set,      "KEY VALUE",                *_doc(_set)),
        Command("help",     _help,     "",                         *_doc(_help)),
        Command("quit",     _quit,     "",                         *_doc(_quit)),
    ]
}
REGISTRY["q"] = REGISTRY["quit"]

# What Tab/the palette suggest for a command's own flags. 'build' only
# offers --jobs= since that's all _build() actually parses -- suggesting
# a flag that then fails would be worse than not suggesting it.
OPTIONS = {
    "plan":  BuildCmd.LONG_OPTIONS,
    "build": ["jobs="],
    "issues": ["filter=", "min-urgency=", "rescan"],
}

# One line, split the way a shell would split it -- so a quoted path
# with a space works from the prompt the same as it would on a command
# line. Raises CommandError on an unterminated quote rather than a raw
# ValueError, so the caller has one exception type to catch.
def split(line):
    try:
        return shlex.split(line)
    except ValueError as e:
        raise CommandError(str(e))

# Dispatches one typed line. Commands start with '/' so they can never
# be confused with plain text. Unknown name or no '/' -> CommandError.
def dispatch(app, line):
    if not line.startswith("/"):
        raise CommandError(
            "commands start with '/' -- e.g. '/build' ('/help' lists them)")
    tokens = split(line[1:])
    if len(tokens) == 0:
        return
    name, argv = tokens[0], tokens[1:]
    # '@path' is the prompt's filesystem-completion marker; stripped
    # here so handlers see a plain path. History keeps the '@'.
    argv = [token[1:] if token.startswith("@") else token for token in argv]
    command = REGISTRY.get(name)
    if command is None:
        raise CommandError("unknown command '/%s' -- '/help' lists them" % name)
    command.run(app, argv)
