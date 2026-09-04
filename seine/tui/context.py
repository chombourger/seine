# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The TUI's "active specification" -- what /use sets, and what
# Overview/Plan act on. Reuses multiconfig.split()'s own '--' grouping:
# 'use a.yaml -- b.yaml' means what 'seine build a.yaml -- b.yaml' means.

from seine import multiconfig
from seine.build import BuildCmd

class Context:
    def __init__(self):
        self.groups = None
        self.builds = None
        # Set only by side_load()/side_unload(): the active group's spec
        # just before the most recent one, for SpecTree.load() to diff
        # against. Cleared by use(), overwritten (not accumulated) by
        # the next call -- highlights only the last change.
        self.changed_from = None

    # Loads/parses each group the way 'seine build' does -- nothing is
    # built, so this is instant and side-effect-free.
    def use(self, args):
        groups = multiconfig.split(args)
        builds = []
        for files in groups:
            build = BuildCmd()
            build.options = dict(build.options, ansible_library=[])
            build.load_all(files)
            build.parse()
            builds.append(build)
        self.groups = groups
        self.builds = builds
        self.changed_from = None

    # Refuses more than one active group, same as /build and
    # /filesystem -- both this and side_unload() below.
    def _one_active_group(self):
        if not self.active:
            raise ValueError("no active specification -- '/use SPEC' first")
        if len(self.builds) != 1:
            raise ValueError(
                "side-load needs exactly one active group -- multi-group "
                "specifications ('/use a -- b') aren't supported here yet")

    # 'group's own declared file list (spec['multiconfig'][group]) -- the
    # thing both side_load()/side_unload() amend when targeting a named
    # sub-build instead of the outer spec.
    def _subbuild_files(self, build, group):
        groups = build.spec.get("multiconfig") or {}
        if group not in groups:
            raise ValueError(
                "'%s' is not one of the declared 'multiconfig:' groups (%s)"
                % (group, ", ".join(sorted(groups)) if groups else "none"))
        return groups[group]

    # Re-parses 'group' alone from 'files', in place -- the outer spec and
    # every other sub-build are untouched. 'build.subbuilds' and
    # 'build.image.subbuilds' are the same dict (BuildCmd._parse_
    # multiconfig()), so mutating one is enough.
    def _reload_subbuild(self, build, group, files):
        if not files:
            raise ValueError(
                "'%s' has no files left to load -- refusing to leave it "
                "empty" % group)
        build.spec["multiconfig"][group] = files
        previous_spec = build.subbuilds[group].spec
        build.subbuilds[group] = multiconfig._load(files, build.options)
        self.changed_from = previous_spec

    # One more fragment appended to the active group's file list, same
    # '--' composition as the real CLI -- or, with 'group' given, to that
    # named 'multiconfig:' sub-build's own file list instead, reparsing
    # only that sub-build.
    def side_load(self, fragment, group=None):
        self._one_active_group()
        build = self.builds[0]
        if group is None:
            previous_spec = build.spec
            self.use(self.groups[0] + [fragment])
            self.changed_from = previous_spec
            return
        files = self._subbuild_files(build, group)
        self._reload_subbuild(build, group, files + [fragment])

    # The reverse of side_load(): one fragment dropped back out of the
    # active group's (or, with 'group' given, the named sub-build's) file
    # list and the rest reparsed. Works on any file currently in the
    # list, not only one side_load() itself added -- the list doesn't
    # distinguish how a file got there, so neither does this.
    def side_unload(self, fragment, group=None):
        self._one_active_group()
        build = self.builds[0]
        if group is None:
            files = self.groups[0]
            if fragment not in files:
                raise ValueError("'%s' isn't currently loaded" % fragment)
            previous_spec = build.spec
            remaining = [f for f in files if f != fragment]
            self.use(remaining)
            self.changed_from = previous_spec
            return
        files = self._subbuild_files(build, group)
        if fragment not in files:
            raise ValueError(
                "'%s' isn't currently loaded in '%s'" % (fragment, group))
        self._reload_subbuild(build, group, [f for f in files if f != fragment])

    @property
    def active(self):
        return self.builds is not None and len(self.builds) > 0

    # multiconfig._label() is the output filename without directory or
    # extension. Several groups show as 'a+b'.
    def label(self):
        if not self.active:
            return None
        return "+".join(multiconfig._label(build) for build in self.builds)
