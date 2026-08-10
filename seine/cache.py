# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import gzip
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

from seine.cmd   import Cmd
from seine.utils import ContainerEngine
from seine.utils import KIND_LABEL
from seine.utils import BUILDER_KIND
from seine.utils import IMAGER_KIND
from seine.utils import TRANSPORT_KIND
from seine.utils import ROOTFS_KIND
from seine.utils import TOOLING_KIND

# What seine keeps between builds, and what a build has to do again once it
# is gone. Nothing here is needed for a build to succeed -- only to spare
# it work it has already done -- which is what makes removing any of it
# safe.
#
# A directory each, except the images: those are in podman's storage, whose
# layout is podman's business, so they are asked of podman rather than
# walked. Hence a name with no directory behind it.
IMAGES = "images"

CACHES = {
    "downloads": lambda: ContainerEngine.cache("downloads"),
    "packages":  lambda: ContainerEngine.cache("packages"),
    "chroots":   lambda: ContainerEngine.cache("chroots"),
    IMAGES:      None,
    "scratch":   ContainerEngine.scratch,
}

# The caches worth carrying to another machine. Scratch is not one of them:
# what is in it belongs to a build that is either running or has died, and
# neither is of use anywhere else.
PORTABLE = ["downloads", "packages", "chroots", IMAGES]

# Where the images ride in the tar. One archive holding all of them rather
# than one per image, so a layer shared by every image seine builds -- and
# they all stand on the host bootstrap -- is written once.
IMAGES_MEMBER = "%s/images.tar.gz" % IMAGES


# Every image except the one an export leaves behind: the image's own root
# file-system, which is what mmdebstrap made of the archive on the day it
# ran and is stale as soon as the archive moves.
#
# The images built on that one go all the same -- the kernel libguestfs
# boots, the appliance it runs for a cross build, the transport bootstrap
# ansible connects through -- because what says whether they are current is
# what their base was built *from* rather than which bytes it came out as.
# The receiving machine bootstraps its own root file-system from the same
# specification, which is a different image and the same inputs, so what
# stands on it is still current. The appliance is the largest and slowest
# thing in a storage and the one most worth not making twice.
#
# '--with-image-rootfs' carries the root file-system as well, for a machine
# that wants a copy of another's storage rather than one it can build with.
#
# An image with no kind and a registry to its name is carried too: that is a
# base image everything here is built on, and carrying it is what lets an
# import work with no route to a registry. One with no kind and no registry
# was built by a seine that did not label them and is rebuilt on sight, so it
# is not worth the bytes.
#
# The root file-system a build hands to ansible is not among these either
# way: it is a container, exported as a tarball and never committed, so
# podman has no image of it.
CARRIED_KINDS = [TOOLING_KIND, BUILDER_KIND, IMAGER_KIND, TRANSPORT_KIND]

def images(with_image_rootfs=False):
    named = []
    for image in json.loads(ContainerEngine.check_output(["images", "--format", "json"])):
        kind = (image.get("Labels") or {}).get(KIND_LABEL)
        # Ones with no name are left out for want of a way to ask for them:
        # an intermediate layer is carried by the image standing on it.
        for name in image.get("Names") or []:
            if "<none>" in name:
                continue
            if with_image_rootfs or kind in CARRIED_KINDS:
                named.append(name)
            elif kind is None and name.startswith(LOCAL) == False:
                named.append(name)
    return named

# What podman calls an image nothing pulled: it has no registry, so it says
# so with one of its own.
LOCAL = "localhost/"

