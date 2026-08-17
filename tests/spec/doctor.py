#!/usr/bin/env python3

import avocado
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import doctor

class Rendering(avocado.Test):
    def test_ok_and_missing_are_marked_apart(self):
        checks = [doctor.Check("A", "one", "ok", "present"),
                 doctor.Check("A", "two", "missing", "not found")]
        text = doctor.render(checks)
        self.assertIn("✔ one", text)
        self.assertIn("✗ two", text)

    def test_group_headers_appear_once(self):
        checks = [doctor.Check("Group", "one", "ok", "x"),
                 doctor.Check("Group", "two", "ok", "y"),
                 doctor.Check("Other", "three", "ok", "z")]
        text = doctor.render(checks)
        self.assertEqual(text.count("Group"), 1)
        self.assertEqual(text.count("Other"), 1)

    def test_the_tally_counts_missing_and_warn_apart(self):
        checks = [doctor.Check("A", "one", "ok", "x"),
                 doctor.Check("A", "two", "missing", "x"),
                 doctor.Check("A", "three", "warn", "x")]
        self.assertIn("1 error", doctor.render(checks))
        self.assertIn("1 note", doctor.render(checks))
        self.assertEqual(doctor.errors(checks), 1)

    def test_no_checks_is_zero_of_both(self):
        self.assertEqual(doctor.errors([]), 0)
        self.assertIn("0 errors · 0 notes", doctor.render([]))

class IndividualChecks(avocado.Test):
    # A binary that cannot exist is 'missing', not an exception.
    def test_a_missing_binary_is_missing_not_a_crash(self):
        check = doctor._binary("Group", "definitely-not-a-real-binary-xyz")
        self.assertEqual(check.status, "missing")

    def test_kvm_missing_when_the_device_is_not_there(self):
        real = os.path.exists
        os.path.exists = lambda path: False if path == "/dev/kvm" else real(path)
        try:
            check = doctor.check_kvm()
        finally:
            os.path.exists = real
        self.assertEqual(check.status, "missing")

    # No key set anywhere: a note, not a failure -- signing is optional.
    def test_no_sign_key_is_a_note(self):
        env = os.environ.pop("SEINE_SIGN_KEY", None)
        try:
            check = doctor.check_sign_key({})
        finally:
            if env is not None:
                os.environ["SEINE_SIGN_KEY"] = env
        self.assertEqual(check.status, "warn")

    def test_a_sign_key_on_the_command_line_is_ok(self):
        check = doctor.check_sign_key({"sign_key": "ABCDEF"})
        self.assertEqual(check.status, "ok")

    def test_a_sign_key_on_the_environment_is_ok(self):
        os.environ["SEINE_SIGN_KEY"] = "ABCDEF"
        try:
            check = doctor.check_sign_key({})
        finally:
            del os.environ["SEINE_SIGN_KEY"]
        self.assertEqual(check.status, "ok")

    # The host architecture's own hypervisor missing is a real failure;
    # another architecture's is only a note (cross-building is optional).
    def test_hypervisors_grade_the_host_architecture_harder(self):
        checks = {c.name: c for c in doctor.check_hypervisors()}
        from seine.imager import DEFAULT_HYPERVISORS
        from seine.utils import HOST_ARCH
        host_name = os.path.basename(DEFAULT_HYPERVISORS[HOST_ARCH])
        for architecture, path in DEFAULT_HYPERVISORS.items():
            if architecture == HOST_ARCH:
                continue
            other_name = os.path.basename(path)
            if checks[other_name].status != "ok":
                self.assertEqual(checks[other_name].status, "warn")

    def test_storage_reports_free_space(self):
        check = doctor.check_storage()
        self.assertEqual(check.status, "ok")
        self.assertIn("GiB free", check.detail)

class Running(avocado.Test):
    def test_run_without_pull_has_no_network_check(self):
        checks = doctor.run()
        self.assertNotIn("debsbom image reachable", [c.name for c in checks])

    def test_every_check_has_a_group(self):
        for check in doctor.run():
            self.assertTrue(check.group)

if __name__ == "__main__":
    avocado.main()
