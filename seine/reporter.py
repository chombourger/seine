# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What seine.tasks.run() asks of whatever is watching a build, already
# implemented by progress.Display. A typing.Protocol, not a runtime ABC,
# so Display keeps working duck-typed without subclassing this.
#
# output/sampled are new and optional (no-op by default): a task's own
# output line, and an analyze.watching() sample, pushed live.
#
# Not @runtime_checkable: that would demand output/sampled on every
# isinstance() check. A caller checks support with
# getattr(reporter, "sampled", None) instead (see Image.build()).
from typing import Protocol

class Reporter(Protocol):
    def started(self, name: str) -> None:
        ...

    def finished(self, name: str, failed: bool = False) -> None:
        ...

    def say(self, text: str) -> None:
        ...

    def output(self, name: str, line: str) -> None:
        pass

    def sampled(self, sample: dict) -> None:
        pass
