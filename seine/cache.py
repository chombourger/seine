# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import contextlib

from seine       import analyze
from seine       import cache_index
from seine.cmd   import Cmd
from seine.container import ContainerEngine
from seine.utils import locked
from seine.utils import KIND_LABEL
from seine.utils import BUILDER_KIND
from seine.utils import IMAGER_KIND
from seine.utils import TRANSPORT_KIND
from seine.utils import ROOTFS_KIND
from seine.utils import TOOLING_KIND
from seine.utils import SOURCE_KIND

# Cache seine keeps between builds. Not needed for a build to succeed,
# only saves repeated work, so removing any of it is safe.
# Each is a directory except images, which live in podman's storage.
IMAGES = "images"

CACHES = {
    "downloads": ContainerEngine.downloads_root,
    "packages":  lambda: ContainerEngine.cache("packages"),
    "chroots":   lambda: ContainerEngine.cache("chroots"),
    # Packages fetched during image builds, kept by the engine.
    "bootstraps": lambda: ContainerEngine.cache("bootstraps"),
    # Packages a 'vendor:' section resolved, kept so a spec can still be
    # rebuilt after that feed is gone.
    "vendor":    lambda: ContainerEngine.cache("vendor"),
    IMAGES:      None,
    # Per-step build cost data, read back by 'seine analyze'.
    analyze.RECORDS: lambda: ContainerEngine.cache(analyze.RECORDS),
    "scratch":   ContainerEngine.scratch,
}

# Caches a tar can hold. Not scratch: that belongs to a running or dead
# build, useless on any other machine.
PORTABLE = ["downloads", "packages", "chroots", IMAGES]

# Default export set. Downloads left out: apt needs the mirror archive
# regardless, so carrying it wastes bandwidth -- name it to include anyway.
# Import has no default: it takes whatever the tar holds.
CARRIED = ["packages", "chroots", IMAGES]

# One archive for all images, so the layer shared by every image seine
# builds is written once.
IMAGES_MEMBER = "%s/images.tar.gz" % IMAGES

# Record of what was cached. Not filed under one cache's name since it
# describes all of them.
INDEX_MEMBER = "index.json"

# Kinds carried on export. Not the plain rootfs image: it goes stale as
# soon as the archive it was built from moves, while what stands on it
# (kernel, appliance, transport bootstrap) stays current since it is
# rebuilt from the same spec. '--with-image-rootfs' carries it anyway.
#
# A base image with a registry is carried too, so import works with no
# route to that registry; one with neither kind nor registry is cheap to
# rebuild and skipped.
CARRIED_KINDS = [TOOLING_KIND, BUILDER_KIND, IMAGER_KIND, TRANSPORT_KIND, SOURCE_KIND]

def images(with_image_rootfs=False):
    named = []
    for image in json.loads(ContainerEngine.check_output(["images", "--format", "json"])):
        kind = (image.get("Labels") or {}).get(KIND_LABEL)
        # Skip unnamed images -- no way to ask for them; carried instead
        # via the image built on top of them.
        for name in image.get("Names") or []:
            if "<none>" in name:
                continue
            if with_image_rootfs or kind in CARRIED_KINDS:
                named.append(name)
            elif kind is None and name.startswith(LOCAL) == False:
                named.append(name)
    return named

# podman's own name for a local image, one nothing ever pulled.
LOCAL = "localhost/"

# Storage size straight from podman, not summed per image: every image
# stands on the shared host bootstrap, so summing would count it many
# times over.
def images_size():
    listed = ContainerEngine.check_output(["system", "df", "--format", "json"])
    for row in json.loads(listed):
        if row.get("Type") == "Images":
            return row.get("RawSize") or 0
    return 0