# What podman says its storage holds, which is not the sum of what its
# images say they weigh: every image seine builds stands on the host
# bootstrap, so adding them up counts that one once per image.
def images_size():
    listed = ContainerEngine.check_output(["system", "df", "--format", "json"])
    for row in json.loads(listed):
        if row.get("Type") == "Images":
            return row.get("RawSize") or 0
    return 0

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
            if name == IMAGES:
                used, where = images_size(), ContainerEngine.root()
            else:
                path = CACHES[name]()
                used = size_of(path) if os.path.isdir(path) else 0
                where = path
            total += used
            print("%-10s %10s  %s" % (name, human(used), where))
        print("%-10s %10s" % ("total", human(total)))
        return 0

    def clear(self, names):
        # A build running at the same time as this loses whatever it was
        # about to reuse and does the work again -- which is what the cache
        # promises -- but one that is *reading* a tarball as it goes away
        # fails. Clearing a cache is something to do between builds.
        for name in names:
            if name == IMAGES:
                self._clear_images()
                continue
            path = CACHES[name]()
            if os.path.isdir(path) == False:
                continue
            print("removing %s" % path)
            shutil.rmtree(path)
        return 0

    # The images go by name and not by removing the storage under them: what
    # is in a podman storage belongs to uids a rootless user cannot unlink
    # without going back through a user namespace, so 'rm -rf' on it fails
    # halfway and leaves a storage that is neither there nor usable.
    def _clear_images(self):
        print("removing the images from %s" % ContainerEngine.root())
        ContainerEngine.run(["rmi", "--all", "--force"], check=True)

    # One tar holding the named caches, each under its own name, so what
    # comes out of one machine goes into another without either having to
    # agree on where a cache lives. Uncompressed unless the filename asks
    # for it: what is in here is .deb and .tar.zst, already compressed.
    def export(self, names, where, with_image_rootfs=False):
        mode = "w|gz" if where.endswith((".gz", ".tgz")) else "w|"
        stream = sys.stdout.buffer if where == "-" else None
        with tarfile.open(None if stream else where, mode, fileobj=stream) as tar:
            for name in names:
                if name == IMAGES:
                    self._export_images(tar, where, with_image_rootfs)
                    continue
                path = CACHES[name]()
                if os.path.isdir(path) == False:
                    continue
                self.say("exporting %s (%s)"
                         % (name, human(size_of(path, self._carried))), where)
                tar.add(path, arcname=name, recursive=True, filter=self._exported)
        return 0

    # podman writes the images out itself rather than seine walking its
    # storage: what is in there is podman's business, and an archive it
    # wrote is what another podman will read back. The ids and the labels
    # come out of that unchanged, which is what matters -- an image is
    # rebuilt when the label saying what it was built from does not match,
    # so an imported bootstrap is current, and so is everything derived
    # from it, whose label carries that image's id.
    #
    # Through gzip on the way: 'podman save' compresses only when it is
    # writing a directory, and the images are the one thing in the tar that
    # is not compressed already, and it compresses several times over.
    # Level 1, because the last few per cent of a gigabyte cost more time
    # than the bytes are worth.
    #
    # Into a temporary file rather than straight into the tar: a member's
    # size has to be written before its bytes, and podman will not say in
    # advance what it is about to produce. That is one extra copy through
    # the scratch space.
    def _export_images(self, tar, where, with_image_rootfs=False):
        named = images(with_image_rootfs)
        if len(named) == 0:
            return
        self.say("exporting %s (%d image%s)"
                 % (IMAGES, len(named), "" if len(named) == 1 else "s"), where)
        with tempfile.NamedTemporaryFile(dir=ContainerEngine.scratch(),
                                         suffix=".tar.gz") as saved:
            with gzip.GzipFile(fileobj=saved, mode="wb", compresslevel=1) as out:
                podman = ContainerEngine.Popen(
                    ["save", "--multi-image-archive"] + named,
                    stdout=subprocess.PIPE)
                shutil.copyfileobj(podman.stdout, out)
                podman.stdout.close()
                if podman.wait() != 0:
                    raise ValueError("podman could not save the images!")
            saved.flush()
            tar.add(saved.name, arcname=IMAGES_MEMBER)

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
                if cache == IMAGES:
                    self._load_images(tar, member, where)
                    continue
                member.name = rest
                tar.extract(member, path=CACHES[cache]())
        for name in names:
            if name == IMAGES:
                continue
            path = CACHES[name]()
            if os.path.isdir(path):
                self.say("imported %s (%s)" % (name, human(size_of(path))), where)
        return 0

    # Handed to podman as it comes out of the tar: podman reads a gzipped
    # archive as happily as a plain one, so the images go from one storage
    # to the other without being written down on the way.
    def _load_images(self, tar, member, where):
        self.say("importing %s (%s)" % (IMAGES, human(member.size)), where)
        podman = ContainerEngine.Popen(["load"], stdin=subprocess.PIPE)
        shutil.copyfileobj(tar.extractfile(member), podman.stdin)
        podman.stdin.close()
        if podman.wait() != 0:
            raise ValueError("podman could not load the images!")

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
        # gnu_getopt, so a flag may come after the action and the file the
        # way one does everywhere else: plain getopt stops at 'export' and
        # hands '--with-image-rootfs' back as if it were the name of a cache.
        try:
            opts, args = getopt.gnu_getopt(argv, "h", ["help", "with-image-rootfs"])
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, USAGE))
            sys.exit(1)
        with_image_rootfs = False
        for o, _ in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                sys.exit()
            elif o in ("--with-image-rootfs"):
                with_image_rootfs = True

        ACTIONS = ["info", "clear", "export", "import"]
        if len(args) == 0:
            sys.stderr.write("error: cache command expects one of %s\n"
                             % ", ".join(ACTIONS))
            sys.exit(1)

        action, names = args[0], args[1:]
        if action not in ACTIONS:
            sys.stderr.write("error: unknown cache action '%s'\n" % action)
            sys.exit(1)
        if with_image_rootfs and action != "export":
            sys.stderr.write("error: --with-image-rootfs is for 'export', not '%s'\n"
                             % action)
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
                sys.exit(self.export(names, where, with_image_rootfs))
            else:
                sys.exit(self.load(names, where))
        except (OSError, tarfile.TarError, ValueError) as e:
            sys.stderr.write("error: cache %s failed: %s\n" % (action, e))
            sys.exit(1)

USAGE = """
Show what seine has cached, remove it, or move it to another machine

Description:
  seine keeps downloaded packages, rebuilt packages, buildd chroots, the
  container images it builds and the scratch space a build unpacks sources
  into, so that the next build does not make them again. None of it is
  needed for a build to succeed, so any of it can be removed to get the
  disk space back.

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
  images      the container images seine built: the bootstrap tooling, the
              builder, the imager's kernel and appliance and the transport
              bootstrap, in podman storage of its own
  scratch     temporary files a build unpacks sources and images into

  An export leaves out one image: the image's own root file-system, what
  mmdebstrap made of the archive on the day it ran, which is stale as soon as
  the archive moves. What is built on it still travels and is still current
  there, since what decides that is what an image's base was built from
  rather than which bytes it came out as. --with-image-rootfs carries the
  root file-system as well, for a machine that wants a copy of another's
  storage.

Examples:
  seine cache info
  seine cache clear chroots
  seine cache clear downloads packages
  seine cache export caches.tar
  seine cache export --with-image-rootfs caches.tar
  seine cache import caches.tar chroots
  seine cache export - | ssh builder seine cache import -

Flags:
  -h, --help            print this message
      --with-image-rootfs
                        carry the image's root file-system as well, for
                        'export' only

"""
