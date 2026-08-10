# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import os
import shutil
import sys
import tarfile

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

# The caches worth carrying to another machine. Scratch is not one of them:
# what is in it belongs to a build that is either running or has died, and
# neither is of use anywhere else.
PORTABLE = ["downloads", "packages", "chroots"]

def size_of(path, carried=None):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        if carried is not None:
            dirnames[:] = [d for d in dirnames
                           if carried(os.path.join(dirpath, d))]
        for name in filenames:
            if carried is not None and carried(os.path.join(dirpath, name)) == False:
                continue
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

    # One tar holding the named caches, each under its own name, so what
    # comes out of one machine goes into another without either having to
    # agree on where a cache lives. Uncompressed unless the filename asks
    # for it: what is in here is .deb and .tar.zst, already compressed.
    def export(self, names, where):
        mode = "w|gz" if where.endswith((".gz", ".tgz")) else "w|"
        stream = sys.stdout.buffer if where == "-" else None
        with tarfile.open(None if stream else where, mode, fileobj=stream) as tar:
            for name in names:
                path = CACHES[name][1]()
                if os.path.isdir(path) == False:
                    continue
                self.say("exporting %s (%s)"
                         % (name, human(size_of(path, self._carried))), where)
                tar.add(path, arcname=name, recursive=True, filter=self._exported)
        return 0

    # What is in a cache but has no business on another machine, by the end
    # of its path:
    #
    #   .lock, lock       the lock files that guard what two builds share,
    #                     seine's own and apt's own
    #   /.packages.db     apt-ftparchive's hash cache, which a build
    #                     rewrites from the .debs it finds
    #   .build            sbuild's build logs, and the symlinks naming the
    #                     latest of them. A kernel's log rivals the .debs
    #                     it came with, and a log of a build that happened
    #                     on another machine is worth none of that. A stamp
    #                     naming one is no problem: what a stamp lists is
    #                     unlinked only if it is still there.
    #   /partial          apt's in-flight downloads, which it creates 0700
    #                     as root inside the container. Not carrying them
    #                     is not an optimisation: the export cannot read
    #                     the directory at all, and stopped there before.
    #
    # What is kept beyond the .debs themselves is the Packages index and
    # the stamps. The index because a build only rewrites it when it has
    # something to add, so an import where nothing needs rebuilding would
    # otherwise leave the .debs there with apt unable to see them; the
    # stamps because they are what says the .debs are current, which is the
    # whole point of carrying them. The .changes and .buildinfo are kept
    # too -- kilobytes, and what documents how the .debs beside them were
    # made.
    NOT_CARRIED = [".lock", "/lock", "/.packages.db", ".build", "/partial"]

    def _carried(self, name):
        return any(name.endswith(suffix)
                   for suffix in CacheCmd.NOT_CARRIED) == False

    # Returning None for a directory prunes what is under it as well, which
    # is what keeps the export out of apt's unreadable 'partial'.
    def _exported(self, entry):
        return entry if self._carried(entry.name) else None

    # The other half, and the only place seine writes files it did not make
    # itself: a tar handed to it may name anything at all. Every member has
    # to be a file, a directory or a link, under a cache seine knows and can
    # carry, and has to stay inside that cache -- both where it is written
    # and, for a link, what it points at. The whole import fails on the
    # first member that does not: half of someone else's tar is not a
    # cache, and a tar reaching out of one is not worth guessing about.
    def load(self, names, where):
        stream = sys.stdin.buffer if where == "-" else None
        with tarfile.open(None if stream else where, "r|*", fileobj=stream) as tar:
            for member in tar:
                cache, _, rest = member.name.partition("/")
                if cache not in PORTABLE:
                    raise ValueError("'%s' is not a cache that can be imported!"
                                     % member.name)
                if self._inside(rest) == False:
                    raise ValueError("'%s' points outside of its cache!"
                                     % member.name)
                # sbuild leaves a symlink beside every build log, naming the
                # latest of them, so a cache holds links as well as files.
                # One is followed on the machine that imports it, so where
                # it leads is checked as closely as where it is written.
                if member.issym():
                    target = os.path.join(os.path.dirname(rest), member.linkname)
                    if self._inside(target) == False:
                        raise ValueError("'%s' leads outside of its cache!"
                                         % member.name)
                elif member.islnk():
                    linked, _, linkrest = member.linkname.partition("/")
                    if linked != cache or self._inside(linkrest) == False:
                        raise ValueError("'%s' is linked outside of its cache!"
                                         % member.name)
                    member.linkname = linkrest
                elif member.isfile() == False and member.isdir() == False:
                    raise ValueError("'%s' is neither a file, a directory nor a link!"
                                     % member.name)
                if cache not in names or rest == "":
                    continue
                member.name = rest
                tar.extract(member, path=CACHES[cache][1]())
        for name in names:
            path = CACHES[name][1]()
            if os.path.isdir(path):
                self.say("imported %s (%s)" % (name, human(size_of(path))), where)
        return 0

    # Whether a path taken from a tar stays within the cache it was found
    # in, once '..' and the rest of it are worked out. An absolute path
    # fails this too: joining one throws away what it was joined to, which
    # is exactly what makes it worth refusing.
    def _inside(self, path):
        resolved = os.path.normpath(os.path.join("cache", path))
        return resolved == "cache" or resolved.startswith("cache" + os.sep)

    # Progress goes to stderr when the tar itself is going to stdout.
    def say(self, message, where):
        print(message, file=sys.stderr if where == "-" else sys.stdout)

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

        ACTIONS = ["info", "clear", "export", "import"]
        if len(args) == 0:
            sys.stderr.write("error: cache command expects one of %s\n"
                             % ", ".join(ACTIONS))
            sys.exit(1)

        action, names = args[0], args[1:]
        if action not in ACTIONS:
            sys.stderr.write("error: unknown cache action '%s'\n" % action)
            sys.exit(1)

        # A tar to write or to read, named first so the caches after it read
        # as they do for the other two actions.
        where = None
        if action in ["export", "import"]:
            if len(names) == 0:
                sys.stderr.write("error: cache %s expects a file ('-' for a pipe)\n"
                                 % action)
                sys.exit(1)
            where, names = names[0], names[1:]

        # Naming nothing already means all of them, but 'all' is what a
        # person types when they want to say so, and an error is a poor
        # answer to being clear. For a tar that means the caches worth
        # carrying, which is not quite all of them.
        every = PORTABLE if where is not None else list(CACHES)
        if "all" in names:
            names = list(every)

        for name in names:
            if name not in CACHES:
                sys.stderr.write("error: unknown cache '%s', expected one of %s\n"
                                 % (name, ", ".join(sorted(CACHES))))
                sys.exit(1)
            if name not in every:
                sys.stderr.write("error: the %s cache cannot be %sed\n"
                                 % (name, action))
                sys.exit(1)

        names = names if len(names) > 0 else list(every)
        try:
            if action == "info":
                sys.exit(self.info(names))
            elif action == "clear":
                sys.exit(self.clear(names))
            elif action == "export":
                sys.exit(self.export(names, where))
            else:
                sys.exit(self.load(names, where))
        except (OSError, tarfile.TarError, ValueError) as e:
            sys.stderr.write("error: cache %s failed: %s\n" % (action, e))
            sys.exit(1)