# What a build of these specs would need from the cache, worked out from
# the specs alone -- fetches or builds nothing. Lets an export scope down
# to one project's part of a shared cache.
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
            # One repository per release covers every architecture, so
            # track wanted files rather than directories.
            files_wanted = self.repositories.setdefault(release, set())
            for package, built_for, stamp in builder.stamps(build.image.packages):
                files_wanted.add(os.path.join(STAMPS, os.path.basename(stamp)))
                for name in CacheCmd()._named_by(stamp):
                    files_wanted.add(name)
                self.chroots.add(
                    (release, builder.chroot_architecture(package, built_for)))

    # Whether an index entry belongs to what was asked for.
    def records(self, kind, key):
        from seine import cache_index
        if kind == cache_index.DOWNLOADS:
            return key in self.releases
        if kind == cache_index.CHROOT:
            return tuple(key.rsplit("-", 1)) in self.chroots
        if kind == cache_index.PACKAGE:
            return key.partition("/")[0] in self.repositories
        if kind == cache_index.IMAGE:
            return key in self.images
        return True

    # Whether a tar path (starting with its cache's own name) belongs to
    # what was asked for.
    def holds(self, path):
        cache, _, rest = path.partition("/")
        if rest == "":
            return True
        parts = rest.split("/")
        if cache == "downloads":
            return parts[0] in self.releases
        # Skip an empty release dir too -- the tar should show only what
        # was actually given.
        if cache == "chroots":
            if len(parts) == 1:
                return any(release == parts[0] for release, _ in self.chroots)
            return (parts[0], parts[1]) in self.chroots
        if cache == "packages":
            wanted = self.repositories.get(parts[0])
            if wanted is None:
                return False
            return len(parts) == 1 or "/".join(parts[1:]) in wanted
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

# Reuse packages.py's own STAMPS name, so the two can't drift apart.
from seine.packages import STAMPS

# Which cache each index-entry kind belongs to, so clear/report can find
# what was recorded about it.
KINDS = {
    "downloads": [cache_index.DOWNLOADS],
    "packages":  [cache_index.PACKAGE],
    "chroots":   [cache_index.CHROOT],
    "vendor":    [cache_index.VENDOR],
    IMAGES:      [cache_index.IMAGE],
}

