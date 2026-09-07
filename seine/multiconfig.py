# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Build several images together under one scheduler:
# 'seine build a.yml -- b.yml -- c.yml'. Groups that agree on release and
# architecture share a package build; groups that only agree on release
# still share a host bootstrap. A single group (no '--') skips this module.

import contextlib
import os
import tempfile
import time

from seine import analyze
from seine import cache_index
from seine import progress
from seine import tasks
from seine import utils
from seine.bootstrap import HostBootstrap
from seine.build     import BuildCmd, remember
from seine.sbuild    import BuilderImage
from seine.container import ContainerEngine
from seine.utils import locked

# Split the file arguments on '--' into groups.
def split(args):
    groups, current = [], []
    for arg in args:
        if arg == "--":
            groups.append(current)
            current = []
        else:
            current.append(arg)
    groups.append(current)
    for group in groups:
        if len(group) == 0:
            raise ValueError(
                "'--' separates groups of specification files -- two of "
                "them with nothing between, or one at either end, leaves "
                "one group empty")
    return groups

# Load and parse one group, like a single 'seine build' would.
# 'ansible_library' starts empty per group so fragments don't leak
# between groups. 'defer_uki_check' skips the parse-time check that a
# named 'initrd:' is already deployed, for a group that runs 'after:'
# another one that deploys it in this same run.
def _load(files, options, defer_uki_check=False):
    build = BuildCmd()
    build.options = dict(options, ansible_library=[])
    build.options["files"] = files
    if defer_uki_check:
        build.options["defer_uki_check"] = True
    build.load_all(files)
    build.parse()
    return build

# A group is a bare file list, or {files:, after:, before:} naming other
# groups it must build after/before. Returns (files, after, before).
def _parse_group(name, value):
    if isinstance(value, list):
        return value, [], []
    if isinstance(value, dict) and isinstance(value.get("files"), list):
        after = value.get("after", [])
        before = value.get("before", [])
        for key, setting in (("after", after), ("before", before)):
            if isinstance(setting, list) == False:
                raise ValueError(
                    "'multiconfig: %s: %s' shall be a list of group names"
                    % (name, key))
        return value["files"], after, before
    raise ValueError(
        "'multiconfig: %s' shall be a list of specification files, or a "
        "mapping with a 'files:' list" % name)

# Fold 'before' into the group it names, so only one direction ('after')
# needs wiring later. Naming an undeclared group, or itself, is an error.
def resolve_order(parsed):
    after = {name: set(entry[1]) for name, entry in parsed.items()}
    for name, (_files, _after, before) in parsed.items():
        for other in before:
            _check_referenced(parsed, name, "before", other)
            after[other].add(name)
    for name, deps in after.items():
        for other in deps:
            _check_referenced(parsed, name, "after", other)
    _check_no_cycles(after)
    return after

def _check_referenced(parsed, name, setting, other):
    if other == name:
        raise ValueError("'multiconfig: %s: %s' names itself" % (name, setting))
    if other not in parsed:
        raise ValueError(
            "'multiconfig: %s: %s' names '%s', which is not one of the "
            "declared 'multiconfig:' groups (%s)"
            % (name, setting, other, ", ".join(sorted(parsed)) or "none"))

# Kahn's algorithm, to report a dependency cycle clearly instead of
# leaving it for the scheduler to fail with a vague deadlock error.
def _check_no_cycles(after):
    remaining = dict(after)
    while len(remaining) > 0:
        ready = [name for name, deps in remaining.items()
                 if len(deps & remaining.keys()) == 0]
        if len(ready) == 0:
            raise ValueError(
                "'multiconfig:' groups depend on each other in a circle: %s"
                % ", ".join(sorted(remaining)))
        for name in ready:
            del remaining[name]

# What a group is called in task names and messages: its output
# filename without directory or extension, or the declared 'name'
# override. A group with no 'image:' section writes no output, so its
# release is used instead.
def _label(build, name=None):
    if name is not None:
        return name
    if build.image._output is None:
        return build.spec["distribution"]["release"]
    return os.path.splitext(os.path.basename(build.image._output))[0]

