#!/usr/bin/env python3

import avocado
import json
import os
import stat
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import settings

class ASettingsFile(avocado.Test):
    def path(self):
        return os.path.join(self.workdir, "settings.json")

# Nothing decides a build from this file, so it being missing,
# unreadable or not an object is settings with nothing set -- the same
# rule 'seine.cache_index.Index' already follows for its own index.
class LoadingIsTolerant(ASettingsFile):
    def test_missing_file_is_defaults(self):
        self.assertEqual(settings.load(self.path()), settings.DEFAULTS)

    def test_not_json_is_defaults(self):
        with open(self.path(), "w") as f:
            f.write("this is not json")
        self.assertEqual(settings.load(self.path()), settings.DEFAULTS)

    def test_not_an_object_is_defaults(self):
        with open(self.path(), "w") as f:
            json.dump(["not", "an", "object"], f)
        self.assertEqual(settings.load(self.path()), settings.DEFAULTS)

    # A file from an older or newer seine -- some keys missing, one this
    # version has never heard of -- still loads, defaults filling the
    # gaps.
    def test_a_partial_file_is_filled_in_with_defaults(self):
        with open(self.path(), "w") as f:
            json.dump({"jobs": 4}, f)
        current = settings.load(self.path())
        self.assertEqual(current["jobs"], 4)
        self.assertEqual(current["theme"], settings.DEFAULTS["theme"])
        self.assertIsNone(current["sbom2cve_program"])

class SavingRoundTrips(ASettingsFile):
    def test_a_saved_value_reads_back(self):
        current = settings.load(self.path())
        current["jobs"] = 4
        current["startup_commands"] = ["/plan"]
        current["sbom2cve_program"] = "/usr/local/bin/my-scanner"
        settings.save(current, self.path())
        expected = dict(settings.DEFAULTS, jobs=4, startup_commands=["/plan"],
                        sbom2cve_program="/usr/local/bin/my-scanner")
        self.assertEqual(settings.load(self.path()), expected)

    # A key this version doesn't know about (a future 'llm', say) is
    # loaded, kept, and written straight back -- 'save()' never drops
    # what it didn't understand.
    def test_an_unknown_key_round_trips(self):
        with open(self.path(), "w") as f:
            json.dump({"future_key": "kept"}, f)
        current = settings.load(self.path())
        settings.save(current, self.path())
        with open(self.path()) as f:
            self.assertEqual(json.load(f)["future_key"], "kept")

    # Written beside itself and moved into place -- a reader never sees
    # half a write, the same as 'seine.cache_index.Index._write()'.
    def test_written_atomically_not_in_place(self):
        settings.save(settings.load(self.path()), self.path())
        self.assertFalse(os.path.exists("%s.new" % self.path()))
        self.assertTrue(os.path.exists(self.path()))

    def test_saved_file_is_owner_only(self):
        settings.save(settings.load(self.path()), self.path())
        mode = stat.S_IMODE(os.stat(self.path()).st_mode)
        self.assertEqual(mode, 0o600)

class DefaultPath(avocado.Test):
    def test_honours_xdg_config_home(self):
        real = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/somewhere"
        try:
            self.assertEqual(settings.default_path(),
                             "/somewhere/seine/settings.json")
        finally:
            if real is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = real

    def test_falls_back_to_home_config(self):
        real = os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            self.assertEqual(settings.default_path(),
                             os.path.expanduser("~/.config/seine/settings.json"))
        finally:
            if real is not None:
                os.environ["XDG_CONFIG_HOME"] = real

if __name__ == "__main__":
    avocado.main()
