#!/usr/bin/env python3

import avocado
import contextlib
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

# Same '_tui_required' guard tests/tui/spectree.py uses -- kept
# self-contained here rather than imported, same reason spectree.py
# gives for not sharing helpers across test files.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

def _write(workdir, name, content):
    path = os.path.join(workdir, name)
    with open(path, "w") as f:
        f.write(content)
    return path

def _write_group(workdir, name):
    return _write(workdir, "%s.yaml" % name,
        "distribution:\n"
        "    release: trixie\n"
        "    architecture: amd64\n")

# Same two-group shape as tests/tui/spectree.py's own fixture, minus the
# 'image:' section this file never needs.
def _write_two_group_spec(workdir):
    main = _write_group(workdir, "main")
    recovery = _write_group(workdir, "recovery")
    return _write(workdir, "outer.yaml",
        "distribution:\n"
        "    release: trixie\n"
        "    architecture: amd64\n"
        "multiconfig:\n"
        "    main:\n"
        "        - %s\n"
        "    recovery:\n"
        "        - %s\n"
        % (main, recovery)), main, recovery

class SideLoadTargetsANamedGroup(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
        self.Context = Context
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def test_side_load_appends_only_to_the_named_groups_files(self):
        outer, main, recovery = _write_two_group_spec(self.workdir)
        context = self.Context()
        context.use([outer])
        fragment = _write(self.workdir, "extra.yaml",
            "playbook:\n    - name: extra play\n      tasks: []\n")

        context.side_load(fragment, group="recovery")

        build = context.builds[0]
        self.assertEqual(build.spec["multiconfig"]["recovery"], [recovery, fragment])
        self.assertEqual(build.spec["multiconfig"]["main"], [main])
        self.assertEqual(len(build.subbuilds["recovery"].spec.get("playbook") or []), 1)
        self.assertIsNone(build.subbuilds["main"].spec.get("playbook"))
        self.assertIsNone(build.spec.get("playbook"))

    def test_side_load_reparses_only_the_named_sub_build(self):
        outer, main, recovery = _write_two_group_spec(self.workdir)
        context = self.Context()
        context.use([outer])
        before_main = context.builds[0].subbuilds["main"]
        fragment = _write(self.workdir, "extra.yaml",
            "playbook:\n    - name: extra play\n      tasks: []\n")

        context.side_load(fragment, group="recovery")

        self.assertIs(context.builds[0].subbuilds["main"], before_main)

    def test_side_load_rejects_an_undeclared_group(self):
        outer, main, recovery = _write_two_group_spec(self.workdir)
        context = self.Context()
        context.use([outer])
        fragment = _write(self.workdir, "extra.yaml",
            "distribution:\n    architecture: amd64\n")

        with self.assertRaises(ValueError):
            context.side_load(fragment, group="does-not-exist")

    def test_side_unload_drops_only_from_the_named_groups_files(self):
        outer, main, recovery = _write_two_group_spec(self.workdir)
        context = self.Context()
        context.use([outer])
        fragment = _write(self.workdir, "extra.yaml",
            "playbook:\n    - name: extra play\n      tasks: []\n")
        context.side_load(fragment, group="recovery")

        context.side_unload(fragment, group="recovery")

        build = context.builds[0]
        self.assertEqual(build.spec["multiconfig"]["recovery"], [recovery])
        self.assertIsNone(build.subbuilds["recovery"].spec.get("playbook"))

    # The group=None path is exactly what it was before group= existed --
    # re-run, not just trusted, against a plain single-group spec.
    def test_side_load_and_unload_with_no_group_are_unaffected(self):
        main = _write_group(self.workdir, "main")
        context = self.Context()
        context.use([main])
        fragment = _write(self.workdir, "extra.yaml",
            "playbook:\n    - name: extra play\n      tasks: []\n")

        context.side_load(fragment)
        self.assertEqual(len(context.builds[0].spec.get("playbook")), 1)

        context.side_unload(fragment)
        self.assertIsNone(context.builds[0].spec.get("playbook"))

    # A different axis (CLI '--' groups) than group= -- still refused
    # exactly as before, group or no group.
    def test_a_cli_multi_group_session_still_refuses(self):
        main = _write_group(self.workdir, "main")
        other = _write_group(self.workdir, "other")
        context = self.Context()
        context.use([main, "--", other])

        with self.assertRaises(ValueError):
            context.side_load("whatever.yaml")
        with self.assertRaises(ValueError):
            context.side_load("whatever.yaml", group="main")

if __name__ == "__main__":
    avocado.main()
