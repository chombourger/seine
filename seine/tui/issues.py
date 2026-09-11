# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The '/issues' screen: a per-source CVE/defect matrix beside summary
# stats, and no spec tree -- the matrix is the reason to be here, so it
# keeps the room the tree would take. The matrix is a DataTable, so its
# header stays put while rows scroll beneath it (and the source column
# stays put while counts scroll beside it); selecting a count -- Enter
# on it, or clicking it -- swaps the matrix for that cell's own entries
# in the same pane, and Esc goes back to the matrix. The stats pane
# shrink-wraps its own contents; the matrix keeps the rest.

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, LoadingIndicator, Static

from seine import bugs as bugs_module
from seine import secscan as secscan_module
from seine.tui.base import BaseScreen, StaticPane
from seine.tui.render import (_issues_build, _issues_sbom_path,
                              issues_matrix_data, render_issues_detail,
                              render_issues_stats)

LEGEND = ("H=high M=medium L=low U=unimportant E=end-of-life N=not-yet-assigned\n"
          "C=critical G=grave S=serious I=important -- "
          "Enter/click a count for details, Esc goes back")

# Progress popup while a '/issues --rescan' runs in a worker thread:
# a spinner and one status line per phase, popping itself when the
# scan finishes. Esc dismisses it early -- the scan keeps going and
# the matrix refreshes underneath when it lands.
class RescanModal(ModalScreen):
    DEFAULT_CSS = """
    RescanModal { align: center middle; }
    #rescanpane {
        width: 60; height: auto;
        border: round $border;
        background: $surface;
        padding: 1 2;
    }
    #rescantitle { text-style: bold; }
    #rescanstatus { height: auto; padding-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss", show=False)]

    def compose(self):
        with Vertical(id="rescanpane"):
            yield Static("Rescanning issues…", id="rescantitle")
            yield LoadingIndicator()
            yield Static("", id="rescanstatus", markup=False)

    def action_dismiss(self):
        self.app.pop_screen()

    # Called from the worker thread via call_from_thread; the modal may
    # already be dismissed by then, in which case there is nothing to
    # update and no crash either.
    def set_status(self, text):
        try:
            self.query_one("#rescanstatus", Static).update(text)
        except NoMatches:
            pass

# Both halves of one rescan, split out so tests can stub the network
# and container without touching the worker/modal around them.
# 'report' takes one status line for the modal.
def _rescan_once(sbom_path, distro, report):
    report("scanning CVEs against the security tracker…")
    secscan_module.scan(sbom_path, distro=distro, rescan=True)
    report("fetching defects from UDD…")
    bugs_module.scan(sbom_path, distro=distro, rescan=True)

class IssuesScreen(BaseScreen):
    HINT_ADD = [("complete", "detail", "Esc back (viewing a detail)")]

    # The stats pane shrink-wraps its own contents -- whatever width
    # its longest line needs, no more -- and the matrix keeps the rest.
    DEFAULT_CSS = """
    #issuestable-pane { width: 1fr; height: 100%; border: round $foreground 40%; }
    #issuelegend { height: auto; padding: 0 1; color: $text-muted; }
    #issuestable { height: 1fr; }
    # Headers read as headers through bold text (and the header's
    # underline rule) alone -- their background stays the table's own,
    # never Textual's contrasting $panel/$secondary-muted fills.
    #issuestable .datatable--header { text-style: bold underline; background: $surface; }
    #issuestable .datatable--fixed { text-style: bold; background: $surface; }
    #issuedetail-pane { height: 1fr; }
    #issuesstats-pane { width: auto; height: 100%; border: round $foreground 40%; }
    #issuesstats { width: auto; }
    """

    # No-op unless a detail view is showing -- same shape as
    # OverviewScreen's own 'escape' -> action_close_log.
    BINDINGS = BaseScreen.BINDINGS + [Binding("escape", "close_detail", show=False)]

    def compose(self):
        yield Horizontal(
            Vertical(
                Static(LEGEND, id="issuelegend", markup=False),
                DataTable(id="issuestable", show_cursor=True, fixed_columns=1),
                StaticPane(Static(id="issuedetail", markup=False), id="issuedetail-pane"),
                id="issuestable-pane",
            ),
            StaticPane(Static(id="issuesstats", markup=False), id="issuesstats-pane"),
            id="main",
        )
        yield from self.footer()

    def on_mount(self):
        # Set before super().on_mount(): it calls refresh_data() right
        # away -- too soon to read this attribute if it was set after.
        #
        # The selected cell showing in place of the matrix, as a
        # (source, kind, bucket) triple, or None for the matrix itself.
        self._detail = None
        # A rescan worker is in flight; a second '/issues --rescan'
        # before it lands is refused rather than stacked.
        self._rescanning = False
        super().on_mount()

    # A count cell's key reads "kind:bucket" (see update_body()); the
    # source column's own key is just "source" and selects nothing.
    # CellSelected fires on Enter and on click alike, so both drive the
    # same detail view.
    def on_data_table_cell_selected(self, event):
        column = event.cell_key.column_key.value
        if column == "source":
            return
        kind, _, bucket = column.partition(":")
        self.action_show_detail(event.cell_key.row_key.value, kind, bucket)

    # Swaps the matrix for one cell's entries -- Esc
    # (action_close_detail) swaps back.
    def action_show_detail(self, source, kind, bucket):
        self._detail = (str(source), str(kind), str(bucket))
        self.update_body()

    def action_close_detail(self):
        if self._detail is not None:
            self._detail = None
            self.update_body()

    # Fills the table from issues_matrix_data(): columns (with keys)
    # then rows (keyed by source), so a later CellSelected reads back
    # (source, kind, bucket) straight from its own cell key.
    def _show_matrix(self, context):
        table = self.query_one("#issuestable", DataTable)
        table.clear(columns=True)
        result, error = issues_matrix_data(
            context, package=self.app.issues_filter,
            min_urgency=self.app.issues_min_urgency,
            min_severity=self.app.issues_min_severity)
        if error:
            self.query_one("#issuelegend", Static).update(error)
            return
        columns, rows, bugs_cached = result
        for label, width, key in columns:
            table.add_column(label, width=width, key=key)
        for source, cells in rows:
            table.add_row(*cells, key=source)
        if rows:
            legend = LEGEND
            if not bugs_cached:
                legend += "\ndefect counts are empty -- '/issues --rescan'"
            self.query_one("#issuelegend", Static).update(legend)
        else:
            self.query_one("#issuelegend", Static).update("no known CVEs or defects found")

    # 'rescan' is one-shot: only '/issues --rescan' forces a fresh scan
    # (CVEs and defects alike); any later refresh_data() just reads the
    # caches. The scan itself runs in a worker thread under a progress
    # modal, so the matrix stays up (and the TUI responsive) until the
    # fresh caches land. A detail view survives refreshes -- it
    # re-renders from the same caches the matrix reads.
    def update_body(self):
        rescan = self.app.issues_rescan
        self.app.issues_rescan = False
        context = self.app.context
        showing_detail = self._detail is not None
        self.query_one("#issuelegend", Static).display = not showing_detail
        self.query_one("#issuestable", DataTable).display = not showing_detail
        self.query_one("#issuedetail-pane", StaticPane).display = showing_detail
        if showing_detail:
            source, kind, bucket = self._detail
            self.query_one("#issuedetail", Static).update(
                render_issues_detail(context, source, kind, bucket,
                                     package=self.app.issues_filter,
                                     min_urgency=self.app.issues_min_urgency,
                                     min_severity=self.app.issues_min_severity))
        else:
            self._show_matrix(context)
        self.query_one("#issuesstats", Static).update(render_issues_stats(context))
        if rescan:
            if self._rescanning:
                self.say("a rescan is already running")
            else:
                self._start_rescan(context)

    # The SBOM a rescan would scan, or None (with the reason said aloud)
    # when there is nothing to scan yet.
    def _rescan_target(self, context):
        build, error = _issues_build(context)
        if error:
            self.say(error.strip(), error=True)
            return None
        path, error = _issues_sbom_path(build)
        if error:
            self.say(error.strip(), error=True)
            return None
        return path, build.spec["distribution"]["release"]

    def _start_rescan(self, context):
        target = self._rescan_target(context)
        if target is None:
            return
        sbom_path, distro = target
        self._rescanning = True
        modal = RescanModal()
        self.app.push_screen(modal)
        report = lambda text: self.app.call_from_thread(modal.set_status, text)

        def run():
            try:
                _rescan_once(sbom_path, distro, report)
            except Exception as e:
                self.app.call_from_thread(self._rescan_done, str(e))
            else:
                self.app.call_from_thread(self._rescan_done, None)

        self.app.run_worker(run, thread=True, exclusive=True,
                            group="issues-rescan")

    # Back on the app thread: fresh caches in place, so re-render and
    # pop the progress modal (unless Esc already did). NoMatches throughout:
    # the screen may be gone already if the session ended mid-scan.
    def _rescan_done(self, error):
        self._rescanning = False
        try:
            if error:
                self.say("rescan failed: %s" % error, error=True)
            else:
                self.update_body()
                self.say("rescan complete")
            if isinstance(self.app.screen, RescanModal):
                self.app.pop_screen()
        except NoMatches:
            pass
