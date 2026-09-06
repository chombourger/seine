# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# web-fetch: a small allow-listed HTTP GET, text-only.

from html.parser import HTMLParser

from . import Tool
from .tools_spec import SPEC_DUMP_CHUNK_LINES, _text_chunk

# Read-only outbound fetch, restricted to a small whitelist of trusted
# domains -- the ai-tool equivalent of source-pull/bash reading real
# upstream state instead of training data, but for a page rather than a
# package's own source. Ungated like bash: the whitelist below is the
# whole blast radius, nothing local is ever touched. [EXTERNAL-REFS]
# (external-refs.txt) is what tells the model to reach for this, and
# only once spec-query/read/docs/source-pull/bash can't answer.
#
# debian.org for the primary, authoritative source (BTS, package
# database, security tracker); opencve.io for the EPSS score alongside
# CVSS on one page -- Debian's own security tracker (seen live, above)
# carries no severity/exploitability field at all.
WEB_FETCH_ALLOWED_DOMAINS = ("debian.org", "opencve.io")
WEB_FETCH_MAX_BYTES = 2_000_000

# Stdlib only (no bs4/html2text among the 'ai' extra's own deps) --
# script/style content is dropped, everything else kept as plain text,
# one chunk per text node.
class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self.chunks.append(text)

def _strip_html(markup):
    extractor = _TextExtractor()
    extractor.feed(markup)
    return "\n".join(extractor.chunks)

# Chunked the same way spec-dump/docs are -- a fetched page can easily
# run past what's worth spending one reply's context on.
def _tool_web_fetch(app, arguments):
    url = arguments.get("url")
    if not url:
        return "web-fetch needs 'url'"
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed = any(host == d or host.endswith("." + d) for d in WEB_FETCH_ALLOWED_DOMAINS)
    if parsed.scheme != "https" or not allowed:
        return ("web-fetch only fetches https:// URLs on %s (the sites "
                "[EXTERNAL-REFS] names) -- '%s' isn't one" % (
                    " or ".join(WEB_FETCH_ALLOWED_DOMAINS), url))
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "seine-ai-chat"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(WEB_FETCH_MAX_BYTES)
            content_type = response.headers.get("Content-Type", "")
    except Exception as e:
        return "could not fetch %s: %s" % (url, e)
    text = data.decode("utf-8", "replace")
    if "html" in content_type.lower():
        text = _strip_html(text)
    lines = text.splitlines()
    try:
        start = int(arguments["start"]) if arguments.get("start") else 1
        end = int(arguments["end"]) if arguments.get("end") else start + SPEC_DUMP_CHUNK_LINES - 1
    except (TypeError, ValueError):
        return "'start'/'end' must be line numbers (1-indexed)"
    return _text_chunk(lines, start, end, SPEC_DUMP_CHUNK_LINES, url)

TOOLS = [
    Tool("web-fetch", "Fetch one page from a trusted site -- either an "
        "official Debian one (https://*.debian.org -- packages.debian.org, "
        "tracker.debian.org, security-tracker.debian.org, bugs.debian.org, "
        "wiki.debian.org, manpages.debian.org, and the rest) or "
        "opencve.io (a CVE aggregator, useful for a severity picture "
        "Debian's own security tracker doesn't carry: CVSS *and* the "
        "EPSS score together, e.g. https://app.opencve.io/cve/CVE-ID) -- "
        "and return its text, with any HTML stripped. No other domain is "
        "reachable. Use this only once the tools above (spec-query/read/"
        "docs/source-pull/bash) can't answer -- they already reflect "
        "this build's real state, no fetch needed for what they cover. "
        "[EXTERNAL-REFS] names which URL to build for a package's bug/"
        "CVE/changelog history, and how to weigh CVSS against EPSS when "
        "asked how critical a CVE really is. Returned in line-numbered "
        "chunks, same as spec-dump/docs -- omit 'start'/'end' for the "
        "first chunk.",
        {"type": "object",
         "properties": {"url": {"type": "string",
                                "description": "an https:// URL on "
                                               "*.debian.org or opencve.io"},
                        "start": {"type": "integer",
                                 "description": "first line to return "
                                                "(1-indexed); omit for 1"},
                        "end": {"type": "integer",
                               "description": "last line to return; omit "
                                              "for 'start' plus a few "
                                              "hundred lines"}},
         "required": ["url"]},
        False, _tool_web_fetch),
]
