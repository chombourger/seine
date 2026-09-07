# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# A seine.reporter.Reporter that forwards each call through
# app.call_from_thread rather than touching widgets directly, since
# tasks.run() calls this from the build's worker thread. 'sink' is
# whatever wants the calls back on the UI thread (BuildState).
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

    # BuildState carries no 'output': seine.tasks captures a step's
    # output to its own file, tailed straight off disk instead.
    def output(self, name, line):
        sink_output = getattr(self.sink, "output", None)
        if sink_output is not None:
            self.app.call_from_thread(sink_output, name, line)

    # VendorState's addition: 'seine vendor' runs three waves, each with
    # a fresh log directory, unlike a build's single stable 'image.logs'.
    # Guarded like 'output': BuildState never defines this.
    def wave_logs(self, path):
        sink_wave_logs = getattr(self.sink, "wave_logs", None)
        if sink_wave_logs is not None:
            self.app.call_from_thread(sink_wave_logs, path)