USAGE = """
Show what seine has cached, remove it, or move it to another machine

Description:
  seine keeps downloaded packages, rebuilt packages, buildd chroots and the
  scratch space a build unpacks sources into so that the next build does not
  make them again. None of it is needed for a build to succeed, so any of it
  can be removed to get the disk space back.

  Where they live can be said with SEINE_CACHE_DIR, and where a build's own
  container storage and scratch space live with SEINE_BUILD_DIR. Unset, the
  caches are under ~/.cache/seine (or XDG_CACHE_HOME) and the scratch space
  follows TMPDIR.

  'export' writes the caches to a tar and 'import' reads one back, so a
  machine that has never built anything can start with the caches of one
  that has. The tar is uncompressed unless its name ends in .gz or .tgz;
  what is in it is compressed already. '-' stands for stdout or stdin, so
  the two can be piped into each other over ssh.

  Named with no cache -- or with 'all' -- an action covers every cache it
  applies to. That is all of them for 'info' and 'clear'; for a tar it is
  the caches worth carrying, which excludes 'scratch'.

Usage:
  seine cache info [CACHE...|all]
  seine cache clear [CACHE...|all]
  seine cache export FILE|- [CACHE...|all]
  seine cache import FILE|- [CACHE...|all]

Caches:
  downloads   packages fetched from the distribution's feeds
  packages    packages the 'packages' section built, as an apt repository
  chroots     buildd chroot tarballs sbuild unpacks to build a package
  scratch     temporary files a build unpacks sources and images into

Examples:
  seine cache info
  seine cache clear chroots
  seine cache clear downloads packages
  seine cache export caches.tar
  seine cache import caches.tar chroots
  seine cache export - | ssh builder seine cache import -

Flags:
  -h, --help            print this message

"""
