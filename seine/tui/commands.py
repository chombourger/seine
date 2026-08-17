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

def _extend(app, argv):
    """load one more fragment on top of the active spec, highlighting what it changed

    Loads one more file on top of the active specification and re-parses
    it, highlighting on the spec tree what that one fragment changed.
    Needs a single active group -- not '/use a -- b'.
    """
    if len(argv) != 1:
        raise CommandError("/extend expects exactly one fragment file")
    try:
        app.context.extend(argv[0])
    except (OSError, ValueError) as e:
        raise CommandError(str(e))
    app.say("extended with %s" % argv[0])
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
# check either way, only the screen name differs.
def _show_over_active_spec(app, argv, screen):
    if len(argv) > 0:
        _use(app, argv)
    if not app.context.active:
        raise CommandError("no active specification -- '/use SPEC' first")
    app.show(screen)

def _plan(app, argv):
    """say what a build would do, without doing any of it

    Shows what a build would actually do without doing any of it: the
    merged specification, diffed against the last real build of these
    files. Given SPEC arguments, first runs '/use SPEC...'.
    """
    _show_over_active_spec(app, argv, "plan")

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
    if len(app.context.builds) != 1:
        raise CommandError(
            "build needs exactly one active group -- multi-group builds "
            "('/use a -- b') aren't driven from the TUI yet")
    build = app.context.builds[0]
    if jobs is not None:
        build.options["jobs"] = jobs
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

def _settings(app, argv):
    """open the settings screen"""
    from seine.tui.settings import SettingsScreen
    app.push_screen(SettingsScreen())

# Textual's own named themes (gruvbox, nord, ...) aren't seine's
# vocabulary -- dark/light is. settings.json always stores one of these
# two keys, never a Textual name.
THEMES = {"dark": "textual-dark", "light": "textual-light"}

# jobs/theme only -- startup_commands is edited from /settings itself.
def _set(app, argv):
    """change one persisted setting: jobs or theme

    Changes one persisted setting and saves it straight away -- 'jobs'
    (an int >= 1, the default '/build'/'seine build' falls back to when
    no '--jobs' is given) or 'theme' ('dark' or 'light', applied
    immediately, not just on the next startup). 'startup_commands' is
    edited from '/settings' itself, not here.
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
    else:
        raise CommandError("unknown setting '%s' -- jobs or theme" % key)
    settings.save(current)
    app.say("%s = %s" % (key, value))

def _cancel(app, argv):
    """stop a running build (same as Ctrl-C)"""
    if not app.build_state.running:
        raise CommandError("no build is running")
    tasks.interrupt()
    app.say("cancelling -- waiting for running steps to finish")

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
        Command("extend",   _extend,   "FRAGMENT.yaml",            *_doc(_extend)),
        Command("plan",     _plan,     "[SPEC...]",                *_doc(_plan)),
        Command("validate", _validate, "SPEC...",                  *_doc(_validate)),
        Command("overview", _overview, "",                         *_doc(_overview)),
        Command("artifacts", _artifacts, "[SPEC...]",               *_doc(_artifacts)),
        Command("packages", _packages, "[SPEC...]",                *_doc(_packages)),
        Command("build",    _build,    "[--jobs=N] [SPEC...]",     *_doc(_build),
                options=_build_options),
        Command("filesystem", _filesystem, "[SPEC...]",             *_doc(_filesystem)),
        Command("cd",       _cd,       "[PATH]",                   *_doc(_cd)),
        Command("cancel",   _cancel,   "",                         *_doc(_cancel)),
        Command("analyze",  _analyze,  "[SPEC...]",                *_doc(_analyze)),
        Command("cache",    _cache,    "",                         *_doc(_cache)),
        Command("doctor",   _doctor,   "",                         *_doc(_doctor)),
        Command("diff",     _diff,     "OLD.spdx.json NEW.spdx.json", *_doc(_diff)),
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
