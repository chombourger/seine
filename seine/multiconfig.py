# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Several images built together, under one scheduler -- 'seine build
# a.yml -- b.yml -- c.yml' -- instead of one 'seine build' per image.
# Sharing is inferred, not declared: two groups agreeing on a release and
# architecture share one package build; agreeing on a release alone still
# shares one host bootstrap. A single group (no '--') never reaches this
# module -- BuildCmd.main() takes that path exactly as it always has.

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
from seine.utils     import ContainerEngine, locked

# The command line's file arguments, split on '--' into the groups they
# name. Not getopt's own end-of-options handling: options are already
# behind the first filename by the time this is called (see
# BuildCmd.main()), so every '--' here is one of these separators.
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

# One group, loaded and parsed the way a single 'seine build' already
# does it. 'ansible_library' starts empty rather than shared with the
# options every group is given: it is filled in by load_all() itself, per
# fragment, and a list shared between groups would carry one group's
# fragments into another's ansible run.
def _load(files, options):
    build = BuildCmd()
    build.options = dict(options, ansible_library=[])
    # Named the way a top-level 'seine build' names it (BuildCmd.main()),
    # so a group with no 'image:' of its own can still name its tarball
    # after the file that asked for it (Image._rootfs_output()).
    build.options["files"] = files
    build.load_all(files)
    build.parse()
    return build

# What a group is called in a task name and in a message naming it: the
# output it would write, without its directory or extension. Distinct by
# construction -- see _check_filenames() -- and the one thing about a
# group a person building several of them together already has in mind.
#
# 'name' is a declared override -- a specification's own 'multiconfig:'
# key names its groups itself, rather than leaving them to whatever a
# group's own output happens to be called.
#
# A group with no 'image:' section (a vendor-only one -- see
# BuildCmd.parse()'s own comment) writes no output at all, so there is
# no filename to take this from; its own release is the next most
# distinct thing about it, and the TUI's Context.use() (unlike this
# module's own real multi-group callers, see the header comment) reaches
# this even for a single, image-less group.
def _label(build, name=None):
    if name is not None:
        return name
    if build.image._output is None:
        return build.spec["distribution"]["release"]
    return os.path.splitext(os.path.basename(build.image._output))[0]

# Two groups writing the same file is caught here rather than by whichever
# of them finishes last silently winning.
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

# What decides whether two groups may share one packages.Builder:
# Builder.architectures() reads this tuple's architecture directly, so
# groups agreeing on release but not architecture share only a release --
# see _cohort_label()/_check_builder_image_collisions() for what that
# alone still shares.
def _arch_key(build):
    distro = build.image.spec["distribution"]
    return (distro["source"], distro["release"], distro["architecture"])

def _cohort_label(key):
    _source, release, architecture = key
    return "%s-%s" % (release, architecture)

# Every package these groups asked for, once each. Two groups naming the
# same package are asking for one task only if they described it the same
# way -- see Package.same_as() -- or this would build only one of two
# different things asked for under one name, and hand it to both.
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

# BuilderImage names for the release alone (see its own defaultName()),
# unminimized unlike TargetBootstrap -- its digest still depends on every
# feed. Two groups sharing a release but differing feeds would retag
# 'builder/<source>/<release>' out from under whichever runs second,
# caught here up front instead.
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

# One arch-cohort's shared_tasks(), built from whichever member is asked
# first -- any will do, since shared_tasks() only reads 'distribution'/
# 'options', the same for every cohort member. The host bootstrap task is
# returned apart from the rest: the one name safe to collapse across
# cohorts too (HostBootstrap doesn't vary by architecture) -- merged_tasks()
# is what does that collapsing.
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

# Every group's tasks, merged into one graph for one tasks.run() to walk.
# Grouped into arch-cohorts for the shared half; one host bootstrap task
# survives across all of them (first cohort's kept, rest dropped, since
# HostBootstrap doesn't vary by architecture). Everything else is
# namespaced by cohort/group, so nothing else collides -- left bare when
# there is only one of it.
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

    # '--packages-only' stops here for every group, the same reason it
    # stops Image.tasks() there: what it is for has no use for own_tasks(),
    # and asking for it would build a target bootstrap nothing needs. A
    # global option, the same in every group's options -- see run().
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

# One group's own record: its own tasks plus whatever shared ones they
# stood on (tasks.ancestors()), keyed by that group's own spec_digest --
# not the whole invocation. Its own outcome too (tasks.succeeded()), not
# the invocation's: a group whose tasks all ran is 'ok' even if a
# sibling failed afterward. 'digest' is taken by the caller, before any
# task ran -- see run()'s own comment on why.
def _record_group(build, all_tasks, jobs, machine, digest):
    own = [t.name for t in all_tasks
          if t.name.startswith("%s:" % _label(build))]
    group_tasks = tasks.ancestors(all_tasks, own)
    ok = tasks.succeeded(group_tasks)
    analyze.record(group_tasks, digest, jobs=jobs, ok=ok, machine=machine)
    return ok

# The prune Image.build() does at the end of a single build, done once
# here instead of once per group: machine-wide either way, and a second
# one while the first still holds the lock is skipped, not queued.
def _prune():
    try:
        with locked(ContainerEngine.storage_lock(), blocking=False):
            ContainerEngine.run(["image", "prune", "-f"], check=False)
    except BlockingIOError:
        pass

# One directory for every group's logs, keyed by all of their files
# together rather than by any one group's -- see Image._logs(), which
# this otherwise matches.
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

# Everything 'seine build'/'seine plan' does for more than one group,
# vendoring BuildCmd.main()'s tail and Image.build() for a single one:
# parse every group, print or build the merged result, then the
# bookkeeping build() does once per image done once here for all of them.
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

    # Taken before the build, which writes into each spec as it goes --
    # see Image.build()'s own comment on this. The two digests below are
    # the same story: 'disk' (PartitionHandler.compute_sizes(), inside
    # each group's own tasks) writes '_size'/'_start_mib'/'_end_mib'
    # straight onto the partition/volume dicts each spec holds, so a
    # digest taken after tasks.run() would never match what reloading
    # these same files fresh, un-run, computes.
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
