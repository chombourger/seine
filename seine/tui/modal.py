# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Shared shell for /help and /settings: both are modal overlays (the
# screen underneath stays put; Esc returns to it) with the same
# centered pane, title, and "back out one level, then pop" dismiss.

from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class ModalBase(ModalScreen):
    TITLE = ""

    BINDINGS = [
        Binding("escape", "dismiss", show=False),
    ]

    DEFAULT_CSS = """
    ModalBase { align: center middle; }
    #modalpane {
        width: 70%; height: 70%;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #modaltitle { color: blue; text-style: bold; }
    #modalhint { color: $text-muted; padding-top: 1; height: auto; }
    """

    def compose(self):
        with Vertical(id="modalpane"):
            yield Static(self.TITLE, id="modaltitle")
            yield from self.compose_body()

    def compose_body(self):
        return ()

    def on_mount(self):
        self._redraw()

    def action_dismiss(self):
        if self._pop_inner():
            return
        self.app.pop_screen()

    # Subclasses override this to close their own inner state first.
    # True means Esc was used up and the modal stays open.
    def _pop_inner(self):
        return False
