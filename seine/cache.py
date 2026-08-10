# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from seine       import cache_index
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

# The caches a tar can hold. Scratch is not one of them: what is in it
# belongs to a build that is either running or has died, and neither is of
# use anywhere else.
PORTABLE = ["downloads", "packages", "chroots", IMAGES]

# What an export carries when nothing is named. The downloads are left out:
# a build needs the archive whatever it was sent, since apt reads its lists
# from there and seine caches none of them, so carrying a release's .debs
# saves bandwidth for a machine that has to reach the mirror anyway. Naming
# 'downloads' carries them for the machine that wants that.
#
# An import has no such default: it takes whatever the tar holds, since
# deciding what to send is the exporting machine's business.
CARRIED = ["packages", "chroots", IMAGES]

# Where the images ride in the tar. One archive holding all of them rather
# than one per image, so a layer shared by every image seine builds -- and
# they all stand on the host bootstrap -- is written once.
IMAGES_MEMBER = "%s/images.tar.gz" % IMAGES

# Where the record of what was cached rides. Not under a cache's name: it
# covers all of them, and it is the one member that is not an object a build
# takes but a description of the ones that are.
INDEX_MEMBER = "index.json"

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

# What a build of these specifications would want out of the caches, worked
# out from the specifications alone: none of this fetches anything or builds
# anything.
#
# A cache holds what a machine has built for every board and release it has
# ever been asked for, and a colleague on one project wants the part of it
# their own build would reach for. What that is, seine already knows how to
# say -- the release and architecture name the directories, a package's stamp
# names the .debs its build produced, and the image classes name themselves.
#
# Each specification is a list of files composed the way 'seine build'
# composes them, so what is scoped is what those files describe together.
class Wanted:
    def __init__(self, specifications):
        from seine.build import BuildCmd
        from seine.packages import Builder, STAMPS

        self.releases, self.repositories = set(), {}
        self.chroots, self.images = set(), set()
        for files in specifications:
            build = BuildCmd()
            for name in files:
                build.load(name)
            spec = build.parse()
            distro = spec["distribution"]
            release, architecture = distro["release"], distro["architecture"]

            self.releases.add(release)
            self.images.update(build.image.images())

            builder = Builder(distro, build.options, None)
            files_wanted = self.repositories.setdefault((release, architecture),
                                                        set())
            for package, stamp in builder.stamps(build.image.packages):
                files_wanted.add(os.path.join(STAMPS, os.path.basename(stamp)))
                for name in CacheCmd()._named_by(stamp):
                    files_wanted.add(name)
                self.chroots.add((release, builder.chroot_architecture(package)))

    # Whether an entry of the record belongs to what was asked for, keyed the
    # way the index keys them.
    def records(self, kind, key):
        from seine import cache_index
        if kind == cache_index.DOWNLOADS:
            return key in self.releases
        if kind == cache_index.CHROOT:
            return tuple(key.rsplit("-", 1)) in self.chroots
        if kind == cache_index.PACKAGE:
            release, _, rest = key.partition("/")
            architecture = rest.partition("/")[0]
            return (release, architecture) in self.repositories
        if kind == cache_index.IMAGE:
            return key in self.images
        return True

    # Whether a path inside a cache belongs to what was asked for. The path
    # is the one the tar will hold, so it starts with the cache's own name.
    def holds(self, path):
        cache, _, rest = path.partition("/")
        if rest == "":
            return True
        parts = rest.split("/")
        if cache == "downloads":
            return parts[0] in self.releases
        # A release nothing wants is not carried as an empty directory
        # either: the tar is what a colleague reads to see what they were
        # given.
        if cache == "chroots":
            if len(parts) == 1:
                return any(release == parts[0] for release, _ in self.chroots)
            return (parts[0], parts[1]) in self.chroots
        if cache == "packages":
            if len(parts) == 1:
                return any(release == parts[0] for release, _ in self.repositories)
            wanted = self.repositories.get((parts[0], parts[1]))
            if wanted is None:
                return False
            return len(parts) == 2 or "/".join(parts[2:]) in wanted
        return True

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

