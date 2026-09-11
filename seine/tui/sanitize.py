# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Test/build/vendor log panes are RichLog/Static widgets, not terminals:
# any escape sequence in the text would be written straight to the real
# terminal, escaping its pane (cursor moves, clears, colour changes).
# Everything tailed into those panes goes through here first.

import re

# CSI: ESC [ params intermediates final (e.g. '\x1b[31m', '\x1b[2K').
_CSI = r"\x1b\[[0-?]*[ -/]*[@-~]"
# OSC: ESC ] ... terminated by BEL or ESC \ (e.g. window titles).
_OSC = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
# Anything else ESC introduces (charset switches, single-char ops).
_OTHER_ESC = r"\x1b[@-Z\\-_]"
# Single-byte C1 CSI (rare, but the same pane escape via another byte).
_C1_CSI = r"\x9b[0-?]*[ -/]*[@-~]"

_ANSI_RE = re.compile("|".join([_OSC, _CSI, _OTHER_ESC, _C1_CSI]))

# C0 controls (minus \t/\n, kept; \r normalized to \n by the caller),
# DEL, and C1 controls -- none of them render as text in a log pane.
_CONTROLS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def sanitize(text):
    """Strip escape sequences and non-printable characters for TUI panes.

    Keeps '\\n' and '\\t' (the pane's own line breaks/indentation);
    anything else that could move the cursor or is non-printable is
    removed. '\\r' (e.g. progress-bar redraws) becomes '\\n' so lines
    stay readable instead of concatenating.
    """
    if not isinstance(text, str):
        text = str(text)
    # Normalize first so an OSC terminated by ESC \ still matches
    # after \r handling, and '\r\n' doesn't become two breaks.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x0b", "\n").replace("\x0c", "\n")
    text = _ANSI_RE.sub("", text)
    text = _CONTROLS_RE.sub("", text)
    return text
