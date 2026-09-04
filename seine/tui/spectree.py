# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Always-visible left-hand pane: the active context's merged spec(s)
# ('build.spec', the same dict '/plan'/'/artifacts' etc. already read),
# as a collapsed Tree -- a map of shape, not meant to be read top to
# bottom. One root per active group, named via multiconfig._label().

from rich.text import Text
from textual.widgets import Tree

from seine import multiconfig
from seine.utils import redact as redact_value
from seine.utils import redactions

# A list item's own name, matched the same fields BuildCmd.diff() uses
# (name/label/suite/package), so an entry reads the same here as there.
def _item_label(item, index):
    if isinstance(item, dict):
        for key in ("name", "label", "suite", "package"):
            if key in item:
                return str(item[key])
    # '[0]', not '0' -- a bare number reads like a value, not a position.
    return "[%d]" % index

# 'old': this key/item's value before /side-load (or /side-unload)
# changed the active spec, or NO_DIFF when the caller isn't diffing at
# all. MISSING means the key/item is entirely new.
NO_DIFF = object()
MISSING = object()

def _populate(node, key, value, old, changed, redact):
    diffing = old is not NO_DIFF
    if isinstance(value, dict):
        branch = node.add(str(key), data=str(key))
        if diffing:
            if old is MISSING or not isinstance(old, dict):
                changed.append(branch)
                old = {}
        for k, v in value.items():
            # '_'-prefixed: seine's own bookkeeping (_origins), never
            # shown -- same convention BuildCmd.dump() reads.
            if isinstance(k, str) and k.startswith("_"):
                continue
            child_old = old.get(k, MISSING) if diffing else NO_DIFF
            _populate(branch, k, v, child_old, changed, redact)
    elif isinstance(value, list):
        branch = node.add(str(key), data=str(key))
        # Matched by the same label as _item_label(), so reordering or
        # an unrelated earlier change doesn't mark every later item too.
        old_by_label = None
        if diffing:
            old_list = old if isinstance(old, list) else []
            old_by_label = {
                (_item_label(o, i) if isinstance(o, dict) else str(o)): o
                for i, o in enumerate(old_list)
            }
        for index, item in enumerate(value):
            if isinstance(item, (dict, list)):
                label = _item_label(item, index)
                child_old = old_by_label.get(label, MISSING) if diffing else NO_DIFF
                _populate(branch, label, item, child_old, changed, redact)
            else:
                text = str(redact(item))
                leaf = branch.add_leaf(text, data=text)
                if diffing and str(item) not in old_by_label:
                    changed.append(leaf)
    else:
        # Diffed on the real value, redacted only for display -- a
        # secret that changed still shows as changed even though both
        # sides render the same '<redacted:...>' placeholder.
        text = "%s: %s" % (key, redact(value))
        leaf = node.add_leaf(text, data=text)
        if diffing and (old is MISSING or old != value):
            changed.append(leaf)

# Each 'multiconfig:' group's own resolved spec, nested under a
# 'multiconfig' branch and keyed by its declared name -- not diffed
# against a previous build, since /side-load has no way to reach a
# sub-build's own file list yet.
def _populate_multiconfig(node, subbuilds, changed):
    branch = node.add("multiconfig", expand=True, data="multiconfig")
    for name, subbuild in subbuilds.items():
        label = multiconfig._label(subbuild, name=name)
        subgroup = branch.add(label, expand=True, data=label)
        patterns = redactions(subbuild.spec)
        redact = lambda value, patterns=patterns: redact_value(value, patterns)
        for key, value in subbuild.spec.items():
            if isinstance(key, str) and key.startswith("_"):
                continue
            section_redact = (lambda v: v) if key == "redact" else redact
            _populate(subgroup, key, value, NO_DIFF, changed, section_redact)

# Prefixed onto a node's label while a running build is touching it --
# a separate axis from the BUILD OUTPUT tasklist's own step marks (one
# step can touch several nodes, or none).
ACTIVE_MARK = "▶ "
ACTIVE_STYLE = "bold orange1"

# What /side-load or /side-unload just changed, a different axis again.
# If a node is somehow both, active wins -- what's happening now over
# what changed a moment ago.
CHANGED_MARK = "+ "
CHANGED_STYLE = "bold cyan1"