# Where a repository keeps the stamps that say what was built from what, as
# packages.py names it. Taken from there rather than spelled again, so the
# two cannot drift apart.
from seine.packages import STAMPS

def _directories(path):
    try:
        return [name for name in os.listdir(path)
                if os.path.isdir(os.path.join(path, name))]
    except OSError:
        return []

# Which cache each kind of index entry belongs to, so a cache that is
# cleared or reported on can find what was recorded about it.
KINDS = {
    "downloads": [cache_index.DOWNLOADS],
    "packages":  [cache_index.PACKAGE],
    "chroots":   [cache_index.CHROOT],
    IMAGES:      [cache_index.IMAGE],
}

class CacheCmd(Cmd):
    def info(self, names, entries=False):
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
        if entries:
            self._entries(names)
        return 0

    # What is in the caches one object at a time, least recently used first
    # -- which is the order to read it in when the question is what to
    # remove. The times are seine's own record rather than the filesystem's:
    # atime is a day's worth of resolution where a filesystem keeps it at
    # all, and podman keeps no last-used time for an image.
    def _entries(self, names):
        kinds = [kind for name in names for kind in KINDS.get(name, [])]
        listed = cache_index.Index().entries(
            present=lambda kind, key: kind in kinds)
        print()
        if len(listed) == 0:
            print("nothing recorded yet: an entry is written as a build "
                  "takes or makes one")
            return
        print("%-10s %-34s %-12s %s" % ("cache", "entry", "last used", "made"))
        for kind, key, entry in listed:
            print("%-10s %-34s %-12s %s"
                  % (kind, key, cache_index.since(entry.get("used")),
                     cache_index.since(entry.get("made"))))

    # What was last wanted longer ago than this, and nothing else. The index
    # is the only thing asked: what it does not know about, it does not
    # remove. A cache written by a seine that kept no record is left alone
    # rather than deleted on a guess about its mtimes.
    def stale(self, names, older_than):
        cutoff = int(time.time()) - older_than
        index = cache_index.Index()
        kinds = [kind for name in names for kind in KINDS.get(name, [])]
        removing = [(kind, key, entry) for kind, key, entry
                    in index.entries(present=lambda kind, key: kind in kinds)
                    if (entry.get("used") or 0) < cutoff]
        if len(removing) == 0:
            print("nothing in %s was last wanted that long ago"
                  % ", ".join(names))
            return 0
        for kind, key, entry in removing:
            print("removing %s %s, last wanted %s"
                  % (kind, key, cache_index.since(entry.get("used"))))
            self._evict(kind, key)
            index.forget(kind, key)
        return 0

    # One cached object, by what the index calls it.
    def _evict(self, kind, key):
        if kind == cache_index.IMAGE:
            ContainerEngine.run(["rmi", "--force", key], check=False)
        elif kind == cache_index.CHROOT:
            release, _, architecture = key.rpartition("-")
            where = os.path.join(CACHES["chroots"](), release, architecture)
            shutil.rmtree(where, ignore_errors=True)
        elif kind == cache_index.DOWNLOADS:
            shutil.rmtree(os.path.join(CACHES["downloads"](), key),
                          ignore_errors=True)
        elif kind == cache_index.PACKAGE:
            release, _, rest = key.partition("/")
            architecture, _, source = rest.partition("/")
            repository = os.path.join(CACHES["packages"](), release, architecture)
            stamps = os.path.join(repository, STAMPS)
            for stamp in sorted(os.listdir(stamps)) if os.path.isdir(stamps) else []:
                if stamp.rsplit("_", 1)[0] != source:
                    continue
                for name in self._named_by(os.path.join(stamps, stamp)):
                    path = os.path.join(repository, name)
                    if os.path.isfile(path):
                        os.unlink(path)
                os.unlink(os.path.join(stamps, stamp))
            # The index the .debs were described by is made from what is
            # there, so the next build writes one that matches.
            for derived in ["Packages", "Packages.gz", ".packages.db"]:
                path = os.path.join(repository, derived)
                if os.path.isfile(path):
                    os.unlink(path)

    def clear(self, names):
        # A build running at the same time as this loses whatever it was
        # about to reuse and does the work again -- which is what the cache
        # promises -- but one that is *reading* a tarball as it goes away
        # fails. Clearing a cache is something to do between builds.
        index = cache_index.Index()
        for name in names:
            # What was recorded about a cache goes with the cache: an entry
            # for something that is not there any more would be reported as
            # a very old object and evicted twice.
            for kind in KINDS.get(name, []):
                index.forget(kind)
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
    def export(self, names, where, with_image_rootfs=False, wanted=None):
        mode = "w|gz" if where.endswith((".gz", ".tgz")) else "w|"
        stream = sys.stdout.buffer if where == "-" else None
        with tarfile.open(None if stream else where, mode, fileobj=stream) as tar:
            for name in names:
                if name == IMAGES:
                    self._export_images(tar, where, with_image_rootfs, wanted)
                    continue
                path = CACHES[name]()
                if os.path.isdir(path) == False:
                    continue
                # The size reported is the size carried, so a scoped export
                # says what it is really sending.
                def inside(real, name=name, path=path):
                    if self._carried(real) == False:
                        return False
                    return wanted is None or wanted.holds(name + real[len(path):])
                self.say("exporting %s (%s)"
                         % (name, human(size_of(path, inside))), where)
                tar.add(path, arcname=name, recursive=True,
                        filter=lambda entry: self._exported(entry, wanted))
            self._export_index(tar, where, names, wanted)
        return 0

    # What each entry is and when it was made, and nothing about this
    # machine's use of it: how often someone else reached for a chroot says
    # nothing about the machine reading this, and a last-used time from over
    # there would have the first eviction sweep deleting on another machine's
    # history.
    def _export_index(self, tar, where, names, wanted=None):
        # Only the caches this tar holds: a record of a cache that was not
        # sent describes nothing the other machine has.
        kinds = [kind for name in names for kind in KINDS.get(name, [])]
        recorded = {kind: entries for kind, entries
                    in cache_index.Index().stripped().items() if kind in kinds}
        if wanted is not None:
            # A record of what was not sent would have the other machine
            # reporting things it does not have.
            recorded = {kind: {key: entry for key, entry in entries.items()
                               if wanted.records(kind, key)}
                        for kind, entries in recorded.items()}
            recorded = {kind: entries for kind, entries in recorded.items()
                        if len(entries) > 0}
        if len(recorded) == 0:
            return
        written = json.dumps(recorded, indent=1, sort_keys=True).encode()
        entry = tarfile.TarInfo(INDEX_MEMBER)
        entry.size = len(written)
        entry.mode = 0o644
        tar.addfile(entry, io.BytesIO(written))

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
    def _export_images(self, tar, where, with_image_rootfs=False, wanted=None):
        named = images(with_image_rootfs)
        if wanted is not None:
            # A base image nothing here built is kept whatever was asked for:
            # it is what everything else is built on, and it is not named by
            # any specification.
            named = [name for name in named
                     if name.startswith(LOCAL) == False
                     or name.removeprefix(LOCAL).rsplit(":", 1)[0] in wanted.images]
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
    #   /Packages         the repository index, and its .gz, and
    #   /Packages.gz      apt-ftparchive's hash cache beside them: all three
    #   /.packages.db     are made from the .debs in the directory, so the
    #                     machine that receives those makes its own in one
    #                     pass rather than being sent a description of a
    #                     repository it is about to change
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
    # What is kept beyond the .debs themselves is the stamps: they are what
    # says the .debs are current, and what names which .debs belong to which
    # source package, which is how an import knows what it supersedes. The
    # .changes and .buildinfo are kept too -- small, and what says how the
    # .debs beside them were made.
    NOT_CARRIED = [".lock", "/lock", "/.packages.db", "/Packages", "/Packages.gz",
                   ".build", "/partial"]

    def _carried(self, name):
        return any(name.endswith(suffix)
                   for suffix in CacheCmd.NOT_CARRIED) == False

    # Returning None for a directory prunes what is under it as well, which
    # is what keeps the export out of apt's unreadable 'partial'.
    def _exported(self, entry, wanted=None):
        if self._carried(entry.name) == False:
            return None
        if wanted is not None and wanted.holds(entry.name) == False:
            return None
        return entry

    # The other half, and the only place seine writes files it did not make
    # itself: a tar handed to it may name anything at all. Every member has
    # to be a file, a directory or a link, under a cache seine knows and can
    # carry, and has to stay inside that cache -- both where it is written
    # and, for a link, what it points at. The whole import fails on the
    # first member that does not: half of someone else's tar is not a
    # cache, and a tar reaching out of one is not worth guessing about.
    def load(self, names, where, replace=False, force=False):
        arrived = []
        # 'make this machine look like that tar', which is what a runner
        # starting from nothing wants: what is here now cannot be worth
        # keeping if it is about to be replaced wholesale.
        if replace:
            self.clear(names)
        stream = sys.stdin.buffer if where == "-" else None
        with tarfile.open(None if stream else where, "r|*", fileobj=stream) as tar:
            for member in tar:
                # The index describes every cache rather than being in one,
                # so it is taken before a member is asked which cache it
                # belongs to.
                if member.name == INDEX_MEMBER:
                    if member.isfile():
                        self._load_index(tar, member, names, where)
                    continue
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
                if cache == "packages":
                    arrived.append(rest)
        # What the tar superseded, once all of it is in: a stamp names the
        # .debs its build produced, so a stamp that arrived for a source
        # package this machine had already built says which of the ones
        # already here are not reachable any more.
        if "packages" in names:
            self._prune(arrived, force, where)
        for name in names:
            if name == IMAGES:
                continue
            path = CACHES[name]()
            if os.path.isdir(path):
                self.say("imported %s (%s)" % (name, human(size_of(path))), where)
        return 0

    # A .deb is reachable if a stamp names it: a stamp lists the files its
    # build produced, which is how seine already undoes an earlier build of
    # the same source package.
    #
    # An import brings .debs and the stamps that describe them, so a stamp
    # that arrived for a source package this machine had already built
    # supersedes the one that was here -- an import means take theirs -- and
    # what only the superseded stamp named is not reachable any more. Left
    # there, the repository would offer two versions of the same package and
    # apt would install the higher of them, which is neither what the
    # specification pinned nor what either stamp describes.
    #
    # A .deb no stamp names at all is a leftover of a seine that did not
    # record them, or of a directory someone tidied by hand. Removing it is
    # guesswork rather than following a stamp, so it waits for --force.
    def _prune(self, arrived, force, where):
        cache = CACHES["packages"]()
        if os.path.isdir(cache) == False:
            return
        arrived = set(arrived)
        for release in sorted(os.listdir(cache)):
            for architecture in sorted(_directories(os.path.join(cache, release))):
                self._prune_one(cache, os.path.join(release, architecture),
                                arrived, force, where)

    def _prune_one(self, cache, inside, arrived, force, where):
        repository = os.path.join(cache, inside)
        stamps = os.path.join(repository, STAMPS)
        if os.path.isdir(stamps) == False:
            return

        # Every stamp in there, by the source package it belongs to: a name
        # is '<source>_<digest of what it was built from>'.
        stamped = {}
        for stamp in sorted(os.listdir(stamps)):
            stamped.setdefault(stamp.rsplit("_", 1)[0], []).append(stamp)

        superseded, keeping = [], []
        for source, versions in stamped.items():
            theirs = [stamp for stamp in versions
                      if os.path.join(inside, STAMPS, stamp) in arrived]
            for stamp in versions:
                if len(theirs) == 0 or stamp in theirs:
                    keeping.append(stamp)
                else:
                    superseded.append(stamp)

        reachable = set()
        for stamp in keeping:
            reachable.update(self._named_by(os.path.join(stamps, stamp)))
        letting_go = set()
        for stamp in superseded:
            letting_go.update(self._named_by(os.path.join(stamps, stamp)))
            self.say("superseded %s" % os.path.join(inside, STAMPS, stamp), where)
            os.unlink(os.path.join(stamps, stamp))

        held = [name for name in sorted(os.listdir(repository))
                if os.path.isfile(os.path.join(repository, name))]
        for name in held:
            if name in reachable or name.startswith("Packages"):
                continue
            if name in letting_go or force:
                self.say("removing %s" % os.path.join(inside, name), where)
                os.unlink(os.path.join(repository, name))
            elif name.endswith(".deb"):
                self.say("keeping %s, which no stamp names ('--force' removes it)"
                         % os.path.join(inside, name), where)

        # The index describes what the directory held a moment ago. It is
        # made from the directory rather than carried, so the honest thing to
        # do with it here is take it away and let the next build write one.
        for derived in ["Packages", "Packages.gz", ".packages.db"]:
            path = os.path.join(repository, derived)
            if os.path.isfile(path):
                os.unlink(path)

    # The files a stamp says its build produced.
    def _named_by(self, stamp):
        try:
            with open(stamp, "r") as f:
                return [line.strip() for line in f if len(line.strip()) > 0]
        except OSError:
            return []

    # What arrived is theirs for the entries they carry and mine for the
    # ones they do not, which is the rule the objects themselves follow: a
    # superseded stamp gives way to the one that came with the tar, and an
    # entry for a cache this import was not asked about is left alone.
    def _load_index(self, tar, member, names, where):
        kinds = [kind for name in names for kind in KINDS.get(name, [])]
        try:
            carried = json.loads(tar.extractfile(member).read())
        except (OSError, ValueError):
            # An index that cannot be read is an index that says nothing.
            # Nothing decides a build from it, so this is not worth failing
            # an import over.
            self.say("the record of what was cached could not be read", where)
            return
        if type(carried) != type({}):
            return
        cache_index.Index().merge({kind: entries
                                   for kind, entries in carried.items()
                                   if kind in kinds})

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
            opts, args = getopt.gnu_getopt(
                argv, "h", ["entries", "force", "help", "older-than=",
                            "replace", "spec=", "with-image-rootfs"])
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, USAGE))
            sys.exit(1)
        entries = False
        force = False
        older_than = None
        replace = False
        specifications = []
        with_image_rootfs = False
        for o, a in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                sys.exit()
            elif o in ("--entries"):
                entries = True
            elif o in ("--force"):
                force = True
            elif o in ("--older-than"):
                try:
                    older_than = cache_index.span(a)
                except ValueError as e:
                    sys.stderr.write("error: %s\n" % e)
                    sys.exit(1)
            elif o in ("--replace"):
                replace = True
            elif o in ("--spec"):
                # One specification per '--spec', its files composed the way
                # 'seine build' composes a list of them.
                specifications.append([name for name in a.split(",") if name])
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
        if entries and action != "info":
            sys.stderr.write("error: --entries is for 'info', not '%s'\n" % action)
            sys.exit(1)
        if older_than is not None and action != "clear":
            sys.stderr.write("error: --older-than is for 'clear', not '%s'\n"
                             % action)
            sys.exit(1)
        if len(specifications) > 0 and action != "export":
            sys.stderr.write("error: --spec is for 'export', not '%s'\n" % action)
            sys.exit(1)
        for flag, asked in [("--force", force), ("--replace", replace)]:
            if asked and action != "import":
                sys.stderr.write("error: %s is for 'import', not '%s'\n"
                                 % (flag, action))
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
        # answer to being clear. For a tar that means every cache one can
        # hold, which is not quite all of them.
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

        # An export named nothing carries what is worth carrying; an import
        # named nothing takes what it was sent.
        default = CARRIED if action == "export" else every
        names = names if len(names) > 0 else list(default)
        try:
            if action == "info":
                sys.exit(self.info(names, entries))
            elif action == "clear":
                sys.exit(self.stale(names, older_than) if older_than is not None
                         else self.clear(names))
            elif action == "export":
                wanted = Wanted(specifications) if len(specifications) > 0 else None
                sys.exit(self.export(names, where, with_image_rootfs, wanted))
            else:
                sys.exit(self.load(names, where, replace, force))
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

  '--spec' scopes an export to what a build of those specifications would
  want: the chroot it unpacks, the .debs its own packages produced, and the
  images it runs in, leaving behind everything this machine holds for other
  boards and releases. Give it one specification per '--spec', its files
  separated by commas as 'seine build' would take them.

  An export named no cache carries the packages, the chroots and the images.
  The downloads are left out: a build reaches the archive whatever it was
  sent, since apt reads its lists from there and seine caches none of them.
  Name 'downloads', or 'all', to carry them anyway. An import named no cache
  takes whatever the tar holds.

  An import extends what is already here: what the tar carries is written
  over what shares its name, and a build of a source package that arrives
  supersedes the one this machine had -- a flat repository offering two
  versions of one package is a repository apt takes the higher of.
  '--replace' empties the caches named before reading the tar instead, which
  is what a runner starting from nothing wants. Neither removes a .deb that
  no stamp names, since that is a leftover rather than something superseded:
  '--force' does.

  The repository index is not carried either way. It is made from whatever
  the directory holds, so an import takes it away and the next build writes
  one describing what is really there.

  Named with no cache -- or with 'all' -- an action covers every cache it
  applies to. That is all of them for 'info' and 'clear'; for a tar it is
  the caches worth carrying, which excludes 'scratch'.

  'clear --older-than' removes one object at a time rather than a whole
  cache: what was last wanted longer ago than the span given. It asks the
  record and nothing else, so a cache seine kept no record of is left alone
  rather than removed on a guess.

  'info --entries' lists what is in the caches one object at a time, least
  recently used first, which is the order to read it in when the question is
  what to remove. An entry appears the first time a build makes or takes the
  thing it names, so a cache that has never been built against lists
  nothing.

