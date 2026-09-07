# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# The '/issues' screen: known CVEs against the active build's SBOM,
# a findings table beside summary stats, both read-only and unfocusable.
# Width split 1:3:2 (spec tree : table : stats).

from textual.containers import Horizontal
from textual.widgets import Static

from seine.tui.base import BaseScreen, StaticPane
from seine.tui.render import render_issues_stats, render_issues_table
from seine.tui.spectree import SpecTree

class IssuesScreen(BaseScreen):
    DEFAULT_CSS = """
    #issuestable-pane { width: 3fr; height: 100%; border: round $foreground 40%; }
    #issuesstats-pane { width: 2fr; height: 100%; border: round $foreground 40%; }
    """

    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            StaticPane(Static(id="issuestable", markup=False), id="issuestable-pane"),
            StaticPane(Static(id="issuesstats", markup=False), id="issuesstats-pane"),
            id="main",
        )
        yield from self.footer()

    # Narrows the spec tree from app.py's global '#spectree { width: 2fr }',
    # wrong once a second pane joins it here. Set as an instance style
    # since a CSS rule here can't override that global one.
    def on_mount(self):
        super().on_mount()
        self.query_one("#spectree").styles.width = "1fr"

    # 'rescan' is one-shot: only '/issues --rescan' forces a fresh scan;
    # any later refresh_data() just reads the cache.
    def update_body(self):
        rescan = self.app.issues_rescan
        self.app.issues_rescan = False
        context = self.app.context
        self.query_one("#issuestable", Static).update(
            render_issues_table(context, package=self.app.issues_filter,
                                min_urgency=self.app.issues_min_urgency, rescan=rescan))
        self.query_one("#issuesstats", Static).update(render_issues_stats(context))
