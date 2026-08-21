# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# /chat's widget half (ai.py holds the model-facing half: the tool
# loop, AIState, ConfirmAction). Shaped like BuildScreen's cockpit: a
# second row with #chatcol where #tail is, and a #stats pane (tokens
# in/out, a context-fill meter) where #tasklist is.

import time

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Markdown, ProgressBar, Static

from seine.progress import SPINNER, elapsed
from seine.tui import ai
from seine.tui.base import BaseScreen, StaticPane
from seine.tui.render import render_chat_header
from seine.tui.spectree import SpecTree

# Same braille spinner progress.py's ANSI Display uses during a build --
# always the fancy set, since Textual already assumes a capable terminal.
FRAMES = SPINNER[True]

class ChatScreen(BaseScreen):
    DEFAULT_CSS = """
    /* Scoped to this screen: #main's default background (Textual's
       plain widget default) is a visibly lighter grey than #chatrow's
       explicit $background, and the two halves of this screen clashed. */
    ChatScreen #main { background: $background; }
    ChatScreen #spectree { background: $background; }
    ChatScreen #cmd { background: $background; }
    #chatrow { height: 1fr; }
    /* $border-blurred's default sits almost on top of $background, so an
       explicit lighter grey is what makes the frame actually read as one. */
    #chatcol {
        width: 2fr; height: 100%;
        border: round $foreground 40%;
        background: $background;
        border-subtitle-color: $warning;
        border-subtitle-align: right;
    }
    /* 'background' alone doesn't reach the scrollbar -- a separate set
       of properties, defaulting to a pure black track/corner. */
    #chatlog {
        height: 1fr; border: none; background: $background;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
        scrollbar-corner-color: $background;
        scrollbar-color: #999999;
        scrollbar-color-hover: #a0a0a0;
        scrollbar-color-active: #a0a0a0;
    }
    #draft { height: auto; padding: 0 2; text-style: italic; background: $background; }
    #stats {
        width: 1fr; height: 100%; padding: 0 1;
        border: round $foreground 40%;
        background: $background;
    }
    #tokenstats { height: auto; }
    #contextbar { margin-top: 1; }
    #contextstats { color: $text-muted; height: auto; }
    """

    def compose(self):
        yield Horizontal(
            SpecTree(id="spectree"),
            StaticPane(Static(id="body", markup=False), id="cmd"),
            id="main",
        )
        yield Horizontal(
            Vertical(
                VerticalScroll(id="chatlog"),
                # Directly under the transcript, not a separate row
                # spanning #stats too -- a reply-in-progress reads as
                # the next line of the same conversation.
                Static(id="draft", markup=False),
                id="chatcol",
            ),
            Vertical(
                Static(id="tokenstats", markup=False),
                ProgressBar(id="contextbar", show_eta=False),
                Static(id="contextstats", markup=False),
                id="stats",
            ),
            id="chatrow",
        )
        yield from self.footer()

    def on_mount(self):
        super().on_mount()
        self._draft_text = ""
        self._spinner_frame = 0
        # Which tool calls' rows are expanded -- reset every fresh mount.
        self._expanded = set()
        state = self.app.ai_state
        state.on_change = self._rebuild_log
        state.on_delta = self._on_delta
        state.on_delta_done = self._on_delta_done
        state.on_stats = self._refresh_stats
        # A fresh mount has no draft in progress yet.
        self.query_one("#draft", Static).display = False
        self._rebuild_log()
        self._refresh_stats()
        # Called once here too, not left to wait for the first scheduled
        # tick -- ask() switches to this screen the same moment it sets
        # busy, and a mount showing nothing for half a second is visible.
        self._tick_working()
        self.set_interval(0.5, self._tick_working)

    # The one place #chatlog is written -- called on every messages/
    # errors change and on a tool row click. Rebuilt whole (remove_children
    # + remount) rather than patched, so a tool call's row can flip
    # between collapsed/expanded after the fact.
    #
    # A person's line and tool-call rows are plain Static/Text -- dim,
    # raw, never markdown-parsed (including expanded tool output, which
    # is arbitrary command text, not prose). Only a model's own finished
    # reply is a Markdown widget: it's the one thing here actually meant
    # to be read as markdown. While that reply is still streaming it
    # lives in #draft instead, as plain text -- swapping it in as
    # Markdown only once the parser has the whole, well-formed string
    # avoids re-parsing (and flickering on) broken mid-token syntax.
    #
    # One blank spacer between groups, never trailing after the last one
    # -- it butts straight up against #draft so a streaming reply reads
    # as the next line of the same log.
    #
    # A tool call with no matching 'role: tool' result yet gets its own
    # row too, "— Working…" instead of the expand arrow, so a person can
    # tell which tool is running rather than just seeing a generic spinner.
    def _rebuild_log(self):
        log = self.query_one("#chatlog", VerticalScroll)
        log.remove_children()
        messages = self.app.ai_state.messages
        tool_names = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                tool_names[call["id"]] = call.get("function", {}).get("name", "?")
        resolved = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}

        groups = []
        for message in messages:
            role = message.get("role")
            if role == "user":
                groups.append([Static(Text("| %s" % message.get("content", "").strip(),
                                           style="dim"), markup=False)])
            elif role == "assistant":
                widgets = []
                # '.strip()': a reasoning model's own 'content' routinely
                # starts (sometimes ends) with a blank line or two of its
                # own -- seen live, repeatedly, against a real endpoint
                # ('"\n\nFour."').
                content = (message.get("content") or "").strip()
                if content:
                    widgets.append(Markdown(content))
                for call in message.get("tool_calls") or []:
                    if call["id"] not in resolved:
                        name = call.get("function", {}).get("name", "?")
                        widgets.append(Static(Text("🔧 %s — Working…" % name, style="dim"),
                                              markup=False))
                if widgets:
                    groups.append(widgets)
            elif role == "tool":
                call_id = message.get("tool_call_id")
                name = tool_names.get(call_id, "?")
                expanded = call_id in self._expanded
                arrow = "▾" if expanded else "▸"
                style = Style(dim=True,
                             meta={"@click": "screen.toggle_tool(%r)" % call_id})
                widgets = [Static(Text("%s 🔧 %s" % (arrow, name), style=style), markup=False)]
                if expanded:
                    result = (message.get("content") or "").strip()
                    widgets.append(Static(Text("    %s" % result, style="dim"), markup=False))
                groups.append(widgets)
        for error in self.app.ai_state.errors:
            groups.append([Static(Text("error: %s" % error, style="bold red"), markup=False)])

        for index, group in enumerate(groups):
            if index > 0:
                log.mount(Static(""))
            log.mount_all(group)
        self.call_after_refresh(lambda: log.scroll_end(animate=False))

    def action_toggle_tool(self, call_id):
        if call_id in self._expanded:
            self._expanded.discard(call_id)
        else:
            self._expanded.add(call_id)
        self._rebuild_log()

    def on_unmount(self):
        state = self.app.ai_state
        state.on_change = state.on_delta = state.on_delta_done = state.on_stats = None

    def update_body(self):
        self.query_one("#body", Static).update(render_chat_header(self.app.context))

    # The trailing block cursor is redrawn with every delta so it's
    # always the last character streamed in, like a terminal cursor.
    # display = True/False (both places here) is what actually reclaims
    # the row -- height: auto on an empty Static still reports one row.
    def _on_delta(self, text):
        self._draft_text += text
        draft = self.query_one("#draft", Static)
        # lstrip() on what's shown, not self._draft_text itself -- the
        # same reasoning-model leading-blank-line habit _rebuild_log()
        # strips for a finished message, caught here before it's shown.
        draft.update(self._draft_text.lstrip() + "▌")
        draft.display = True

    def _on_delta_done(self):
        self._draft_text = ""
        draft = self.query_one("#draft", Static)
        draft.update("")
        draft.display = False

    # Ticks for the whole turn, not just the wait before the first token
    # -- a tool round-trip after a first reply is still "working", not
    # "done". #chatcol's own border_subtitle, not a row of its own, so
    # it costs no space at all, busy or idle.
    def _tick_working(self):
        state = self.app.ai_state
        chatcol = self.query_one("#chatcol")
        if not state.busy:
            chatcol.border_subtitle = ""
            return
        frame = FRAMES[self._spinner_frame % len(FRAMES)]
        self._spinner_frame += 1
        started = state.turn_started_at or time.time()
        chatcol.border_subtitle = "%s Working… (%s)" % (frame, elapsed(time.time() - started))

    def _refresh_stats(self):
        state = self.app.ai_state
        self.query_one("#tokenstats", Static).update(
            "TOKENS\n  in:  %6d\n  out: %6d" % (state.prompt_tokens, state.completion_tokens))
        bar = self.query_one("#contextbar", ProgressBar)
        contextstats = self.query_one("#contextstats", Static)
        max_tokens = state.context_max
        if max_tokens in (None, ai._UNSET):
            bar.display = False
            contextstats.update("CONTEXT\n  (unknown)" if max_tokens is None else "")
            return
        model = ai._resolved()[0]
        used = state.used_tokens(model) if model else None
        bar.display = True
        bar.update(total=max_tokens, progress=used or 0)
        contextstats.update("CONTEXT\n  %s/%s tokens" % (used, max_tokens))