Usage:
  seine cache info [--entries] [CACHE...|all]
  seine cache clear [--older-than SPAN] [CACHE...|all]
  seine cache export [--with-image-rootfs] FILE|- [CACHE...|all]
  seine cache import [--replace] [--force] FILE|- [CACHE...|all]

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
  seine cache info --entries
  seine cache clear chroots
  seine cache clear downloads packages
  seine cache clear --older-than 30d
  seine cache export caches.tar
  seine cache export caches.tar all
  seine cache export --spec common/amd64.yaml,pc-image/main.yaml caches.tar
  seine cache export --with-image-rootfs caches.tar
  seine cache import caches.tar chroots
  seine cache import --replace caches.tar
  seine cache export - | ssh builder seine cache import -

Flags:
      --entries         list what is cached one object at a time, least
                        recently used first, for 'info' only
      --force           remove .debs no stamp names, for 'import' only
      --older-than SPAN remove only what was last wanted longer ago than
                        this ('30d', '6h', '2w'), for 'clear' only
  -h, --help            print this message
      --replace         empty the caches named before reading the tar, for
                        'import' only
      --spec FILE[,...] carry only what a build of this specification would
                        want, for 'export' only. May be given more than once
      --with-image-rootfs
                        carry the image's root file-system as well, for
                        'export' only

"""
