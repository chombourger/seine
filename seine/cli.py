#!/usr/bin/python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import os
import sys
from seine.analyze import AnalyzeCmd
from seine.build import BuildCmd, PlanCmd
from seine.cache import CacheCmd
from seine.cmd import Cmd
from seine.gists import GistCmd
from seine.progress import interactive
from seine.sources import SourceCmd

# Deliberately doesn't import seine.tui (and hence textual) at module
# level -- only once "tui" is the command actually given.
class TuiCmd(Cmd):
    NAME = "tui"
    USAGE = """
Usage:
  seine tui [SPEC...] [-- SPEC...]...

Opens the interactive TUI, optionally starting with SPEC as the active
specification -- the same grouping 'seine build' takes. Needs the 'tui'
extra (pip install seine[tui], or the seine-tui package).
"""

    def main(self, argv):
        # Answered without importing 'seine.tui': '--help' works whether
        # or not the extra is installed, same as every other command.
        if argv and argv[0] in ("-h", "--help"):
            print(self.USAGE)
            return
        try:
            from seine.tui.app import run
        except ImportError as e:
            sys.stderr.write(
                "error: the TUI needs the 'tui' extra "
                "(pip install seine[tui], or the seine-tui package): %s\n" % e)
            sys.exit(1)
        if not interactive(sys.stdout, os.environ):
            sys.stderr.write("error: 'seine tui' needs a real terminal\n")
            sys.exit(1)
        run(argv or None)

# Just the probe/load/parse 'seine build' does before touching a
# container -- the same BuildCmd.load_all()/.parse() calls, not a copy.
class ValidateCmd(Cmd):
    NAME = "validate"
    USAGE = """
Usage:
  seine validate SPEC... [-- SPEC...]...

Loads and parses one or more specification files and says whether they
are valid, without building, planning, or touching a container -- the
same probe/load/parse 'seine build' does before any of that.
"""

    def main(self, argv):
        if argv and argv[0] in ("-h", "--help"):
            print(self.USAGE)
            return
        if len(argv) == 0:
            sys.stderr.write(
                "error: validate command expects one or more specification files\n")
            sys.exit(1)
        from seine import multiconfig
        try:
            groups = multiconfig.split(argv)
            for files in groups:
                build = BuildCmd()
                build.options = dict(build.options, ansible_library=[])
                build.load_all(files)
                build.parse()
        except OSError as e:
            sys.stderr.write("error: couldn't open specification file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: specification is invalid: %s\n" % e)
            sys.exit(3)
        print("valid: %s" % " -- ".join(" ".join(files) for files in groups))

# Read-only browsing of a finished image, via seine/inspect.py.
# guestfs is already a hard dependency of core seine (unlike textual),
# so no import guarding needed.
class InspectCmd(Cmd):
    NAME = "inspect"
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "path="]
    USAGE = """
Usage:
  seine inspect [--path=PATH] SPEC...

Reads the image SPEC would build (already built, not built again) and
lists PATH inside it (default '/'), or prints its contents if PATH names
a file -- read-only, via the same libguestfs 'seine build' itself uses
to write an image.
"""

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.USAGE)
            sys.exit(1)
        path = "/"
        for o, a in opts:
            if o in ("-h", "--help"):
                print(self.USAGE)
                return
            elif o == "--path":
                path = a
        if len(args) == 0:
            sys.stderr.write("error: inspect command expects a specification file\n")
            sys.exit(1)

        build = BuildCmd()
        build.options = dict(build.options, ansible_library=[])
        try:
            build.load_all(args)
            build.parse()
        except OSError as e:
            sys.stderr.write("error: couldn't open specification file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: specification is invalid: %s\n" % e)
            sys.exit(3)

        from seine.inspect import Inspector
        try:
            with Inspector(build.raw_spec, build.image._output) as inspector:
                if inspector.is_dir(path):
                    for name, kind, size, target in inspector.ls(path):
                        if kind == "d":
                            print("%s/" % name)
                        elif kind == "l":
                            print("%s -> %s" % (name, target))
                        else:
                            print("%-40s %10d" % (name, size))
                else:
                    sys.stdout.buffer.write(inspector.cat(path))
        except ImportError as e:
            sys.stderr.write(
                "error: 'seine inspect' needs python3-guestfs: %s\n" % e)
            sys.exit(1)
        except OSError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(2)
        except RuntimeError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(4)

# Whether the machine has what a real build assumes -- 'seine/doctor.py'
# runs the checks, this only prints them.
class DoctorCmd(Cmd):
    NAME = "doctor"
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "pull", "sign-key="]
    USAGE = """
Usage:
  seine doctor [--pull] [--sign-key=KEY]

Says whether this machine has what a real build assumes: podman, crun,
passt, guestfs, kvm, a hypervisor per architecture, ansible-playbook and
its podman collection, gnupg, and free space under the build directory.
Nothing here builds anything or touches the network, unless '--pull' is
given, which also checks that the debsbom container image can be
reached (not pulled).
"""

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.USAGE)
            sys.exit(1)
        pull = False
        options = {}
        for o, a in opts:
            if o in ("-h", "--help"):
                print(self.USAGE)
                return
            elif o == "--pull":
                pull = True
            elif o == "--sign-key":
                options["sign_key"] = a

        from seine import doctor
        checks = doctor.run(options, pull=pull)
        print(doctor.render(checks))
        sys.exit(1 if doctor.errors(checks) > 0 else 0)

