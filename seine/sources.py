# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Package sources pulled for the AI chat's 'bash'/'read' tools (and by
# hand, via 'seine source') to inspect -- what a package actually does,
# rather than what training data guesses it does. Unpacked under
# ContainerEngine.workbench(), one directory per source, named however
# dpkg-source names it (same "let the tool name it" rule packages.py's
# own rebuild fetch follows) rather than something we invent.

import getopt
import json
import os
import shutil
import sys
import time

from seine.bootstrap import Bootstrap
from seine.cmd       import Cmd
from seine.utils     import apt_sources
from seine.utils     import ContainerEngine
from seine.utils     import SOURCE_KIND

INDEX_FILE = "index.json"

# Host architecture, never the target's: fetching and unpacking a source
# package runs no target code, so paying qemu's emulation cost for it (the
# way a BuilderImage-based fetch would, being sbuild's own chroot arch)
# buys nothing. Generic per release rather than per specification -- no
# feed is baked in here, only dpkg-dev -- so every specification on the
# same release shares one image regardless of which mirror or components
# its own 'distro' asks for; pull() applies those live, the same split
# TargetBootstrap/AnsibleContainerRunner._configure_feeds() already use
# for the target rootfs.
class SourceBootstrap(Bootstrap):
    kind = SOURCE_KIND

    def create(self):
        return self.build(SOURCE_IMAGE_SCRIPT.format(
            self.distro["source"], self.distro["release"]))

    def defaultName(self):
        # Host architecture only -- see the class comment -- so it is left
        # out of the tag the same way BuilderImage.defaultName() leaves it
        # out for the same reason.
        return os.path.join("source", self.distro["source"], self.distro["release"])

    # No SBUILD_RUN_OPTIONS: unlike BuilderImage, nothing here runs sbuild
    # or needs a nested user namespace, just apt-get/dpkg-source.
    # run_captured() so a caller running from the AI chat's worker
    # thread gets (returncode, output) back instead of apt's progress
    # output hitting the terminal raw.
    def exec(self, args, volumes=None, workdir=None):
        cmd = ["container", "run", "--rm"]
        for host, container in volumes or []:
            cmd += ["-v", "%s:%s" % (host, container)]
        if workdir is not None:
            cmd += ["-w", workdir]
        return ContainerEngine.run_captured(cmd + [self.name] + args)

SOURCE_IMAGE_SCRIPT = """
FROM {0}:{1}
RUN apt-get update -qqy && \\
    apt-get install -qqy --no-install-recommends dpkg-dev && \\
    rm -rf /var/lib/apt/lists/*
"""

# One index.json for the whole workbench, 'sources' its own key rather
# than the whole file -- room for whatever else ends up sharing the
# workbench later (the 'bash' tool's own use of it, say) without a
# second index file or a reshuffle of this one.
#
# Within 'sources', keyed by name rather than by directory: a name is
# what a person or the AI chat asks for and removes by, and it is the
# one thing a directory name alone cannot always be recovered from
# (dpkg-source's own '<source>-<version>' split is ambiguous for a
# version containing a dash). Architecture is deliberately not carried
# here -- fetching a source is host-architecture work regardless of the
# target's own (see SourceBootstrap's own comment above), so it would
# only be recorded, never actually distinguish anything.
def _index_path(directory):
    return os.path.join(directory, INDEX_FILE)

