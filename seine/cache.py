# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import os
import shutil
import sys

from seine.cmd   import Cmd
from seine.utils import ContainerEngine

# What seine keeps between builds, and what a build has to do again once it
# is gone. Nothing here is needed for a build to succeed -- only to spare
# it work it has already done -- which is what makes removing any of it
# safe.
#
# Container images are not listed: they live in seine's own podman storage
# rather than under ~/.cache, and emptying that is podman's job ('podman
# --root ~/.local/share/seine system reset').
CACHES = {
    "downloads": ("packages fetched from the distribution's feeds",
                  lambda: ContainerEngine.cache("downloads")),
    "packages":  ("packages the 'packages' section built, as an apt repository",
                  lambda: ContainerEngine.cache("packages")),
    "chroots":   ("buildd chroot tarballs sbuild unpacks to build a package",
                  lambda: ContainerEngine.cache("chroots")),
    "scratch":   ("temporary files a build unpacks sources and images into",
                  ContainerEngine.scratch),
}
def size_of(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                # A build running beside us may remove a file between the
                # walk and the stat; a size is not worth failing over.
                pass
    return total

def human(count):
    for unit in ["B", "KiB", "MiB", "GiB"]:
        if count < 1024.0:
            return "%.1f %s" % (count, unit)
        count /= 1024.0
    return "%.1f TiB" % count

class CacheCmd(Cmd):
    def info(self, names):
        total = 0
        for name in names:
            path = CACHES[name][1]()
            used = size_of(path) if os.path.isdir(path) else 0
            total += used
            print("%-10s %10s  %s" % (name, human(used), path))
        print("%-10s %10s" % ("total", human(total)))
        return 0

    def clear(self, names):
        # A build running at the same time as this loses whatever it was
        # about to reuse and does the work again -- which is what the cache
        # promises -- but one that is *reading* a tarball as it goes away
        # fails. Clearing a cache is something to do between builds.
        for name in names:
            path = CACHES[name][1]()
            if os.path.isdir(path) == False:
                continue
            print("removing %s" % path)
            shutil.rmtree(path)
        return 0

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, "h", ["help"])
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, USAGE))
            sys.exit(1)
        for o, _ in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                sys.exit()

        ACTIONS = ["info", "clear"]
        if len(args) == 0:
            sys.stderr.write("error: cache command expects one of %s\n"
                             % ", ".join(ACTIONS))
            sys.exit(1)

        action, names = args[0], args[1:]
        if action not in ACTIONS:
            sys.stderr.write("error: unknown cache action '%s'\n" % action)
            sys.exit(1)

        # Naming nothing already means all of them, but 'all' is what a
        # person types when they want to say so, and an error is a poor
        # answer to being clear.
        if "all" in names:
            names = list(CACHES)

        for name in names:
            if name not in CACHES:
                sys.stderr.write("error: unknown cache '%s', expected one of %s\n"
                                 % (name, ", ".join(sorted(CACHES))))
                sys.exit(1)

        names = names if len(names) > 0 else list(CACHES)
        try:
            sys.exit(self.info(names) if action == "info" else self.clear(names))
        except OSError as e:
            sys.stderr.write("error: cache %s failed: %s\n" % (action, e))
            sys.exit(1)

USAGE = """
Show what seine has cached, or remove it

Description:
  seine keeps downloaded packages, rebuilt packages, buildd chroots and the
  scratch space a build unpacks sources into so that the next build does not
  make them again. None of it is needed for a build to succeed, so any of it
  can be removed to get the disk space back.

  Where they live can be said with SEINE_CACHE_DIR, and where a build's own
  container storage and scratch space live with SEINE_BUILD_DIR. Unset, the
  caches are under ~/.cache/seine (or XDG_CACHE_HOME) and the scratch space
  follows TMPDIR.

  Named with no cache -- or with 'all' -- both actions cover all of them.

Usage:
  seine cache info [CACHE...|all]
  seine cache clear [CACHE...|all]

Caches:
  downloads   packages fetched from the distribution's feeds
  packages    packages the 'packages' section built, as an apt repository
  chroots     buildd chroot tarballs sbuild unpacks to build a package
  scratch     temporary files a build unpacks sources and images into

Examples:
  seine cache info
  seine cache clear chroots
  seine cache clear downloads packages

Flags:
  -h, --help            print this message

"""
