# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# 'seine test': the same load/compile/run seine.testing.runner does for
# the TUI's '/test' and the AI chat's 'run-test' tool, wired to the
# command line -- progress.Display is a Reporter the same way it is for
# 'seine build', so a suite's own tests show up the same way a build's
# own tasks do. Exit code is unambiguous for CI: 0 only if every test
# that ran passed (see run() below).

import getopt
import sys

from seine.cmd import Cmd

class TestCmd(Cmd):
    NAME = "test"
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "tags=", "outdir=", "verbose", "dry-run"]
    USAGE = """
Usage:
  seine test [--tags=TAG,...] [--outdir=DIR] [--dry-run] SPEC...

Loads SPEC the same way 'seine build' would ('requires:', '[[ ]]'
variables, several files on one command line) and runs its merged
'test:' section against a real target -- see docs/testing.md for the
step grammar. A specification carries its own tests the same way it
carries its own packages/playbook/image: 'test:' is an ordinary
section, so the same fragment that configures the root account, say,
is where a 'Log In' keyword reused by every board 'requires:'-ing it
belongs. '--tags' runs only tests carrying at least one of the given
tags. '--outdir' is where output.xml and any captured screen/image
artifacts land, default a fresh directory under the container engine's
own logs root (see seine.utils.ContainerEngine.logs_root). '--dry-run'
is Robot Framework's own dry run: every step's keyword is resolved and
its arguments checked, but no keyword body actually runs -- nothing
touches real hardware, the way a syntax/reference check on a
specification ('seine validate') does for a build. Exit status is 0
only if every test that ran passed; 1 if any failed; 2 if SPEC has no
'test:' section or could not be loaded.
"""

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.USAGE)
            sys.exit(1)
        tags = []
        outdir = None
        verbose = False
        dry_run = False
        for o, a in opts:
            if o in ("-h", "--help"):
                print(self.USAGE)
                return
            elif o == "--tags":
                tags += [t.strip() for t in a.split(",") if t.strip()]
            elif o == "--outdir":
                outdir = a
            elif o == "--verbose":
                verbose = True
            elif o == "--dry-run":
                dry_run = True

        if len(args) == 0:
            sys.stderr.write("error: 'seine test' expects one or more specification files\n")
            sys.exit(1)

        from seine.testing import available
        if not available():
            sys.stderr.write(
                "error: 'seine test' needs the 'test' extra "
                "(pip install seine[test], or the seine-test package)\n")
            sys.exit(1)

        import os
        from seine import progress
        from seine.testing import runner
        display = None if verbose else progress.Display(total=None, environment=os.environ)

        try:
            import contextlib
            with (display if display is not None else contextlib.nullcontext()):
                result = runner.run_spec(args, tags=tags or None, outdir=outdir,
                                         reporter=display, dryrun=dry_run)
        except OSError as e:
            sys.stderr.write("error: couldn't open specification file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(2)

        outdir = os.path.dirname(result.output_xml)
        print("output under %s" % outdir)
        for t in result.tests:
            mark = "PASS" if t.status == "PASS" else ("SKIP" if t.status == "SKIP" else "FAIL")
            print("  %-4s %s" % (mark, t.name))
            if t.failed and t.message:
                print("        %s" % t.message)
        print(result.summary())
        if not result.ok:
            print("see %s/console.log and %s/interactions.json for what led up to it"
                 % (outdir, outdir))
        sys.exit(0 if result.ok else 1)
