#!/usr/bin/python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import sys
from seine.build import BuildCmd

def main():
    argv = sys.argv[1:]

    if len(argv) == 0:
        print("%s: error: missing command argument!" % sys.argv[0])
        sys.exit(1)

    cmd = argv[0]
    if cmd == "build":
        BuildCmd().main(argv[1:])
    else:
        print("%s: unknown command '%s'!" % (sys.argv[0], cmd))
        sys.exit(1)

if __name__ == "__main__":
    main()
