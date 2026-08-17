# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Always-visible left-hand pane: the active context's merged spec(s)
# ('build.spec', the same dict '/plan'/'/artifacts' etc. already read),
# as a collapsed Tree -- a map of shape, not meant to be read top to
# bottom. One root per active group, named via multiconfig._label().

from rich.text import Text
from textual.widgets import Tree

from seine import multiconfig

# A list item's own name, matched the same fields BuildCmd.diff() uses
# (name/label/suite/package), so an entry reads the same here as there.
def _item_label(item, index):
    if isinstance(item, dict):
        for key in ("name", "label", "suite", "package"):
            if key in item:
                return str(item[key])
    # '[0]', not '0' -- a bare number reads like a value, not a position.
    return "[%d]" % index

# 'old': this key/item's value before /extend loaded one more fragment,
# or NO_DIFF when the caller isn't diffing at all. MISSING means the
# key/item is entirely new.
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

# Prefixed onto a node's label while a running build is touching it --
# a separate axis from the BUILD OUTPUT tasklist's own step marks (one
# step can touch several nodes, or none).
ACTIVE_MARK = "▶ "
ACTIVE_STYLE = "bold orange1"

# What /extend just added or changed, a different axis again. If a node
# is somehow both, active wins -- what's happening now over what
# changed a moment ago.
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
        # Nodes /extend marked changed/new, rebuilt wholesale by every
        # load() -- not refcounted like _marked above.
        self._changed = set()

    # Rebuilt from scratch every call -- a spec tree is small enough
    # that diffing old against new isn't worth it.
    #
    # previous_spec: the active group's build.spec just before the most
    # recent /extend, or None otherwise. Only ever diffed against the
    # first group -- /extend refuses more than one active group.
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
            patterns = build.redactions(build.spec)
            redact = lambda value, build=build, patterns=patterns: build._redact(value, patterns)
            old = previous_spec if (previous_spec is not None and index == 0) else NO_DIFF
            for key, value in build.spec.items():
                if isinstance(key, str) and key.startswith("_"):
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
