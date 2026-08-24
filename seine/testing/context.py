# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# The duck-typed stand-in for the Textual App that seine.tui.target's
# functions already expect (get_client(app), power(app, ...), console_*
# (app, ...)) -- built once per test run and handed to every keyword
# library, so target.py runs unmodified whether it is called from '/target'
# in the TUI or headless from 'seine test'. call_from_thread/
# refresh_indicators are still needed here even without Textual: mtda's
# own console-remote subscription calls them from its background EVT
# thread the moment a console is live (target.ConsoleAdapter.on_event()),
# regardless of who connected it -- run synchronously here, since there
# is no separate UI thread to marshal onto headless.
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
        # Where a run's own artifacts (screenshots, ...) are written --
        # seine.testing.library.observation.ObservationLibrary's own
        # directory, one per run.
        self.outdir = outdir

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