class SpecTree(Tree):
    def __init__(self, **kwargs):
        super().__init__("spec", **kwargs)
        self.show_root = False
        # One path per active key (a seine Task name, or build.py's own
        # Ansible play/task key), so --jobs > 1 lights several branches
        # at once. Innermost node last in each path.
        self._active = {}
        # Refcounts: how many active keys still need a node
        # expanded/marked, so a shared ancestor stays open until every
        # key needing it is gone. Two dicts since expansion applies to
        # a whole path, marking only to its leaf.
        self._expanded = {}
        self._marked = {}
        # Nodes /side-load or /side-unload marked changed/new, rebuilt
        # wholesale by every load() -- not refcounted like _marked above.
        self._changed = set()

    # Rebuilt from scratch every call -- a spec tree is small enough
    # that diffing old against new isn't worth it.
    #
    # previous_spec: the active group's build.spec just before the most
    # recent /side-load or /side-unload, or None otherwise. Only ever
    # diffed against the first group -- both refuse more than one
    # active group.
    def load(self, context, previous_spec=None):
        self.clear()
        self._active = {}
        self._expanded = {}
        self._marked = {}
        self._changed = set()
        if not context.active:
            text = "no active specification -- '/use SPEC...'"
            self.root.add_leaf(text, data=text)
            return
        changed = []
        for index, build in enumerate(context.builds):
            # expand=True: one group is the common case; a collapsed
            # single root would need an extra Enter to show anything.
            label = multiconfig._label(build)
            group = self.root.add(label, expand=True, data=label)
            # Same redact: patterns/substitution 'seine plan'/'--dump'
            # already apply -- one redaction rule, not a second one
            # invented for the TUI.
            patterns = redactions(build.spec)
            redact = lambda value, patterns=patterns: redact_value(value, patterns)
            old = previous_spec if (previous_spec is not None and index == 0) else NO_DIFF
            for key, value in build.spec.items():
                if isinstance(key, str) and key.startswith("_"):
                    continue
                # A spec's own 'multiconfig:' key names sub-builds by
                # group -- rendered as each group's own resolved spec
                # (build.subbuilds), not the raw file list it names.
                if key == "multiconfig" and build.subbuilds:
                    _populate_multiconfig(group, build.subbuilds, changed)
                    continue
                child_old = old.get(key, MISSING) if old is not NO_DIFF else NO_DIFF
                # 'redact' itself is shown as written, same exclusion
                # BuildCmd.dump() makes.
                section_redact = (lambda v: v) if key == "redact" else redact
                _populate(group, key, value, child_old, changed, section_redact)
        for node in changed:
            self._changed.add(node)
            self._render(node)
            self._reveal(node)

    # Matched on 'data' (the label a node was built with), not the
    # currently rendered/marked text.
    @staticmethod
    def _child(node, label):
        for child in node.children:
            if child.data == label:
                return child
        return None

    # 'labels': a path from the single active group's root -- /build
    # only ever runs one. Resolved as far as it goes: a name with
    # nothing to match degrades to whatever prefix did match.
    #
    # 'key' is whatever the caller tracks this path under; a shared
    # node stays expanded/marked as long as any key still needs it.
    # Idempotent per key, so a screen can call this every tick without
    # flicker.
    def set_active(self, key, labels):
        if len(self.root.children) == 0:
            self.clear_active(key)
            return
        node = self.root.children[0]
        path = []
        for label in labels:
            child = self._child(node, label)
            if child is None:
                break
            path.append(child)
            node = child
        if len(path) == 0:
            self.clear_active(key)
            return
        old = self._active.get(key, [])
        if path == old:
            return
        if old:
            self._unmark(old[-1])
        shared = 0
        for a, b in zip(old, path):
            if a is not b:
                break
            shared += 1
        for node in reversed(old[shared:]):
            self._release(node)
        for node in path[shared:]:
            self._acquire(node)
        self._mark(path[-1])
        self._active[key] = path

    def clear_active(self, key):
        old = self._active.pop(key, None)
        if not old:
            return
        self._unmark(old[-1])
        for node in reversed(old):
            self._release(node)

    # Keys with a path currently active, so a caller tracking its own
    # running tasks can retire the ones no longer active.
    def active_keys(self):
        return list(self._active.keys())

    # The node a given key's path currently ends at, or None.
    def leaf(self, key):
        path = self._active.get(key)
        return path[-1] if path else None

    def _acquire(self, node):
        self._expanded[node] = self._expanded.get(node, 0) + 1
        node.expand()

    def _release(self, node):
        count = self._expanded.get(node, 0) - 1
        if count <= 0:
            self._expanded.pop(node, None)
            node.collapse()
        else:
            self._expanded[node] = count

    def _mark(self, node):
        self._marked[node] = self._marked.get(node, 0) + 1
        self._render(node)

    def _unmark(self, node):
        count = self._marked.get(node, 0) - 1
        if count > 0:
            self._marked[node] = count
        else:
            self._marked.pop(node, None)
        self._render(node)

    # Expands every ancestor of 'node' (expand() reveals a node's own
    # children, and it's 'node' itself that needs revealing in its
    # parent). A whole new section marks every node under it too, so it
    # opens up in full rather than one collapsed node the user has to
    # click into.
    def _reveal(self, node):
        parent = node.parent
        while parent is not None:
            parent.expand()
            parent = parent.parent

    # A node's label is set from its own stable 'data' plus whichever
    # mark applies -- active beats changed beats neither.
    def _render(self, node):
        text = node.data
        if self._marked.get(node, 0) > 0:
            node.set_label(Text(ACTIVE_MARK + text, style=ACTIVE_STYLE))
        elif node in self._changed:
            node.set_label(Text(CHANGED_MARK + text, style=CHANGED_STYLE))
        else:
            node.set_label(text)