# Catch two groups writing the same output file, rather than letting
# whichever finishes last silently win.
def _check_filenames(builds):
    seen = {}
    for build in builds:
        output = build.image._output
        if output in seen:
            raise ValueError(
                "'%s' and '%s' both write to '%s' -- give one of them a "
                "different 'image: filename:'"
                % (seen[output], _label(build), output))
        seen[output] = _label(build)

# Two groups share one packages.Builder only if they agree on this
# key (source, release, architecture).
def _arch_key(build):
    distro = build.image.spec["distribution"]
    return (distro["source"], distro["release"], distro["architecture"])

def _cohort_label(key):
    _source, release, architecture = key
    return "%s-%s" % (release, architecture)

# Every package these groups asked for, once each. Two groups naming
# the same package must describe it identically (Package.same_as()),
# or it's an error rather than silently building only one of them.
def _union(builds):
    merged = []
    by_name = {}
    for build in builds:
        for package in build.image.packages:
            found = by_name.get(package.name)
            if found is None:
                by_name[package.name] = (package, _label(build))
                merged.append(package)
            elif found[0].same_as(package) == False:
                raise ValueError(
                    "'%s' is described differently by '%s' and '%s' -- "
                    "give one of them a different name if they are meant "
                    "to be two packages"
                    % (package.name, found[1], _label(build)))
    return merged

# BuilderImage is named by release alone, but its digest depends on the
# apt feeds too. Catch two groups sharing a release with different feeds
# here, before one silently retags the other's image.
def _check_builder_image_collisions(builds):
    seen = {}
    for build in builds:
        distro = build.image.spec["distribution"]
        options = build.image.options
        host = HostBootstrap(distro, options)
        image = BuilderImage(distro, options)
        digest = image.digest(image.dockerfile(host), base=host.name)
        label = _label(build)
        found = seen.get(image.name)
        if found is not None and found[0] != digest:
            raise ValueError(
                "'%s' and '%s' would both build the builder image '%s', "
                "but with different apt sources -- give them the same "
                "feeds, or build one of them in a separate 'seine build'"
                % (found[1], label, image.name))
        seen.setdefault(image.name, (digest, label))

# One arch-cohort's shared_tasks(), built from any member (they all
# agree on 'distribution'/'options'). The host bootstrap task is
# returned separately since it can be shared across cohorts too.
def _cohort_tasks(members, prefix):
    requested = _union(members)
    image = members[0].image
    distro = image.spec["distribution"]
    hostBootstrap = HostBootstrap(distro, image.options)
    shared = image.shared_tasks(hostBootstrap=hostBootstrap, requested=requested)
    host_task, rest = shared[0], shared[1:]
    if prefix is not None:
        rest = tasks.namespaced(rest, prefix)
    return host_task, rest

# Merge every group's tasks into one graph for one tasks.run() to walk.
# Groups are bucketed into arch-cohorts for the shared half; only the
# first cohort's host bootstrap task is kept, since it doesn't vary by
# architecture. Everything else is namespaced by cohort/group to avoid
# collisions, left bare when there is only one of it.
def merged_tasks(builds):
    cohorts, order = {}, []
    for build in builds:
        key = _arch_key(build)
        if key not in cohorts:
            cohorts[key] = []
            order.append(key)
        cohorts[key].append(build)

    multi_cohort = len(cohorts) > 1
    merged = []
    host_added = False
    barrier = {}
    for key in order:
        prefix = _cohort_label(key) if multi_cohort else None
        host_task, rest = _cohort_tasks(cohorts[key], prefix)
        if host_added == False:
            merged.append(host_task)
            host_added = True
        merged += rest
        barrier[key] = "%s:packages" % prefix if prefix else "packages"

    # '--packages-only' stops here, same as Image.tasks() does: no need
    # for own_tasks() or a target bootstrap. Same option value in every
    # group, so checking builds[0] is enough.
    if builds[0].options.get("packages_only"):
        return merged

    for build in builds:
        image = build.image
        distro = image.spec["distribution"]
        hostBootstrap = HostBootstrap(distro, image.options)
        own = image.own_tasks(hostBootstrap=hostBootstrap,
                              needs_packages=barrier[_arch_key(build)])
        merged += tasks.namespaced(own, _label(build))
    return merged

