# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Filesystem screen: read-only browse of a built image via seine/inspect.py
# (guestfs). Opening an image boots a small VM, so listings run in a
# worker thread to avoid blocking the event loop.

import os

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static

from seine.tui.base import BaseScreen
from seine.tui.spectree import SpecTree

MARKS = {"d": "📁", "l": "📄", "r": "📄"}

# Built as a real Text, not markup: a name with a literal '[' must not
# be parsed as markup.
def _numbered_text(text):
    lines = text.splitlines()
    width = len(str(len(lines))) if lines else 1
    numbered = Text()
    for i, line in enumerate(lines, start=1):
        numbered.append("%*d " % (width, i), style="dim")
        numbered.append(line + "\n")
    return numbered

# Shared by render() and options() so both agree on entry formatting.
def _entry_label(name, kind, size, target):
    mark = MARKS.get(kind, "  ")
    if kind == "l":
        return "%s %s -> %s" % (mark, name, target)
    if kind == "d":
        return "%s %s/" % (mark, name)
    return "%s %s" % (mark, name)

# Size cap plus a NUL/UTF-8 check: enough to keep a binary or huge
# file from being shown as garbage.
PREVIEW_CAP = 256 * 1024

def _as_text(data):
    if len(data) > PREVIEW_CAP or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None

class FilesystemState:
    def __init__(self):
        self.build = None
        self.path = "/"
        self.entries = []
        self.error = None
        self.loading = False
        # (path, text) of a file shown instead of the directory listing.
        self.preview = None
        # One-shot message for a failed preview; keeps the current
        # listing instead of blanking it.
        self.notice = None
        # Set by FilesystemScreen on mount, cleared on unmount.
        self.on_change = None

    def reset(self, build):
        self.build = build
        self.path = "/"
        self.entries = []
        self.error = None
        self.loading = False
        self.preview = None
        self.notice = None

    # Called on the UI thread once a worker's Inspector.ls() returns.
    def loaded(self, path, entries):
        self.path = path
        self.entries = entries
        self.error = None
        self.loading = False
        if self.on_change is not None:
            self.on_change()

    def failed(self, text):
        self.error = text
        self.loading = False
        if self.on_change is not None:
            self.on_change()

    def previewed(self, path, text):
        self.preview = (path, text)
        self.loading = False
        if self.on_change is not None:
            self.on_change()

    def preview_failed(self, text):
        self.loading = False
        self.notice = text
        if self.on_change is not None:
            self.on_change()

    def close_preview(self):
        self.preview = None
        if self.on_change is not None:
            self.on_change()

    def render(self):
        if self.build is None:
            return "no active specification -- '/use SPEC' first\n"
        if self.loading:
            return "%s\n\nreading...\n" % self.path
        if self.error:
            return "%s\n\nerror: %s\n" % (self.path, self.error)
        lines = [self.path, ""]
        for entry in self.entries:
            lines.append(_entry_label(*entry))
        return "\n".join(lines) + "\n"

    def header(self):
        if self.build is None:
            return "no active specification -- '/use SPEC' first"
        if self.error:
            return "%s -- error: %s" % (self.path, self.error)
        return self.path

    # (label, name) per entry, with a leading '..' unless already at root.
    def options(self):
        if self.build is None or self.error:
            return []
        items = []
        if self.path != "/":
            items.append(("⬆️  ..", ".."))
        for entry in self.entries:
            items.append((_entry_label(*entry), entry[0]))
        return items

# Path resolution for the image's filesystem: no symlinks to chase.
def resolve(current, given):
    if given in ("", "."):
        return current
    if given == "..":
        parent = os.path.dirname(current.rstrip("/"))
        return parent if parent else "/"
    if given.startswith("/"):
        return os.path.normpath(given)
    return os.path.normpath(os.path.join(current, given))

# Runs Inspector.ls() in a worker thread; 'state' updates on the UI
# thread once it returns.
def browse(app, state, path):
    if state.build is None:
        return
    state.loading = True
    build = state.build

    def work():
        from seine.inspect import Inspector
        try:
            with Inspector(build.raw_spec, build.image._output) as inspector:
                if not inspector.is_dir(path):
                    raise ValueError("'%s' is not a directory" % path)
                entries = inspector.ls(path)
        except Exception as e:
            app.call_from_thread(state.failed, str(e))
            return
        app.call_from_thread(state.loaded, path, entries)

    app.run_worker(work, thread=True, exclusive=True, group="filesystem")