# What BuildCmd.changed() already computes for 'seine plan', exposed on
# its own -- no build needed in between.
class DiffCmd(Cmd):
    NAME = "diff"
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "no-color", "sbom="]
    USAGE = """
Usage:
  seine diff [--no-color] SPEC...
  seine diff --sbom=OLD.spdx.json --sbom=NEW.spdx.json

First form: the same specification diff 'seine plan' shows, against the
last build of these files, on its own -- no build needed in between.
Second form: the package list of two SBOMs (seine build --sbom), added
and removed packages between OLD and NEW. The two forms do not combine:
an image-to-image file diff needs an image retention convention seine
does not have yet.
"""

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.USAGE)
            sys.exit(1)
        color = True
        sboms = []
        for o, a in opts:
            if o in ("-h", "--help"):
                print(self.USAGE)
                return
            elif o == "--no-color":
                color = False
            elif o == "--sbom":
                sboms.append(a)

        if len(sboms) > 0:
            if len(sboms) != 2:
                sys.stderr.write("error: --sbom takes exactly two files (old, new)\n")
                sys.exit(1)
            from seine.sbom_diff import diff_files
            try:
                print(diff_files(sboms[0], sboms[1]))
            except OSError as e:
                sys.stderr.write("error: couldn't open SBOM file: %s\n" % e)
                sys.exit(2)
            except ValueError as e:
                sys.stderr.write("error: %s\n" % e)
                sys.exit(3)
            return

        if len(args) == 0:
            sys.stderr.write("error: diff command expects one or more specification "
                            "files, or two --sbom=FILE\n")
            sys.exit(1)
        build = BuildCmd()
        build.options = dict(build.options, ansible_library=[], color=color)
        try:
            build.load_all(args)
            spec = build.parse()
        except OSError as e:
            sys.stderr.write("error: couldn't open specification file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: specification is invalid: %s\n" % e)
            sys.exit(3)
        print(build.changed(args, spec))

# What seine can be asked to do. Each command says the rest for itself, with
# '-h' -- there is no point restating a command's flags here, where they
# would go out of date the day one is added.
COMMANDS = {
    "build": (BuildCmd, "build an image from one or more specification files"),
    "plan":  (PlanCmd, "say what a build would do, without doing any of it"),
    "cache": (CacheCmd, "show what seine has cached, remove it, or move it"),
    "gist":  (GistCmd, "list, show, or remove reusable spec fragments"),
    "source": (SourceCmd, "list, remove, or pull a package's source"),
    "analyze": (AnalyzeCmd, "say where the time went in a build that ran"),
    "validate": (ValidateCmd, "check a specification loads and parses, without using it"),
    "inspect": (InspectCmd, "browse a finished image, read-only"),
    "doctor": (DoctorCmd, "say whether this machine has what a build needs"),
    "diff":   (DiffCmd, "diff a specification, or two SBOMs, package by package"),
    "tui":    (TuiCmd, "open the interactive TUI (needs the 'tui' extra)"),
}

USAGE = """
Build Embedded Linux images from a specification

Usage:
  seine COMMAND [options] [arguments]

Commands:
%s

Run 'seine COMMAND --help' for what a command takes.
""" % "\n".join("  %-9s %s" % (name, what) for name, (_, what) in COMMANDS.items())

# Whether 'seine.tui' can even be imported, without importing 'textual'
# to find out: 'find_spec' only has to locate the module, not run it.
def _tui_available():
    import importlib.util
    return importlib.util.find_spec("seine.tui.app") is not None

def main():
    argv = sys.argv[1:]

    if len(argv) > 0 and argv[0] in ["-h", "--help"]:
        print(USAGE)
        sys.exit()

    if len(argv) == 0:
        # 'seine' with nothing else: the TUI if one could plausibly show up
        # on this terminal, today's exact USAGE otherwise -- so every CI
        # job, pipe or script that runs bare 'seine' today keeps doing
        # exactly what it already does.
        if (not os.environ.get("SEINE_NO_TUI")
                and interactive(sys.stdout, os.environ) and _tui_available()):
            TuiCmd().main([])
            sys.exit()
        sys.stderr.write("error: no command given%s" % USAGE)
        sys.exit(1)

    command = COMMANDS.get(argv[0])
    if command is None:
        sys.stderr.write("error: '%s' is not a seine command%s" % (argv[0], USAGE))
        sys.exit(1)
    command[0]().main(argv[1:])

if __name__ == "__main__":
    main()
