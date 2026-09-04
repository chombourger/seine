#!/usr/bin/env python3

import asyncio
import avocado
import contextlib
import os
import sys
import threading

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from tests.native_image import native_image

NATIVE_IMAGE = native_image()

def _run(scenario):
    asyncio.run(scenario())

# Same '_tui_required' guard tests/tui/tui.py uses for anything that
# needs the 'tui' extra (textual). Kept self-contained here rather than
# imported from tui.py, same reason tui.py gives for not sharing helpers
# across test files.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# Two independently-packaged groups sharing one disk -- the same spec
# shape as tests/image/multiconfig.py's TwoSourcesOnOneDisk, minus the
# playbook/packages content, since nothing here actually builds
# (SpecTree only ever reads a parsed spec).
def _write_two_group_spec(workdir):
    def _group(name):
        path = os.path.join(workdir, "%s.yaml" % name)
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "    release: trixie\n"
                "    architecture: amd64\n"
                "image:\n"
                "    filename: %s.img\n"
                "    table: gpt\n"
                "    partitions:\n"
                "        - label: rootfs\n"
                "          where: /\n"
                "          size: 256MiB\n"
                % name)
        return path
    main = _group("main")
    recovery = _group("recovery")
    outer = os.path.join(workdir, "outer.yaml")
    with open(outer, "w") as f:
        f.write(
            "distribution:\n"
            "    release: trixie\n"
            "    architecture: amd64\n"
            "multiconfig:\n"
            "    main:\n"
            "        - %s\n"
            "    recovery:\n"
            "        - %s\n"
            "image:\n"
            "    filename: disk.img\n"
            "    table: gpt\n"
            "    partitions:\n"
            "        - label: main-root\n"
            "          source: main\n"
            "          where: /\n"
            "          size: 256MiB\n"
            "        - label: recovery-root\n"
            "          source: recovery\n"
            "          where: /\n"
            "          size: 256MiB\n"
            % (main, recovery))
    return outer

def _child(node, label):
    for child in node.children:
        if child.data == label:
            return child
    return None

# Descends 'labels' from 'node', asserting each step actually matched --
# a broken chain fails at the label that went missing, not with an
# AttributeError three calls later.
def _descend(test, node, *labels):
    for label in labels:
        child = _child(node, label)
        test.assertIsNotNone(child, "no '%s' branch under '%s'" % (label, node.data))
        node = child
    return node

# What SpecTree.load() does with a spec's own 'multiconfig:' key -- no
# running App needed, same style as tests/tui/tui.py's
# ActiveSpecification/Rendering classes.
class NestedGroups(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui.context import Context
            from seine.tui.spectree import SpecTree
        self.Context = Context
        self.SpecTree = SpecTree
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir

    def _tree(self, files):
        context = self.Context()
        context.use(files)
        tree = self.SpecTree()
        tree.load(context)
        return tree

    def test_groups_render_as_nested_branches_with_own_partitions(self):
        outer = _write_two_group_spec(self.workdir)
        tree = self._tree([outer])
        root = tree.root.children[0]
        for name in ("main", "recovery"):
            group = _descend(self, root, "multiconfig", name)
            root_part = _descend(self, group, "image", "partitions", "rootfs")
            self.assertIsNotNone(_child(root_part, "where: /"),
                                 "%s's own 'where: /' leaf missing" % name)

    # source:/where: on the outer disk's own partitions -- unaffected by
    # nesting each group's own spec under 'multiconfig', still exactly
    # where the generic walker always put them.
    def test_outer_partitions_show_which_group_they_source_from(self):
        outer = _write_two_group_spec(self.workdir)
        tree = self._tree([outer])
        root = tree.root.children[0]
        main_root = _descend(self, root, "image", "partitions", "main-root")
        self.assertIsNotNone(_child(main_root, "source: main"))
        self.assertIsNotNone(_child(main_root, "where: /"))

    def test_a_single_build_spec_is_unaffected(self):
        tree = self._tree([NATIVE_IMAGE])
        root = tree.root.children[0]
        self.assertIsNone(_child(root, "multiconfig"))

# Full app, real Textual event loop -- highlight_active()/_branch_for()
# only prove they route a namespaced task name to the right subtree when
# a build is actually running and ticking the tree, same as
# tests/tui/tui.py's own spectree-highlighting test.
class HighlightsNamespacedGroupTasks(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.image import Image
            from seine.tui.app import OverviewScreen, SeineApp
            from seine.tui.spectree import SpecTree
        self.Image = Image
        self.OverviewScreen = OverviewScreen
        self.SeineApp = SeineApp
        self.SpecTree = SpecTree
        self.real_build = Image.build
        self.addCleanup(setattr, Image, "build", self.real_build)
        from seine import tasks
        self.addCleanup(tasks._interrupted.clear)
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_HISTORY_FILE"] = os.path.join(self.workdir, "history.json")

    def tearDown(self):
        os.environ.pop("SEINE_HISTORY_FILE", None)

    def test_a_groups_own_task_highlights_under_its_own_branch_only(self):
        from seine import tasks

        proceed = threading.Event()

        def gated_build(image, reporter=None):
            for step in tasks.ordered(image.tasks()):
                reporter.started(step.name)
                proceed.wait()
                proceed.clear()
                reporter.finished(step.name, failed=False)
        self.Image.build = gated_build

        outer = _write_two_group_spec(self.workdir)

        async def scenario():
            app = self.SeineApp(files=[outer])
            async with app.run_test() as pilot:
                # In a finally: run_test()'s teardown joins the worker
                # thread, and a failed assertion would otherwise leave
                # it blocked on proceed.wait() forever.
                try:
                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/build"
                    await pilot.press("enter")
                    await pilot.pause()
                    for _ in range(400):
                        if app.build_state.current == "recovery:rootfs":
                            break
                        proceed.set()
                        await asyncio.sleep(0.01)
                    self.assertEqual(app.build_state.current, "recovery:rootfs")

                    prompt = app.screen.query_one("#prompt")
                    prompt.value = "/overview"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, self.OverviewScreen)
                    tree = app.screen.query_one(self.SpecTree)
                    for _ in range(100):
                        if tree.active_keys():
                            break
                        await asyncio.sleep(0.02)
                        await pilot.pause()
                    self.assertTrue(tree.active_keys())

                    root = tree.root.children[0]
                    recovery = _descend(self, root, "multiconfig", "recovery")
                    main = _descend(self, root, "multiconfig", "main")
                    leaf = tree.leaf("recovery:rootfs")
                    self.assertIsNotNone(leaf, "recovery:rootfs never became active")
                    ancestors = set()
                    node = leaf
                    while node is not None:
                        ancestors.add(node)
                        node = node.parent
                    self.assertIn(recovery, ancestors)
                    self.assertNotIn(main, ancestors)
                finally:
                    for _ in range(400):
                        if not app.build_state.running:
                            break
                        proceed.set()
                        await asyncio.sleep(0.01)
                self.assertTrue(app.build_state.done)
        _run(scenario)

if __name__ == "__main__":
    avocado.main()