# What each seine Task is doing, in top-level branch labels. Steps left
# out (tarball/sbom/appliance) package what an earlier step already
# built, not their own bit of spec -- nothing lights up for them.
TASK_BRANCHES = {
    "bootstrap-host": ("distribution",),
    "bootstrap-target": ("distribution",),
    "packages-prepare": ("packages",),
    "packages": ("packages",),
    "rootfs": ("playbook",),
    "disk": ("image",),
    "image": ("image",),
}

# Every per-package/per-source task name packages.Builder.tasks() gives
# itself. With --jobs > 1 several can run at once; all still light up
# the same 'packages' branch.
PACKAGE_TASK_PREFIXES = ("package:", "prepare:", "deploy:", "fetch:", "fetch-upstream:")

def _branch_for(name):
    branch = TASK_BRANCHES.get(name)
    if branch:
        return branch
    if name.startswith(PACKAGE_TASK_PREFIXES):
        return ("packages",)
    # A spec's own 'multiconfig:' group namespaces its tasks
    # ("<group>:<name>", tasks.namespaced()) -- resolved one level in,
    # then nested under that group's own branch in the tree.
    if ":" in name:
        prefix, rest = name.split(":", 1)
        branch = _branch_for(rest)
        if branch:
            return ("multiconfig", prefix) + branch
    return None

# A separate key from the plain task name, so the coarse rootfs ->
# playbook highlight and the finer Ansible play/task one don't fight
# over the same dict entry.
ROOTFS_ANSIBLE_KEY = "rootfs:ansible"

# Several branches can be lit at once (--jobs > 1); auto-scroll picks
# one -- packages first (what --jobs actually parallelises), then
# playbook/Ansible, everything else last. Ties break on key name so the
# choice doesn't jump around tick to tick.
def _priority(key):
    if key in (ROOTFS_ANSIBLE_KEY, "rootfs"):
        return 1
    if _branch_for(key) == ("packages",):
        return 0
    return 2

# Highlights every Task actually running, refined to the Ansible
# play/task for rootfs specifically. Shared by every screen (BaseScreen's
# tick), not only Build's, so a build kept running while the person
# navigated elsewhere still lights up wherever their spec tree is.
# Returns the keys now wanted active, for scroll_to_active() below.
def highlight_active(tree, state):
    if state.done:
        for key in tree.active_keys():
            tree.clear_active(key)
        return set()
    running = {name for name, row in state.rows.items()
              if row["state"] == "running"}
    wanted = set()
    for name in running:
        if name == "rootfs" and state.play:
            key, labels = ROOTFS_ANSIBLE_KEY, ["playbook", state.play]
            if state.ansible_task:
                labels += ["tasks", state.ansible_task]
        else:
            # The specific packages: [i] entry when resolvable, falling
            # back to the whole branch otherwise.
            path = state.package_paths.get(name)
            if path is not None:
                key, labels = name, list(path)
            else:
                branch = _branch_for(name)
                if branch is None:
                    continue
                key, labels = name, list(branch)
        wanted.add(key)
        tree.set_active(key, labels)
    for key in tree.active_keys():
        if key not in wanted:
            tree.clear_active(key)
    return wanted

# A running test's spec-tree path, keyed by its fully qualified Robot
# name ('<suite>.<case name>', the same key TestState.rows uses).
# suite_name() (seine.testing.loader) decides the '<suite>' half,
# imported lazily so core TUI never pulls in the optional 'test' extra
# just to compute a name.
def test_paths(spec):
    from seine.testing.loader import suite_name
    entries = (spec or {}).get("test") or []
    name = suite_name(entries)
    paths = {}
    for i, entry in enumerate(entries):
        entry_label = _item_label(entry, i)
        for j, case in enumerate(entry.get("tests", [])):
            qualified = "%s.%s" % (name, case.get("name", "[%d]" % j))
            paths[qualified] = ["test", entry_label, "tests", _item_label(case, j)]
    return paths

# highlight_active()'s own shape, for TestState instead of BuildState:
# 'state.rows' is the same {name: {"state": ...}} shape either way, but
# a test's path comes from 'state.test_paths' (precomputed at reset())
# rather than a package/Ansible-specific lookup -- clears only the keys
# this function itself ever sets (state.rows' own), not every active
# key in the tree, so a build highlighted at the same time is untouched.
def highlight_active_test(tree, state):
    if state.done:
        for key in list(state.rows):
            tree.clear_active(key)
        return set()
    wanted = set()
    for name, row in state.rows.items():
        if row["state"] != "running":
            continue
        path = state.test_paths.get(name)
        if path is None:
            continue
        tree.set_active(name, path)
        wanted.add(name)
    for key in state.rows:
        if key not in wanted:
            tree.clear_active(key)
    return wanted

# Auto-scrolls to whichever active node _priority() picks, only when the
# pick changed since the last call -- doesn't fight a user who scrolled
# elsewhere. 'scrolled_to' is the caller's own memory, passed in and
# returned rather than kept here.
def scroll_to_active(tree, wanted, scrolled_to):
    if len(wanted) == 0:
        return None
    key = min(wanted, key=lambda k: (_priority(k), k))
    node = tree.leaf(key)
    if node is not None and node is not scrolled_to:
        tree.scroll_to_node(node)
        return node
    return scrolled_to