def _load_index(directory):
    try:
        with open(_index_path(directory)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def _save_index(directory, index):
    path = _index_path(directory)
    temporary = "%s.new" % path
    with open(temporary, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(temporary, path)

# apt's own 'name[=version]' syntax, the one 'seine source pull' and the
# AI tool's 'name' argument both accept as one string rather than a
# separate 'version' field.
def parse(spec):
    name, _, version = spec.partition("=")
    return name, version or None

# Recorded by pull() once a source lands -- split out so the bookkeeping
# can be tested without a container.
def record(name, dirname, version, release, requested, directory=None):
    directory = directory or ContainerEngine.workbench()
    index = _load_index(directory)
    index.setdefault("sources", {})[name] = {
        "dir": dirname, "version": version, "release": release,
        "requested": requested, "pulled_at": int(time.time())}
    _save_index(directory, index)

# name -> index entry, or None if name was never recorded. Every listed
# entry's own 'dir' is checked against what is actually on disk, so a
# directory removed by hand does not linger as a phantom pull.
def _entries(directory):
    sources = _load_index(directory).get("sources", {})
    return {name: entry for name, entry in sources.items()
            if os.path.isdir(os.path.join(directory, entry["dir"]))}

# (name, entry) pairs, sorted by name, for every source the index knows
# about and still has a directory for. A directory present on disk but
# missing from the index (dropped in by hand, or the index lost) is not
# guessed at here -- see remove()'s own fallback for why a name still
# reaches it without one.
def list_pulled(directory=None):
    directory = directory or ContainerEngine.workbench()
    return sorted(_entries(directory).items())

# The index entry first; lacking one (dropped in by hand, or the index
# lost), the one directory starting 'name-' -- dpkg-source's own naming,
# same prefix packages.py's _source_dir() relies on. Ambiguous (more
# than one such directory) or missing either way is refused rather than
# guessed at.
def remove(name, directory=None):
    directory = directory or ContainerEngine.workbench()
    index = _load_index(directory)
    sources = index.setdefault("sources", {})
    entry = sources.get(name)
    if entry is not None:
        dirname = entry["dir"]
    else:
        prefix = name + "-"
        candidates = [d for d in os.listdir(directory)
                     if d.startswith(prefix)
                     and os.path.isdir(os.path.join(directory, d))]
        if len(candidates) != 1:
            raise ValueError("no pulled source named '%s'" % name)
        dirname = candidates[0]
    shutil.rmtree(os.path.join(directory, dirname))
    if name in sources:
        del sources[name]
        _save_index(directory, index)

# Fetches 'spec' ('name' or 'name=version', apt's own syntax) into the
# workbench, resolved against 'distro' 's own feeds -- applied live
# rather than baked into SourceBootstrap, see its class comment. Returns
# the directory dpkg-source left it in.
def pull(spec, distro, options=None, directory=None):
    name, version = parse(spec)
    directory = directory or ContainerEngine.workbench()
    existing = _load_index(directory).get("sources", {}).get(name)
    if existing and os.path.isdir(os.path.join(directory, existing["dir"])):
        raise ValueError("'%s' is already pulled (as %s) -- 'source rm %s' "
                         "first" % (name, existing["dir"], name))

    image = SourceBootstrap(distro, options or {})
    image.create()

    package = "%s=%s" % (name, version) if version else name
    feed_lines = "".join(
        "echo '%s' >> /etc/apt/sources.list.d/seine-source.list; " % line
        for line in apt_sources(distro, sources=True))
    script = feed_lines + "apt-get update -qqy && apt-get source %s" % package

    before = set(os.listdir(directory))
    returncode, output = image.exec(["sh", "-c", script],
                                    volumes=[(directory, directory)],
                                    workdir=directory)
    if returncode != 0:
        raise ValueError("'apt-get source %s' failed:\n%s"
                         % (package, output.strip()))

    # apt-get source also leaves the .dsc and the tarballs it unpacked
    # from beside the directory -- of no further use once unpacked, and
    # otherwise never cleaned up since nothing here fetches into a
    # directory of its own the way sbuild's staging does.
    new = set(os.listdir(directory)) - before
    new_dirs = [n for n in new if os.path.isdir(os.path.join(directory, n))]
    if len(new_dirs) != 1:
        raise ValueError("'apt-get source %s' did not leave exactly one new "
                         "directory (found: %s)"
                         % (package, ", ".join(sorted(new_dirs)) or "none"))
    dirname = new_dirs[0]
    for n in new - {dirname}:
        os.remove(os.path.join(directory, n))

    resolved_version = dirname[len(name) + 1:] if dirname.startswith(name + "-") else None
    record(name, dirname, resolved_version, distro["release"], spec,
          directory=directory)
    return dirname

USAGE = """
Usage:
  seine source ls
  seine source rm NAME
  seine source pull NAME[=VERSION] SPEC...

List, remove, or pull a package's source into the workbench
(%s, or SEINE_WORKBENCH_DIR) -- for the AI chat's 'bash'/'read' tools,
or by hand, to check what a package actually does rather than what
training data guesses. 'pull' resolves NAME against SPEC's own
distribution/feeds, the same ones a build of SPEC would install from;
give '=VERSION' (apt's own syntax) for a specific one, e.g. a version
an SBOM (seine build --sbom) recorded as actually installed.
""" % ContainerEngine.workbench()

class SourceCmd(Cmd):
    def main(self, argv):
        try:
            opts, args = getopt.gnu_getopt(argv, "h", ["help"])
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, USAGE))
            sys.exit(1)
        for o, a in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                sys.exit()

        ACTIONS = ["ls", "rm", "pull"]
        if len(args) == 0:
            sys.stderr.write("error: source command expects one of %s\n"
                             % ", ".join(ACTIONS))
            sys.exit(1)
        action, rest = args[0], args[1:]
        if action not in ACTIONS:
            sys.stderr.write("error: unknown source action '%s'\n" % action)
            sys.exit(1)

        if action == "ls":
            self._ls()
        elif action == "rm":
            if len(rest) != 1:
                sys.stderr.write("error: source rm expects one NAME\n")
                sys.exit(1)
            try:
                remove(rest[0])
            except ValueError as e:
                sys.stderr.write("error: %s\n" % e)
                sys.exit(1)
        else:
            self._pull(rest)

    def _ls(self):
        found = list_pulled()
        if not found:
            print("no sources pulled yet -- %s" % ContainerEngine.workbench())
            return
        width = max(len(name) for name, _ in found)
        for name, entry in found:
            print("%-*s  %-12s  %s" % (width, name, entry["version"] or "?",
                                       entry["release"]))

    # Loads SPEC the same way 'seine validate'/'seine inspect' do -- just
    # far enough to reach 'distribution', never touching a container --
    # so pulling a source needs nothing a real build hasn't already asked
    # the specification for.
    def _pull(self, rest):
        if len(rest) < 2:
            sys.stderr.write(
                "error: source pull expects NAME[=VERSION] and one or "
                "more specification files\n")
            sys.exit(1)
        package, files = rest[0], rest[1:]

        from seine.build import BuildCmd
        build = BuildCmd()
        build.options = dict(build.options, ansible_library=[])
        try:
            build.load_all(files)
            spec = build.parse()
        except OSError as e:
            sys.stderr.write("error: couldn't open specification file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: specification is invalid: %s\n" % e)
            sys.exit(3)

        try:
            dirname = pull(package, spec["distribution"])
        except ValueError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(1)
        print("pulled %s into %s" % (package, os.path.join(
            ContainerEngine.workbench(), dirname)))
