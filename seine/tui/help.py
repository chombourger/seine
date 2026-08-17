# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# /help: a modal overlay, not a screen app.show() navigates to -- the
# screen underneath stays put, Esc returns to it, same as Ctrl+P.

import textwrap

from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static

from seine.tui import commands

TABS = ["General", "Commands"]

SHORTCUTS = [
    ("/",      "focus the prompt from anywhere"),
    ("Tab",    "switch pane (spec tree, and a screen's own extra pane)"),
    ("→", "accept the ghost-text suggestion"),
    ("Ctrl+P", "command palette"),
    ("!",      "run a shell command ('!' alone opens one)"),
    ("Esc",    "close a preview, or this help"),
]

# The active tab in reverse video, the rest dim -- a real Text, not
# markup, for styled spans within one line.
def _tab_bar(active):
    bar = Text()
    for i, name in enumerate(TABS):
        if i > 0:
            bar.append("   ")
        bar.append(" %s " % name, style="reverse bold" if i == active else "dim")
    return bar

def _general_text():
    text = Text("seine builds Debian-based embedded images from a "
               "specification -- this TUI drives the same build the "
               "command line does.\n\n")
    text.append("Shortcuts\n", style="bold")
    width = max(len(key) for key, _ in SHORTCUTS)
    for key, what in SHORTCUTS:
        text.append("  %-*s  " % (width, key), style="bold cyan")
        text.append("%s\n" % what)
    return text

# One row per command, not per alias -- filters on c.name so
# REGISTRY["q"] (same object as REGISTRY["quit"]) isn't listed twice.
class CommandList(OptionList):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._names = []

    def set_commands(self):
        if self._names:
            return
        entries = [c for name, c in commands.REGISTRY.items() if name == c.name]
        width = max(len(("/%s %s" % (c.name, c.args)).rstrip()) for c in entries) + 2
        for c in entries:
            label = Text()
            head = ("/%s %s" % (c.name, c.args)).rstrip()
            label.append(head.ljust(width), style="bold cyan")
            label.append(c.help)
            self.add_option(label)
            self._names.append(c.name)

    def name_at(self, index):
        if index is None or index < 0 or index >= len(self._names):
            return None
        return self._names[index]

# A command's detail page, laid out like a man page: bold all-caps
# section headers, indented body. DESCRIPTION is skipped when a command
# has nothing beyond its one-line help.
DETAIL_WIDTH = 64

def _section(title, body):
    text = Text()
    text.append("%s\n" % title, style="bold")
    text.append(textwrap.fill(body, width=DETAIL_WIDTH,
                              initial_indent="    ", subsequent_indent="    "))
    text.append("\n")
    return text

# One flags line per option, description indented further under it --
# the same two-line shape man's own OPTIONS section uses.
def _options_section(options):
    text = Text()
    text.append("OPTIONS\n", style="bold")
    for flags, desc in options:
        text.append("    %s\n" % flags)
        text.append(textwrap.fill(desc, width=DETAIL_WIDTH - 4,
                                  initial_indent="        ",
                                  subsequent_indent="        "))
        text.append("\n")
    return text

def _detail_text(c):
    text = Text()
    text.append(_section("NAME", "/%s - %s" % (c.name, c.help)))
    text.append("\n")
    synopsis = ("/%s %s" % (c.name, c.args)).rstrip()
    text.append(_section("SYNOPSIS", synopsis))
    if c.detail:
        text.append("\n")
        text.append(_section("DESCRIPTION", c.detail))
    if c.options:
        text.append("\n")
        text.append(_options_section(c.options))
    return text

LIST_HINT = "← → tabs · ↑↓ move · Enter open · Esc close"
DETAIL_HINT = "Enter fill prompt · Esc back to the list"

class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss",     show=False),
        Binding("left",   "prev_tab",    show=False),
        Binding("right",  "next_tab",    show=False),
        # Only reached while a detail page shows: CommandList binds its
        # own 'enter' and consumes it first while focused.
        Binding("enter",  "activate",    show=False),
    ]

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #helppane {
        width: 70%; height: 70%;
        /* 'round', not 'tall' -- see app.py's CSS comment on glyph support. */
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #helptitle { color: blue; text-style: bold; }
    #helptabs { padding: 1 0; height: auto; }
    #helpbody { height: 1fr; }
    #helplist { height: 1fr; }
    #helpdetail { height: 1fr; }
    #helphint { color: $text-muted; padding-top: 1; height: auto; }
    """

    def __init__(self):
        super().__init__()
        self._active = 0
        # The command CommandList's Enter last opened, or None while
        # the list (or General tab) is showing.
        self._detail = None

    def compose(self):
        with Vertical(id="helppane"):
            yield Static("Help", id="helptitle")
            yield Static(id="helptabs")
            yield Static(id="helpbody", markup=False)
            yield CommandList(id="helplist")
            yield Static(id="helpdetail", markup=False)
            yield Static(LIST_HINT, id="helphint")

    def on_mount(self):
        self._redraw()

    # Esc backs out one level at a time: detail page first, then the
    # whole screen.
    def action_dismiss(self):
        if self._detail is not None:
            self._detail = None
            self._redraw()
            return
        self.app.pop_screen()

    def action_prev_tab(self):
        if self._detail is not None:
            return
        self._active = (self._active - 1) % len(TABS)
        self._redraw()

    def action_next_tab(self):
        if self._detail is not None:
            return
        self._active = (self._active + 1) % len(TABS)
        self._redraw()

    # Closes Help and hands the prompt "/name ", ready to type on -- same
    # "fill, don't run" as the command palette's RegistryProvider._fill().
    def action_activate(self):
        if self._detail is None:
            return
        name = self._detail
        self.app.pop_screen()
        from seine.tui.base import Prompt
        prompt = self.app.screen.query_one(Prompt)
        prompt.value = "/" + name + " "
        prompt.cursor_position = len(prompt.value)
        prompt.focus()

    # Not '_render': that's Widget._render(), an internal Textual hook --
    # shadowing it breaks rendering (see build.py's _redraw()).
    def _redraw(self):
        self.query_one("#helptabs", Static).update(_tab_bar(self._active))
        body = self.query_one("#helpbody", Static)
        clist = self.query_one(CommandList)
        detail = self.query_one("#helpdetail", Static)
        showing_commands = TABS[self._active] == "Commands"
        showing_detail = showing_commands and self._detail is not None
        body.display = not showing_commands
        clist.display = showing_commands and not showing_detail
        detail.display = showing_detail
        self.query_one("#helphint", Static).update(
            DETAIL_HINT if showing_detail else LIST_HINT)
        if showing_detail:
            detail.update(_detail_text(commands.REGISTRY[self._detail]))
        elif showing_commands:
            clist.set_commands()
            clist.focus()
        else:
            body.update(_general_text())

    # Enter on a command row opens its detail page; action_activate()
    # fills the prompt from there, one Enter later.
    def on_option_list_option_selected(self, event):
        name = self.query_one(CommandList).name_at(event.option_index)
        if name is None:
            return
        self._detail = name
        self._redraw()