# Unlike browse(), doesn't assume 'path' is a directory: lists or
# previews depending on what guestfs reports. A read failure or
# non-text file goes through state.preview_failed(), which keeps the
# current listing instead of blanking it.
def open_entry(app, state, path):
    if state.build is None:
        return
    state.loading = True
    build = state.build

    def work():
        from seine.inspect import Inspector
        try:
            with Inspector(build.raw_spec, build.image._output) as inspector:
                if inspector.is_dir(path):
                    entries = inspector.ls(path)
                    app.call_from_thread(state.loaded, path, entries)
                    return
                data = inspector.cat(path)
        except Exception as e:
            app.call_from_thread(state.preview_failed, str(e))
            return
        text = _as_text(data)
        if text is None:
            app.call_from_thread(
                state.preview_failed, "'%s' does not look like a text file" % path)
        else:
            app.call_from_thread(state.previewed, path, text)

    app.run_worker(work, thread=True, exclusive=True, group="filesystem")

# A third Tab stop on this screen (prompt, spec tree, this). Keeps
# 'name' per row alongside the rendered label, since name_at() needs
# the name, not the label.
class FilesystemList(OptionList):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._names = []

    def set_entries(self, options):
        self.clear_options()
        self._names = [name for _, name in options]
        for label, _ in options:
            self.add_option(label)

    def name_at(self, index):
        if index is None or index < 0 or index >= len(self._names):
            return None
        return self._names[index]

# Plain VerticalScroll, not StaticPane: needs to be focusable so a
# long file scrolls with the keyboard like the list does.
class PreviewPane(VerticalScroll):
    pass

class FilesystemScreen(BaseScreen):
    DEFAULT_CSS = """
    #path { height: 3; padding: 0 2; border: round $foreground 40%; background: $background; }
    FilesystemScreen #fslist { background: $background; }
    """

    HINT_ADD = [
        ("command",  "cdpath", "'/cd PATH'"),
        ("cdpath",   "cdup",   "'/cd ..' up"),
        ("complete", "browse", "↑↓/Enter open"),
        ("browse",   "back",   "Esc back"),
    ]

    # No-op unless a file is being previewed.
    BINDINGS = BaseScreen.BINDINGS + [Binding("escape", "close_preview", show=False)]

    # #previewpane shares the body slot with #fslist while previewing.
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            Vertical(
                Static(id="path", markup=False),
                FilesystemList(id="fslist"),
                PreviewPane(Static(id="preview", markup=False), id="previewpane"),
                id="cmd",
            ),
            id="main",
        )
        yield from self.footer()

    def on_mount(self):
        # Tracks which pane update_body() last showed, so focus follows
        # only on an actual switch, not on every plain re-list.
        self._previewing = False
        super().on_mount()
        self.app.fs_state.on_change = self.update_body

    def on_unmount(self):
        self.app.fs_state.on_change = None

    def action_close_preview(self):
        if self.app.fs_state.preview is not None:
            self.app.fs_state.close_preview()

    def update_body(self):
        state = self.app.fs_state
        if state.notice is not None:
            self.say(state.notice, error=True)
            state.notice = None
        fslist = self.query_one(FilesystemList)
        preview = self.query_one(PreviewPane)
        previewing = state.preview is not None
        if previewing:
            path, text = state.preview
            self.query_one("#path", Static).update(path)
            self.query_one("#preview", Static).update(_numbered_text(text))
            fslist.display = False
            preview.display = True
        else:
            self.query_one("#path", Static).update(state.header())
            fslist.set_entries(state.options())
            fslist.display = True
            preview.display = False
        if previewing != self._previewing:
            (preview if previewing else fslist).focus()
        self._previewing = previewing

    def on_option_list_option_selected(self, event):
        name = self.query_one(FilesystemList).name_at(event.option_index)
        if name is None:
            return
        target = resolve(self.app.fs_state.path, name)
        open_entry(self.app, self.app.fs_state, target)
