# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Interface a build watcher (e.g. progress.Display) implements.
# output/sampled are optional (no-op default); check support with
# getattr(reporter, "sampled", None) rather than isinstance().
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
