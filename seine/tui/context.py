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

    # One more fragment appended to the active group's file list, same
    # '--' composition as the real CLI.
    def side_load(self, fragment):
        self._one_active_group()
        previous_spec = self.builds[0].spec
        self.use(self.groups[0] + [fragment])
        self.changed_from = previous_spec

    # The reverse of side_load(): one fragment dropped back out of the
    # active group's file list and the rest reparsed. Works on any file
    # currently in the list, not only one side_load() itself added --
    # the list doesn't distinguish how a file got there, so neither does
    # this.
    def side_unload(self, fragment):
        self._one_active_group()
        files = self.groups[0]
        if fragment not in files:
            raise ValueError("'%s' isn't currently loaded" % fragment)
        previous_spec = self.builds[0].spec
        remaining = [f for f in files if f != fragment]
        self.use(remaining)
        self.changed_from = previous_spec

    @property
    def active(self):
        return self.builds is not None and len(self.builds) > 0

    # multiconfig._label() is the output filename without directory or
    # extension. Several groups show as 'a+b'.
    def label(self):
        if not self.active:
            return None
        return "+".join(multiconfig._label(build) for build in self.builds)
