# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os
import re
import threading
import time

from seine.testing.loader import DEFAULT_LIBRARIES as _INTERESTING_LIBS

# Stand-in for the Textual App that seine.tui.target expects, so
# target.py runs unmodified whether called from '/target' or headless.
# call_from_thread/refresh_indicators run synchronously: there is no UI
# thread to marshal onto here.
#
# Also a Robot listener (start_test/end_test/start_keyword/end_keyword)
# that builds the interaction timeline. start_keyword() only records
# calls into seine's own libraries (DEFAULT_LIBRARIES), not every
# BuiltIn call or user keyword.

class RunContext:
    def __init__(self, spec=None, spec_files=None, outdir=None):
        self._target_client = None
        self._target_console = None
        self.target_state = None
        # No in-memory history outside a chat session.
        self.history = None
        # Merged spec 'test:' came from, exposed to tests via
        # ImageLibrary's 'Get Spec Value'.
        self.spec = spec
        self.spec_files = spec_files or []
        # Set once ImageLibrary.build_image() actually runs a build.
        self.built_image = None
        # Directory this run's artifacts (screenshots, logs, ...) go to.
        self.outdir = outdir
        # Raw console capture for the whole run, appended across
        # connect/disconnect cycles.
        self.console_log_path = os.path.join(outdir, "console.log") if outdir else None
        # Asciinema v2 recording of the same stream.
        self.console_cast_path = os.path.join(outdir, "console.cast") if outdir else None
        # One cast per test, keyed by test name (see start_test()).
        self.console_casts = {}
        self.current_test = None
        # Guards current_test/console_casts against a console byte
        # arriving mid-transition from mtda's background thread.
        self._console_lock = threading.Lock()
        # Timeline of keyword calls worth reporting. record_artifact()
        # attaches a file to the call that produced it. Written to
        # interactions.json by runner.run_spec() at the end of a run.
        self.interactions = []
        self._entry_stack = []

    # Attaches 'path' to the keyword call currently open, or a standalone
    # entry if none is open. Known gap: sometimes ends up standalone even
    # when called from inside Capture Screen/Capture Screen Image.
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
            # data.args is empty for named-argument calls (seine's
            # own shorthand spells nearly everything that way);
            # result.args holds what actually ran, resolved.
            resolved = getattr(result, "args", None)
            if resolved:
                entry["args"] = [str(a) for a in resolved]

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
