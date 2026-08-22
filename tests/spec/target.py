#!/usr/bin/env python3

import avocado
import os
import sys
import types

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.tui import target

# available() caches its result in target._available -- each test resets
# it, so one test's outcome can never leak into the next.
class Availability(avocado.Test):
    def setUp(self):
        target._available = None
        self._real_mtda_client = sys.modules.pop("mtda.client", None)
        self._real_mtda = sys.modules.pop("mtda", None)

    def tearDown(self):
        target._available = None
        sys.modules.pop("mtda.client", None)
        sys.modules.pop("mtda", None)
        if self._real_mtda is not None:
            sys.modules["mtda"] = self._real_mtda
        if self._real_mtda_client is not None:
            sys.modules["mtda.client"] = self._real_mtda_client

    # sys.modules[name] = None is the standard way to force 'import
    # name' to raise ImportError without mtda actually being uninstalled.
    def test_not_available_when_mtda_cannot_be_imported(self):
        sys.modules["mtda.client"] = None
        self.assertFalse(target.available())

    def test_available_when_mtda_imports_cleanly(self):
        sys.modules["mtda"] = types.ModuleType("mtda")
        sys.modules["mtda.client"] = types.ModuleType("mtda.client")
        self.assertTrue(target.available())

    def test_result_is_cached_not_rechecked(self):
        sys.modules["mtda.client"] = None
        self.assertFalse(target.available())
        # Presence now wouldn't matter -- the cached False must stick.
        sys.modules["mtda"] = types.ModuleType("mtda")
        sys.modules["mtda.client"] = types.ModuleType("mtda.client")
        self.assertFalse(target.available())