class CacheCmd(Cmd):
    def info(self, names, entries=False, matching=None):
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
            self._entries(names, matching)
        return 0

    # List cache entries oldest-used first -- the order to read them in
    # when deciding what to remove. Times are seine's own record, not the
    # filesystem's (podman keeps none for images at all).
    #
    # 'matching' filters by key (a release/arch/source triple for a
    # package, an image name for the rest); a package entry that survives
    # also prints what it was actually built from, see Builder.digest_excerpt().
    def _entries(self, names, matching=None):
        kinds = [kind for name in names for kind in KINDS.get(name, [])]
        listed = cache_index.Index().entries(
            present=lambda kind, key: kind in kinds
                                      and (matching is None
                                           or matching.search(key) is not None))
        print()
        if len(listed) == 0:
            print("nothing recorded yet: an entry is written as a build "
                  "takes or makes one")
            return
        print("%-10s %-34s %-12s %s" % ("cache", "entry", "last used", "made"))
        for kind, key, entry in listed:
            line = ("%-10s %-34s %-12s %s"
                    % (kind, key, cache_index.since(entry.get("used")),
                       cache_index.since(entry.get("made"))))
            if kind == cache_index.PACKAGE:
                stamp = self._package_stamp(key)
                if stamp:
                    line += "  (%s)" % stamp
            print(line)
            if matching is not None and kind == cache_index.PACKAGE and stamp:
                excerpt = self._package_excerpt(key, stamp)
                if excerpt:
                    print("\n".join("    %s" % l for l in excerpt.splitlines()))

    # The on-disk stamp '<source>_<arch>_<digest>' for an entry -- the
    # index itself never records the digest, only that some build
    # happened.
    def _package_stamp(self, key):
        release, architecture, source = key.split("/", 2)
        stamps = os.path.join(CACHES["packages"](), release, STAMPS)
        if not os.path.isdir(stamps):
            return None
        for stamp in os.listdir(stamps):
            if stamp.rsplit("_", 2)[:2] == [source, architecture]:
                return stamp
        return None

    # Digest excerpt beside a stamp; paths stay relative as
    # Builder._portable_path() wrote them. Missing for older stamps or
    # imports without one -- not an error.
    def _package_excerpt(self, key, stamp):
        from seine.packages import STAMPS_SPEC
        release = key.split("/", 1)[0]
        path = os.path.join(CACHES["packages"](), release, STAMPS_SPEC,
                            "%s.spec" % stamp)
        try:
            with open(path, "r") as f:
                return f.read().rstrip("\n")
        except OSError:
            return None

    # Remove only what's older than this, per the index. A cache with no
    # record is left alone rather than guessed at from mtimes.
    def stale(self, names, older_than):
        with self._alone():
            return self._stale(names, older_than)

    def _stale(self, names, older_than):
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
        elif kind == cache_index.VENDOR:
            # Key is '<suite>_<source>_<name>_<arch>_<version>'; plain
            # split works since Debian names never hold '_'. 'name' is
            # literal "source" for a source artifact (see vendor.py's
            # _artifact_key()), else a binary package name.
            suite, source, name, arch, version = key.split("_", 4)
            where = os.path.join(CACHES["vendor"](), suite)
            # Source filenames drop the epoch ('1:1.2-3' -> '..._1.2-3...');
            # .deb filenames keep it, '%3a'-escaped (vendor.py's
            # _binary_filename()). 'stripped' is the source-side form.
            stripped = version.split(":", 1)[-1]
            if os.path.isdir(where):
                if name == "source":
                    # orig tarball uses the upstream version alone (no
                    # debian revision); .dsc/.debian.tar.* use the full
                    # version. Match either to catch all files of one build.
                    upstream = stripped.rsplit("-", 1)[0]
                    for entry in sorted(os.listdir(where)):
                        if entry.startswith("%s_" % source) and \
                                (stripped in entry or upstream in entry):
                            path = os.path.join(where, entry)
                            if os.path.isfile(path):
                                os.unlink(path)
                else:
                    # 'Architecture: all' binaries are fetched as 'all',
                    # not the resolved arch, so both are tried (see
                    # vendor.py's _binary_already_fetched()) -- and each
                    # under both epoch spellings, same fallback.
                    for candidate in (arch, "all"):
                        for encoded in (version.replace(":", "%3a"), stripped):
                            path = os.path.join(
                                where, "%s_%s_%s.deb" % (name, encoded, candidate))
                            if os.path.isfile(path):
                                os.unlink(path)
            # No derived files to clean here -- the built repository lives
            # in deploy/ instead (vendor.py's deploy_repository()),
            # rebuilt fresh by the next run regardless.
        elif kind == cache_index.PACKAGE:
            release, _, rest = key.partition("/")
            architecture, _, source = rest.partition("/")
            repository = os.path.join(CACHES["packages"](), release)
            stamps = os.path.join(repository, STAMPS)
            # Stamp is '<source>_<arch>_<digest>'; one repository holds
            # every architecture's stamps.
            for stamp in sorted(os.listdir(stamps)) if os.path.isdir(stamps) else []:
                if stamp.rsplit("_", 2)[:2] != [source, architecture]:
                    continue
                for name in self._named_by(os.path.join(stamps, stamp)):
                    path = os.path.join(repository, name)
                    if os.path.isfile(path):
                        os.unlink(path)
                os.unlink(os.path.join(stamps, stamp))
            # The index the .debs were described by is made from what is
            # there, so the next build writes one that matches.
            for derived in ["Packages", "Packages.gz", "Sources", "Sources.gz",
                            "Release", "Release.gpg", "InRelease",
                            ".packages.db"]:
                path = os.path.join(repository, derived)
                if os.path.isfile(path):
                    os.unlink(path)

    # Refuse rather than wait while a build holds the shared storage --
    # better than hanging silently until it finishes.
    @contextlib.contextmanager
    def _alone(self):
        try:
            with locked(ContainerEngine.storage_lock(), blocking=False):
                yield
        except BlockingIOError:
            raise ValueError(
                "a build is running -- the chroots, images and packages it "
                "is standing on would go with the caches. Clear them once "
                "it has finished.")

    def clear(self, names):
        with self._alone():
            return self._clear(names)

    def _clear(self, names):
        # Best effort, cache by cache: a container's apt leaves files
        # owned by a uid we can't unlink (e.g. downloads/*/partial). Skip
        # and continue, so one stuck cache doesn't block the rest.
        index = cache_index.Index()
        left = []
        for name in names:
            try:
                if name == IMAGES:
                    self._clear_images()
                else:
                    self._clear_one(CACHES[name]())
            except (OSError, subprocess.CalledProcessError) as e:
                left.append((name, str(e)))
                continue
            # Drop the index record with the cache -- else a gone entry
            # reports as ancient and gets "evicted" again.
            for kind in KINDS.get(name, []):
                index.forget(kind)

        # Flush stdout first: piped output is block-buffered, so warnings
        # could otherwise print out of order.
        sys.stdout.flush()
        for name, why in left:
            sys.stderr.write("warning: '%s' was not emptied: %s\n" % (name, why))
        if len(left) > 0:
            sys.stderr.write(
                "warning: what a container wrote belongs to a user of its "
                "own; 'podman unshare rm -rf DIR' removes it\n")
        return 1 if len(left) > 0 else 0

    # What can go, goes: a directory that cannot be unlinked leaves the rest
    # of the cache removed rather than the whole of it kept.
    def _clear_one(self, path):
        if os.path.isdir(path) == False:
            return
        print("removing %s" % path)
        shutil.rmtree(path, ignore_errors=True)
        if os.path.exists(path):
            raise OSError("%s is still there" % path)

    # Remove images by name, not by rm -rf on the storage dir: rootless
    # podman's storage has uids we can't unlink directly.
    def _clear_images(self):
        print("removing the images from %s" % ContainerEngine.root())
        ContainerEngine.run(["rmi", "--all", "--force"], check=True)

    # One tar, each cache under its own name. Uncompressed by default --
    # contents (.deb, .tar.zst) are already compressed.
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

    # Export what each entry is and when made, not this machine's usage --
    # an imported last-used time would drive eviction by another
    # machine's history.
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

    # Let podman save/load images itself -- ids and labels survive intact,
    # which is what the rebuild-on-label-mismatch check needs.
    #
    # Gzipped here at level 1: images are the one uncompressed thing in
    # the tar, and squeezing the last bytes out costs more time than it's
    # worth.
    #
    # Written to a temp file first, since podman won't say the size
    # upfront and a tar member needs its size before its bytes.
    def _export_images(self, tar, where, with_image_rootfs=False, wanted=None):
        named = images(with_image_rootfs)
        if wanted is not None:
            # Always keep base images (not built by us, not named by any
            # spec) -- everything else stands on them.
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

    # Excluded, by path suffix:
    #   .lock, /lock      seine's and apt's own lock files
    #   /Packages*        repo indices (+ apt-ftparchive's hash cache) --
    #   /Sources*         rebuilt on the receiving machine from what's
    #   /Release*         there, so shipping them saves nothing. The key
    #   /InRelease        that signs them can't be sent, so its signature
    #   /.packages.db     couldn't be renewed there either.
    #   .build            sbuild logs and their "latest" symlinks -- a
    #                     kernel's log rivals its own .debs, and is
    #                     useless from another machine's build
    #   /partial          apt's in-flight downloads, root-owned 0700
    #                     inside the container -- unreadable anyway
    #
    # Kept beyond the .debs: the stamps (say which .debs are current, for
    # which source package) and the small .changes/.buildinfo (say how
    # the .debs were made).
    NOT_CARRIED = [".lock", "/lock", "/.packages.db", "/Packages", "/Packages.gz",
                   "/Sources", "/Sources.gz", "/Release", "/Release.gpg",
                   "/InRelease", ".build", "/partial"]

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

    # Import: the only place seine writes files it didn't make itself, so
    # every tar member is checked -- file/dir/link, under a known cache,
    # staying inside it (target too, for a link). Fails hard on the first
    # bad member rather than guessing.
    def load(self, names, where, replace=False, force=False):
        arrived = []
        # --replace: make this machine look like the tar, for a runner
        # starting from nothing.
        if replace:
            self.clear(names)
        stream = sys.stdin.buffer if where == "-" else None
        with tarfile.open(None if stream else where, "r|*", fileobj=stream) as tar:
            for member in tar:
                # Handle the index member first -- it describes all
                # caches, not one.
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
                # sbuild's logs include a symlink to the latest one, so
                # links are checked (target inside the cache) as closely
                # as regular files.
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
        # Once everything's extracted: an arrived stamp for a source
        # package we already had supersedes the old build's files.
        if "packages" in names:
            self._prune(arrived, force, where)
        for name in names:
            if name == IMAGES:
                continue
            path = CACHES[name]()
            if os.path.isdir(path):
                self.say("imported %s (%s)" % (name, human(size_of(path))), where)
        return 0

    # A .deb is reachable if some stamp names it. An arrived stamp
    # supersedes an existing build of the same source (import means take
    # theirs), leaving the old stamp's .debs unreachable -- else the repo
    # would offer two versions and apt picks the higher one, pinning aside.
    #
    # A .deb no stamp names at all is a leftover; removing it is a guess,
    # so it waits for --force.
    def _prune(self, arrived, force, where):
        cache = CACHES["packages"]()
        if os.path.isdir(cache) == False:
            return
        arrived = set(arrived)
        for release in sorted(os.listdir(cache)):
            self._prune_one(cache, release, arrived, force, where)

    def _prune_one(self, cache, inside, arrived, force, where):
        repository = os.path.join(cache, inside)
        stamps = os.path.join(repository, STAMPS)
        if os.path.isdir(stamps) == False:
            return

        # Group stamps by source: name is '<source>_<arch>_<digest>', and
        # a later build of the same source+arch supersedes an earlier one.
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
            # The indices and the key that answers for them are the
            # repository's own, made or exported by the build rather than
            # produced by any package in it.
            if name in reachable or name.startswith("Packages") \
                                 or name.startswith("Sources") \
                                 or name.startswith("Release") \
                                 or name == "InRelease" \
                                 or name.endswith(".gpg"):
                continue
            if name in letting_go or force:
                self.say("removing %s" % os.path.join(inside, name), where)
                os.unlink(os.path.join(repository, name))
            elif name.endswith(".deb") or name.endswith(".dsc"):
                self.say("keeping %s, which no stamp names ('--force' removes it)"
                         % os.path.join(inside, name), where)

        # Drop the indices too -- they describe what the directory held a
        # moment ago; the next build regenerates them.
        for derived in ["Packages", "Packages.gz", "Sources", "Sources.gz",
                        "Release", "Release.gpg", "InRelease",
                        ".packages.db"]:
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

    # Merge rule: arrived entries win (a superseded stamp gives way), and
    # entries for a cache not asked about are left alone.
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

    # Streamed straight to podman -- it reads gzipped or plain archives,
    # so images move storage-to-storage without touching disk.
    def _load_images(self, tar, member, where):
        self.say("importing %s (%s)" % (IMAGES, human(member.size)), where)
        podman = ContainerEngine.Popen(["load"], stdin=subprocess.PIPE)
        shutil.copyfileobj(tar.extractfile(member), podman.stdin)
        podman.stdin.close()
        if podman.wait() != 0:
            raise ValueError("podman could not load the images!")

    # Whether a tar path stays inside its cache after resolving '..'. An
    # absolute path fails too -- os.path.join would discard the prefix,
    # so it's rejected explicitly.
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
                argv, "h", ["entries", "entries-matching=", "force", "help",
                            "older-than=", "replace", "spec=",
                            "with-image-rootfs"])
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, USAGE))
            sys.exit(1)
        entries = False
        matching = None
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
            elif o in ("--entries-matching"):
                # Implies '--entries' -- a filter with nothing to filter
                # is not a second flag anyone should have to remember.
                entries = True
                try:
                    matching = re.compile(a)
                except re.error as e:
                    sys.stderr.write(
                        "error: '%s' is not a usable pattern: %s\n" % (a, e))
                    sys.exit(1)
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

        # No names already means all; 'all' just spells that out rather
        # than erroring. For a tar that's PORTABLE, not literally all.
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
                sys.exit(self.info(names, entries, matching))
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

  SEINE_BUILD_DIR moves all of it -- container storage, downloads, caches
  and scratch space alike -- to one drive, defaulting to ./build under the
  working directory; SEINE_CACHE_DIR and SEINE_DL_DIR move the caches or
  the downloads on their own, and win when both are set.

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

  'clear' is refused while a build is running, here or in another
  terminal: what it would remove is what that build is standing on. Builds
  themselves run beside each other as they always have.

  'clear' is best effort. A cache holding what this user cannot unlink --
  what a container's apt left behind belongs to a user of the container's
  own -- does not stop the others being emptied: what was left is named,
  with the way to remove it, and the command exits non-zero.

  'clear --older-than' removes one object at a time rather than a whole
  cache: what was last wanted longer ago than the span given. It asks the
  record and nothing else, so a cache seine kept no record of is left alone
  rather than removed on a guess.

  'info --entries' lists what is in the caches one object at a time, least
  recently used first, which is the order to read it in when the question is
  what to remove. An entry appears the first time a build makes or takes the
  thing it names, so a cache that has never been built against lists
  nothing.

  '--entries-matching PATTERN' (a regex) narrows that listing to entries
  whose key matches, and implies --entries. A package entry that survives
  the filter also prints the specification content it was actually built
  from -- source, patches, and whichever 'extends:' settings it has --
  redacted the same way 'redact:' applies everywhere else, and with every
  file path relative to whichever specification file named it rather than
  where it happened to sit on this machine.

Usage:
  seine cache info [--entries] [--entries-matching PATTERN] [CACHE...|all]
  seine cache clear [--older-than SPAN] [CACHE...|all]
  seine cache export [--with-image-rootfs] FILE|- [CACHE...|all]
  seine cache import [--replace] [--force] FILE|- [CACHE...|all]

Caches:
  downloads   packages fetched from the distribution's feeds
  packages    packages the 'packages' section built, as an apt repository
  chroots     buildd chroot tarballs sbuild unpacks to build a package
  bootstraps  packages the bootstrap and builder image builds fetched
  vendor      packages a 'vendor' section pinned from a remote feed, kept so
              a specification can still be rebuilt once that feed is gone
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
  seine cache info --entries-matching linux
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
      --entries-matching PATTERN
                        as --entries, narrowed to keys matching this
                        regex; a matching package entry also prints what
                        it was built from. Implies --entries, for 'info'
                        only
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
