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
