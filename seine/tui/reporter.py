# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# A seine.reporter.Reporter that forwards each call through
# app.call_from_thread rather than touching widgets directly -- tasks.run()
# calls this from the build's worker thread, never the UI's event loop.
# 'sink' is whatever wants the calls back on the UI thread (BuildState).
class TextualReporter:
    def __init__(self, app, sink):
        self.app = app
        self.sink = sink

    def started(self, name):
        self.app.call_from_thread(self.sink.task_started, name)

    def finished(self, name, failed=False):
        self.app.call_from_thread(self.sink.task_finished, name, failed)

    def say(self, text):
        self.app.call_from_thread(self.sink.say, text)

    def sampled(self, sample):
        self.app.call_from_thread(self.sink.sampled, sample)

    # BuildState carries no 'output' (nothing calls this for a build
    # today -- seine.tasks captures a step's output to its own file
    # instead, tailed straight off disk); guarded the same way a
    # Reporter caller checks for 'sampled' before calling it.
    def output(self, name, line):
        sink_output = getattr(self.sink, "output", None)
        if sink_output is not None:
            self.app.call_from_thread(sink_output, name, line)

    # VendorState's own addition: 'seine vendor' runs three waves, each
    # its own tasks.run() with a fresh log directory of its own (see
    # VendorCmd._run_wave()) -- unlike a build's single, stable
    # 'image.logs', there is no one directory the vendor screen could
    # read 'state.logs' from without being told each time it changes.
    # Guarded the same way 'output' is: nothing calls this for a sink
    # that never defined it (BuildState never will).
    def wave_logs(self, path):
        sink_wave_logs = getattr(self.sink, "wave_logs", None)
        if sink_wave_logs is not None:
            self.app.call_from_thread(sink_wave_logs, path)
