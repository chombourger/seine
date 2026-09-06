# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# source-list/pull/rm: apt:// sources pulled into the workbench.

import os

from seine.utils import ContainerEngine

from . import Preview, Tool, _no_args, _single_group, NO_SINGLE_GROUP

# 'source-list' reads no active spec, same reasoning as 'gist-list':
# a pulled source outlives whichever build asked for it.
def _tool_source_list(app, arguments):
    from seine import sources
    found = sources.list_pulled()
    if not found:
        return "no sources pulled yet -- %s" % ContainerEngine.workbench()
    from seine.cache       import human, size_of
    from seine.cache_index import since
    directory = ContainerEngine.workbench()
    lines = []
    for name, entry in found:
        size = human(size_of(os.path.join(directory, entry["dir"])))
        lines.append("%s %s (%s, %s, used %s) -- %s" % (
            name, entry["version"] or "?", entry["release"], size,
            since(entry.get("accessed_at")),
            os.path.join(directory, entry["dir"])))
    return "\n".join(lines)

# No diff to show ahead of a fetch (nothing local changes until
# 'apt-get source' actually runs) -- this only says what would run and
# refuses early what pull() would refuse anyway, same "check without
# the side effect" spirit spec-update's own preview follows.
def _source_pull_preview(app, arguments):
    package = arguments.get("package")
    if not package:
        return Preview(False, "source-pull needs 'package' ('name' or "
                       "'name=version', apt's own syntax)")
    build = _single_group(app)
    if build is None:
        return Preview(False, NO_SINGLE_GROUP)
    from seine import sources
    name, version = sources.parse(package)
    existing = dict(sources.list_pulled()).get(name)
    if existing:
        return Preview(False, "'%s' is already pulled (as %s) -- "
                       "source-rm it first" % (name, existing["dir"]))
    distro = build.spec["distribution"]
    return Preview(True, "would fetch %s against %s %s's own feeds into "
                   "%s/%s" % (package, distro["source"], distro["release"],
                             ContainerEngine.workbench(), name))

def _tool_source_pull(app, arguments):
    build = _single_group(app)
    if build is None:
        return NO_SINGLE_GROUP
    package = arguments.get("package")
    if not package:
        return "source-pull needs 'package' ('name' or 'name=version')"
    from seine import sources
    try:
        dirname = sources.pull(package, build.spec["distribution"])
    except ValueError as e:
        return "could not pull: %s" % e
    return "pulled %s into %s" % (package,
                                  os.path.join(ContainerEngine.workbench(), dirname))

def _tool_source_rm_preview(app, arguments):
    name = arguments.get("name")
    if not name:
        return Preview(False, "source-rm needs 'name'")
    from seine import sources
    entry = dict(sources.list_pulled()).get(name)
    if entry is None:
        return Preview(False, "no pulled source named '%s'" % name)
    from seine.cache import human, size_of
    path = os.path.join(ContainerEngine.workbench(), entry["dir"])
    return Preview(True, "would remove %s (%s, %s %s) -- any change made "
                   "to it goes with it" % (path, human(size_of(path)),
                                           entry["version"] or "?", entry["release"]))

def _tool_source_rm(app, arguments):
    name = arguments.get("name")
    if not name:
        return "source-rm needs 'name'"
    from seine import sources
    try:
        sources.remove(name)
    except ValueError as e:
        return "could not remove: %s" % e
    return "removed %s" % name

TOOLS = [
    Tool("source-list", "List package sources already pulled into the "
        "workbench -- name, version, release, size on disk, how long "
        "since 'read'/'bash' last looked at it, and where it landed. "
        "Check this before pulling one that sounds like it may already "
        "be there, or to find ones worth source-rm-ing (large, long "
        "unused).", _no_args(), False, _tool_source_list),
    Tool("source-pull", "Fetch a package's source into the workbench, "
        "unpacked and ready for 'read'/'bash' -- the way to check what a "
        "package actually does (a config option, a patch, a default) "
        "rather than answer from training data, which may not match the "
        "version this image actually installs. 'package' is 'name' or "
        "'name=version' (apt's own syntax); give an exact version when "
        "one is known -- an SBOM (seine build --sbom), if read, names "
        "the one actually installed -- otherwise this resolves 'name' "
        "against the active build's own feeds, which may differ from "
        "what is installed. Refused if 'name' is already pulled.",
        {"type": "object",
         "properties": {"package": {"type": "string",
                                    "description": "'name' or 'name=version'"}},
         "required": ["package"]},
        True, _tool_source_pull, _source_pull_preview),
    Tool("source-rm", "Remove a pulled source and everything under it -- "
        "a person reviews what will be deleted (size, version) before it "
        "is, same as any other consequential action.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a source name, from source-list"}},
         "required": ["name"]},
        True, _tool_source_rm, _tool_source_rm_preview),
]