# Record one group's own tasks (plus the shared ones it stood on) and
# its own outcome, keyed by that group's own spec_digest -- not the
# whole invocation's. A group is 'ok' if its tasks all ran, even if a
# sibling group failed afterward.
def _record_group(build, all_tasks, jobs, machine, digest):
    own = [t.name for t in all_tasks
          if t.name.startswith("%s:" % _label(build))]
    group_tasks = tasks.ancestors(all_tasks, own)
    ok = tasks.succeeded(group_tasks)
    analyze.record(group_tasks, digest, jobs=jobs, ok=ok, machine=machine)
    return ok

# Same prune Image.build() does after a single build, done once here
# for the whole run instead of once per group.
def _prune():
    try:
        with locked(ContainerEngine.storage_lock(), blocking=False):
            ContainerEngine.run(["image", "prune", "-f"], check=False)
    except BlockingIOError:
        pass

# One log directory for the whole run, keyed by all groups' files
# together rather than any single group's.
def _logs(groups_files):
    base = ContainerEngine.logs_root()
    os.makedirs(base, exist_ok=True)
    run = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    files = [f for group in groups_files for f in group]
    spec = os.path.join(base, utils.digest(files, 8))
    try:
        path = os.path.join(spec, run)
        os.makedirs(path)
        return path
    except FileExistsError:
        return tempfile.mkdtemp(dir=spec, prefix="%s-" % run)

# Multi-group version of what 'seine build'/'seine plan' does for one
# group: parse every group, print or build the merged result, then run
# the bookkeeping Image.build() normally does per image.
def run(groups_files, options):
    builds = [_load(files, options) for files in groups_files]
    _check_filenames(builds)
    _check_builder_image_collisions(builds)

    if options.get("build") == False:
        for build in builds:
            print(build.dump(build.spec))
        return 0

    if options.get("dry_run"):
        if options.get("spec", True):
            for build, files in zip(builds, groups_files):
                print(build.changed(files, build.spec))
        if options.get("tasks", True):
            print("\nsteps:")
            tasks.describe(merged_tasks(builds))
        return 0

    all_tasks = merged_tasks(builds)
    jobs = options.get("jobs", 1)
    verbose = options.get("verbose", False)

    for build in builds:
        release = build.image.spec["distribution"]["release"]
        cache_index.Index().hit(cache_index.DOWNLOADS, release)

    logs = None
    if verbose == False or jobs > 1:
        logs = _logs(groups_files)
        print("output under %s" % logs)

    display = None
    if verbose == False:
        display = progress.Display(total=len(all_tasks), environment=os.environ)

    # Taken before the build runs: it writes computed sizes back into
    # each spec, so a digest taken afterward wouldn't match a fresh,
    # un-run reload of these same files.
    recorded = [build.dump(build.spec) for build in builds]
    combined_digest = analyze.spec_digest({"groups": [build.spec for build in builds]})
    group_digests = {build: analyze.spec_digest(build.spec) for build in builds}
    ok = False
    group_ok = {}
    machine = analyze.watching()
    with locked(ContainerEngine.storage_lock(), shared=True):
        try:
            with machine, (display if display is not None
                           else contextlib.nullcontext()):
                tasks.run(all_tasks, jobs=jobs, logs=logs, verbose=verbose,
                         display=display)
            ok = True
        finally:
            analyze.record(all_tasks, combined_digest,
                           jobs=jobs, ok=ok, machine=machine)
            for build in builds:
                group_ok[build] = _record_group(
                    build, all_tasks, jobs, machine, group_digests[build])
    _prune()

    said = cache_index.summary()
    if said is not None:
        print(said)

    for build, files, dump in zip(builds, groups_files, recorded):
        if group_ok.get(build):
            remember(files, dump)
    return 0
