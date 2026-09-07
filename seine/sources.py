# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Fetches package sources into ContainerEngine.workbench() so the AI
# chat's 'bash'/'read' tools (or 'seine source' by hand) can inspect
# what a package actually does. Each source keeps dpkg-source's own
# directory name.

import getopt
import json
import os
import shutil
import sys
import time

from seine.bootstrap import Bootstrap, HostBootstrap
from seine.cmd       import Cmd
from seine.utils     import apt_sources
from seine.container import ContainerEngine
from seine.utils     import SOURCE_KIND

INDEX_FILE = "index.json"

# Always host architecture: fetching a source package runs no target
# code, so there is no reason to pay qemu's emulation cost. Generic per
# release, not per specification -- feeds are applied live by pull().
class SourceBootstrap(Bootstrap):
    kind = SOURCE_KIND

    def create(self):
        return self.build(SOURCE_IMAGE_SCRIPT.format(
            self.distro["source"], self.distro["release"]))

    def defaultName(self):
        # No arch in the tag: always host arch, see class comment above.
        return os.path.join("source", self.distro["source"], self.distro["release"])

    # No PRIVILEGED_RUN_OPTIONS: just apt-get/dpkg-source here, no sbuild
    # or mmdebstrap needing a nested user namespace.
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

# One index.json per workbench, sources keyed by name (not directory,
# since dpkg-source's '<source>-<version>' split is ambiguous when the
# version itself has a dash).
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

# Splits apt's own 'name[=version]' syntax into (name, version).
def parse(spec):
    name, _, version = spec.partition("=")
    return name, version or None

# Records a pulled source in the index. 'accessed_at' starts equal to
# 'pulled_at' and moves forward via touch()/touch_path() as 'read'/'bash'
# use it, for "what hasn't been used lately" housekeeping.
def record(name, dirname, version, release, requested, directory=None):
    directory = directory or ContainerEngine.workbench()
    index = _load_index(directory)
    now = int(time.time())
    index.setdefault("sources", {})[name] = {
        "dir": dirname, "version": version, "release": release,
        "requested": requested, "pulled_at": now, "accessed_at": now}
    _save_index(directory, index)

# Bumps 'name' 's 'accessed_at' to now. Called by 'read'/'bash', not by
# ls/pull (which already set pulled_at). No-op if name isn't in the index.
def touch(name, directory=None):
    directory = directory or ContainerEngine.workbench()
    index = _load_index(directory)
    entry = index.get("sources", {}).get(name)
    if entry is not None:
        entry["accessed_at"] = int(time.time())
        _save_index(directory, index)

# touch(), but by path: finds which pulled source's directory 'path'
# falls under and touches that one. No match, no-op.
def touch_path(path, directory=None):
    directory = directory or ContainerEngine.workbench()
    real = os.path.realpath(path)
    for name, entry in _entries(directory).items():
        entry_dir = os.path.realpath(os.path.join(directory, entry["dir"]))
        if real == entry_dir or real.startswith(entry_dir + os.sep):
            touch(name, directory)
            return

# Index entries whose directory still exists on disk, so one removed by
# hand doesn't linger as a phantom pull.
def _entries(directory):
    sources = _load_index(directory).get("sources", {})
    return {name: entry for name, entry in sources.items()
            if os.path.isdir(os.path.join(directory, entry["dir"]))}

# (name, entry) pairs, sorted by name, for sources still in the index.
def list_pulled(directory=None):
    directory = directory or ContainerEngine.workbench()
    return sorted(_entries(directory).items())

# Uses the index entry if there is one, else falls back to the single
# directory starting 'name-' (dpkg-source's naming). Refuses if that's
# ambiguous or missing.
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

# Fetches 'spec' ('name' or 'name=version') into the workbench using
# 'distro' 's feeds. Returns the directory dpkg-source left it in.
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

    # apt-get source also leaves the .dsc and source tarballs behind;
    # remove them, only the unpacked directory is kept.
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

# Wall-clock ceiling on one 'bash' call, enforced via podman's own
# 'container run --timeout' so the container is killed server-side.
BASH_TIMEOUT_SECONDS = 120

# Past this many lines, only the tail is kept, so output can't grow
# unbounded.
BASH_OUTPUT_MAX_LINES = 200

# Runs 'command' in a throwaway, unprivileged container using the
# existing HostBootstrap image. Only the workbench (or 'cwd' under it)
# is bind-mounted in, so nothing else on the host is reachable.
def bash(command, distro, options=None, cwd=None, directory=None,
        timeout=BASH_TIMEOUT_SECONDS):
    directory = directory or ContainerEngine.workbench()
    workdir = os.path.join(directory, cwd) if cwd else directory
    real = os.path.realpath(workdir)
    root = os.path.realpath(directory)
    if real != root and not real.startswith(root + os.sep):
        raise ValueError("'cwd' must stay under the workbench")
    if not os.path.isdir(real):
        raise ValueError("'%s' is not a directory under the workbench" % cwd)
    touch_path(real, directory)

    image = HostBootstrap(distro, options or {})
    image.create()

    cmd = ["container", "run", "--rm", "--timeout", str(timeout),
          "-v", "%s:%s" % (directory, directory), "-w", real,
          image.name, "sh", "-c", command]
    returncode, output = ContainerEngine.run_captured(cmd)

    lines = output.splitlines()
    truncated = len(lines) > BASH_OUTPUT_MAX_LINES
    if truncated:
        lines = lines[-BASH_OUTPUT_MAX_LINES:]
    text = "\n".join(lines)
    if truncated:
        text = ("... (truncated to the last %d lines -- narrow the "
                "command instead of asking for everything at once)\n"
                % BASH_OUTPUT_MAX_LINES) + text
    if returncode != 0:
        text += "\n(exit status %d)" % returncode
    return text

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
        from seine.cache       import human, size_of
        from seine.cache_index import since
        directory = ContainerEngine.workbench()
        width = max(len(name) for name, _ in found)
        for name, entry in found:
            size = human(size_of(os.path.join(directory, entry["dir"])))
            print("%-*s  %-12s  %-10s  %8s  used %s" % (
                width, name, entry["version"] or "?", entry["release"],
                size, since(entry.get("accessed_at"))))

    # Loads SPEC far enough to reach 'distribution', without touching a
    # container.
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
