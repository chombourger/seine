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
        # Set only by extend(): the active group's spec just before the
        # most recent /extend, for SpecTree.load() to diff against.
        # Cleared by use(), overwritten (not accumulated) by the next
        # extend() -- highlights only the last fragment's changes.
        self.extended_from = None

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
        self.extended_from = None

    # One more fragment appended to the active group's file list, same
    # '--' composition as the real CLI. Refuses more than one active
    # group, same as /build and /filesystem.
    def extend(self, fragment):
        if not self.active:
            raise ValueError("no active specification -- '/use SPEC' first")
        if len(self.builds) != 1:
            raise ValueError(
                "extend needs exactly one active group -- multi-group "
                "specifications ('/use a -- b') aren't supported here yet")
        previous_spec = self.builds[0].spec
        self.use(self.groups[0] + [fragment])
        self.extended_from = previous_spec

    @property
    def active(self):
        return self.builds is not None and len(self.builds) > 0

    # multiconfig._label() is the output filename without directory or
    # extension. Several groups show as 'a+b'.
    def label(self):
        if not self.active:
            return None
        return "+".join(multiconfig._label(build) for build in self.builds)
