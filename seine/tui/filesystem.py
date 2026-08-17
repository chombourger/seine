# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The Filesystem screen: a read-only browse of a *finished* image via
# seine/inspect.py (guestfs). Opening an image boots a small VM
# (libguestfs's supermin appliance), so every listing runs in a worker
# thread, same as the Build cockpit, so it doesn't block the event loop.
#
# ponytail: a fresh Inspector (and appliance boot) opens for every
# directory listed, rather than staying open across a browsing session --
# correct but a few seconds slower per navigation. Keep FilesystemState's
# Inspector open across browse() calls instead if that turns out to matter.

import os

from rich.text import Text
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static

from seine.tui.base import BaseScreen
from seine.tui.spectree import SpecTree

MARKS = {"d": "📁", "l": "📄", "r": "📄"}

# Built as a real Text, not markup -- a file with a literal '[' must
# never be parsed as markup.
def _numbered_text(text):
    lines = text.splitlines()
    width = len(str(len(lines))) if lines else 1
    numbered = Text()
    for i, line in enumerate(lines, start=1):
        numbered.append("%*d " % (width, i), style="dim")
        numbered.append(line + "\n")
    return numbered

# Shared by FilesystemState.render() and .options(), so the two never
# drift into different ideas of what an entry looks like. No size
# column -- doesn't fit the pane's third of the screen width.
def _entry_label(name, kind, size, target):
    mark = MARKS.get(kind, "  ")
    if kind == "l":
        return "%s %s -> %s" % (mark, name, target)
    if kind == "d":
        return "%s %s/" % (mark, name)
    return "%s %s" % (mark, name)

# A size cap plus NUL/UTF-8 check, not a real charset/MIME sniff --
# enough to keep a binary or huge file off the screen as garbage.
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
        # One-shot status for a failed preview -- unlike a failed /cd,
        # opening a binary/huge file must not blank the current listing.
        self.notice = None
        # Set by FilesystemScreen.on_mount()/cleared by on_unmount(),
        # same import-cycle workaround as seine/tui/build.py.
        self.on_change = None

    def reset(self, build):
        self.build = build
        self.path = "/"
        self.entries = []
        self.error = None
        self.loading = False
        self.preview = None
        self.notice = None

    # Called back on the UI thread once a worker's Inspector.ls() returns.
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

    # (label, name) per entry -- a leading '..' when not at the root,
    # the same way a file picker offers "up" as a real row.
    def options(self):
        if self.build is None or self.error:
            return []
        items = []
        if self.path != "/":
            items.append(("⬆️  ..", ".."))
        for entry in self.entries:
            items.append((_entry_label(*entry), entry[0]))
        return items

# Not a general path-resolution library -- an image's filesystem has no
# '.'/symlink-loop concerns to chase, only what a typed name would mean.
def resolve(current, given):
    if given in ("", "."):
        return current
    if given == "..":
        parent = os.path.dirname(current.rstrip("/"))
        return parent if parent else "/"
    if given.startswith("/"):
        return os.path.normpath(given)
    return os.path.normpath(os.path.join(current, given))

# Runs Inspector.ls() in a worker thread; 'state' is updated back on the
# UI thread once it returns, same as TextualReporter does for a build.
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

# Unlike browse(), doesn't assume 'path' is a directory: asks guestfs,
# then lists or previews as appropriate. A binary/huge file or read
# error goes through state.preview_failed(), which leaves the current
# listing alone rather than blanking it -- opening the wrong row by
# accident is routine here in a way a bad typed path is not.
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

# A third Tab stop on this screen only (prompt, spec tree, this).
# set_entries() keeps 'name' per row alongside the rendered label, since
# name_at() is what selection reads back, not the label.
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

# A plain VerticalScroll, not StaticPane: focusable is exactly what's
# wanted, so a long file scrolls with the keyboard like the list does.
class PreviewPane(VerticalScroll):
    pass

class FilesystemScreen(BaseScreen):
    HINT_ADD = [
        ("command",  "cdpath", "'/cd PATH'"),
        ("cdpath",   "cdup",   "'/cd ..' up"),
        ("complete", "browse", "↑↓/Enter open"),
        ("browse",   "back",   "Esc back"),
    ]

    # No-op unless a file is being previewed, so it never steals the key
    # from anything else on the screen stack.
    BINDINGS = BaseScreen.BINDINGS + [Binding("escape", "close_preview", show=False)]

    # #body sits above a focusable FilesystemList instead of an
    # unfocusable StaticPane -- the body here is a list to act on, not
    # text to read. #previewpane shares that slot while previewing.
    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            Vertical(
                Static(id="body", markup=False),
                FilesystemList(id="fslist"),
                PreviewPane(Static(id="preview", markup=False), id="previewpane"),
                id="cmd",
            ),
            id="main",
        )
        yield from self.footer()

    def on_mount(self):
        # Tracks which of FilesystemList/PreviewPane update_body() last
        # showed, so focus follows only on an actual switch, not on
        # every plain re-list (a /cd shouldn't yank focus off the prompt).
        self._previewing = False
        super().on_mount()
        # update_body, not refresh_data: browsing never changes the spec
        # tree, only #body/#fslist/#previewpane.
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
            self.query_one("#body", Static).update(path)
            self.query_one("#preview", Static).update(_numbered_text(text))
            fslist.display = False
            preview.display = True
        else:
            self.query_one("#body", Static).update(state.header())
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
