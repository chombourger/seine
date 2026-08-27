# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import re
import threading
import time

from seine.testing.loader import DEFAULT_LIBRARIES as _INTERESTING_LIBS

# The duck-typed stand-in for the Textual App that seine.tui.target's
# functions already expect -- built once per run and handed to every
# keyword library, so target.py runs unmodified whether called from
# '/target' or headless. call_from_thread/refresh_indicators exist even
# headless because mtda's console-remote subscription calls them from
# its own background thread regardless of who connected it; run
# synchronously here, since there is no UI thread to marshal onto.
#
# Also a Robot listener (start_test/end_test/start_keyword/end_keyword),
# kept apart from runner._Listener since the interaction timeline is
# context's own business -- a keyword library reads/writes it.
# start_keyword() only records seine's own three libraries (matched
# against 'result.libname', Robot's dotted import path -- reusing
# loader.DEFAULT_LIBRARIES' own strings rather than a second spelling
# that could drift), not every BuiltIn call output.xml already has in
# full; a user keyword ('Log In') is skipped the same way, so only the
# real actions it calls show up.

class RunContext:
    def __init__(self, spec=None, spec_files=None, outdir=None):
        self._target_client = None
        self._target_console = None
        self.target_state = None
        # No in-memory history to feed -- '/target's own recall isn't
        # useful outside a chat session, and target.py's history side-
        # load/unload calls are already no-ops when this is None (see
        # its own get(app, "history", None) guards).
        self.history = None
        # The merged, parsed specification 'test:' came from -- the same
        # files 'seine test'/'/test'/'run-test' were given, exposed to
        # tests via seine.testing.library.image.ImageLibrary's own
        # 'Get Spec Value'.
        self.spec = spec
        self.spec_files = spec_files or []
        # Set by ImageLibrary.build_image() once a build actually ran --
        # None until then, which ImageLibrary.inspect_image_path() reads
        # as "nothing built yet this run" rather than a stale one.
        self.built_image = None
        # Where a run's own artifacts (screenshots, console log,
        # interactions.json, ...) are written -- one directory per run.
        self.outdir = outdir
        # target.ConsoleAdapter's own raw capture (opt-in: only set
        # when 'outdir' is, see run_spec()) -- one file for the whole
        # run, appended across however many connect/disconnect cycles
        # a suite's own per-test setup/teardown makes.
        self.console_log_path = os.path.join(outdir, "console.log") if outdir else None
        # Asciinema v2 recording of the same stream -- replayable evidence
        # next to console.log (see seine.tui.target.ConsoleAdapter).
        self.console_cast_path = os.path.join(outdir, "console.cast") if outdir else None
        # One cast per test (see start_test()) -- supporting evidence that
        # stays scoped to a single test rather than the whole run.
        self.console_casts = {}
        self.current_test = None
        # Held around every current_test transition and every read of it
        # from ConsoleAdapter.print() (mtda's own background thread) --
        # closes the race where a console byte arrives mid-transition and
        # gets routed by a torn read of current_test/console_casts. Does
        # not (cannot) resolve which test a byte straddling the real
        # hardware boundary truly belongs to -- see record_artifact()'s
        # own comment on that gap.
        self._console_lock = threading.Lock()
        # One entry per 'interesting' keyword call, in order -- a
        # timeline, not a lookup table. 'record_artifact()' attaches a
        # file (a screenshot, say) to the entry for the keyword call
        # that produced it, rather than a separate list of files with
        # no way to tell what led up to one. runner.run_spec() writes
        # this to 'interactions.json' once the run ends.
        self.interactions = []
        self._entry_stack = []

    # Attaches 'path' to the keyword call currently open (Capture
    # Screen/Capture Screen Image call this from inside their own
    # keyword body); a standalone entry otherwise. A real run's own
    # interactions.json has shown standalone entries even from inside
    # one of those two, not yet root-caused -- a known gap, not blocking.
    def record_artifact(self, kind, path):
        entry = self._entry_stack[-1] if self._entry_stack else None
        if entry is None:
            entry = {"test": self.current_test, "keyword": None, "args": [],
                     "timestamp": time.time()}
            self.interactions.append(entry)
        entry["artifact_kind"] = kind
        entry["artifact_path"] = os.path.relpath(path, self.outdir) if self.outdir else path

    def _cast_path_for(self, test_name):
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', test_name)
        return os.path.join(self.outdir, "%s.cast" % safe) if self.outdir else None

    def _ensure_cast_for_test(self, test_name):
        if not self.outdir or not test_name:
            return
        if test_name in self.console_casts:
            return
        path = self._cast_path_for(test_name)
        self.console_casts[test_name] = path
        if os.path.isfile(path):
            return
        # Header-only cast so the file exists even when no console
        # bytes ever arrived during this test.
        import json as _json
        from seine.tui.target import CONSOLE_COLUMNS, CONSOLE_LINES
        header = {
            "version": 2,
            "width": CONSOLE_COLUMNS,
            "height": CONSOLE_LINES,
            "timestamp": int(time.time()),
            "env": {"TERM": "xterm-256color"},
        }
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(header, f)
            f.write("\n")

    # Robot listener API -- see this class's own header comment.
    def start_test(self, data, result):
        with self._console_lock:
            self.current_test = "%s.%s" % (result.parent.name, data.name)
            self._ensure_cast_for_test(self.current_test)

    def end_test(self, data, result):
        with self._console_lock:
            self.current_test = None

    def start_keyword(self, data, result):
        if data.type != data.KEYWORD or getattr(result, "libname", None) not in _INTERESTING_LIBS:
            self._entry_stack.append(None)
            return
        entry = {"test": self.current_test, "keyword": result.kwname,
                 "args": list(data.args), "timestamp": time.time()}
        self.interactions.append(entry)
        self._entry_stack.append(entry)

    def end_keyword(self, data, result):
        entry = self._entry_stack.pop()
        if entry is not None:
            entry["status"] = result.status

    def call_from_thread(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def refresh_indicators(self):
        pass

    def __enter__(self):
        from seine.tui import target
        self.target_state = target.TargetState()
        return self

    def __exit__(self, *exc):
        from seine.tui import target
        target.disconnect(self)
        return False
