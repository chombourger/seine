#!/usr/bin/python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import sys
from seine.build import BuildCmd
from seine.cache import CacheCmd

# What seine can be asked to do. Each command says the rest for itself, with
# '-h' -- there is no point restating a command's flags here, where they
# would go out of date the day one is added.
COMMANDS = {
    "build": (BuildCmd, "build an image from one or more specification files"),
    "cache": (CacheCmd, "show what seine has cached, remove it, or move it"),
}

USAGE = """
Build Embedded Linux images from a specification

Usage:
  seine COMMAND [options] [arguments]

Commands:
%s

Run 'seine COMMAND --help' for what a command takes.
""" % "\n".join("  %-9s %s" % (name, what) for name, (_, what) in COMMANDS.items())

def main():
    argv = sys.argv[1:]

    if len(argv) > 0 and argv[0] in ["-h", "--help"]:
        print(USAGE)
        sys.exit()

    if len(argv) == 0:
        sys.stderr.write("error: no command given%s" % USAGE)
        sys.exit(1)

    command = COMMANDS.get(argv[0])
    if command is None:
        sys.stderr.write("error: '%s' is not a seine command%s" % (argv[0], USAGE))
        sys.exit(1)
    command[0]().main(argv[1:])

if __name__ == "__main__":
    main()
