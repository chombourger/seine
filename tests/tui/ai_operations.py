#!/usr/bin/env python3

import avocado
import contextlib
import io
import json
import os
import sys
import tarfile
import tempfile

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

os.environ.setdefault("SEINE_CACHE_DIR", tempfile.mkdtemp(prefix="seine-ai-tests-"))
os.chdir(tempfile.mkdtemp(prefix="seine-ai-tests-cwd-"))

from tests.native_image import native_image

NATIVE_IMAGE = native_image()
PC_IMAGE = os.path.join(path_to_sources, "examples", "pc-image", "main.yaml")

# The three env vars ('SEINE_LLM_MODEL' etc.) are process-global state,
# same as any other 'SEINE_*' override elsewhere in this suite -- popped
# in every 'setUp()' below, not just where a test sets its own, so a
# leftover from one test can never leak into the next one run in the
# same process.
LLM_ENV = ["SEINE_LLM_MODEL", "SEINE_LLM_API_BASE", "SEINE_LLM_API_KEY"]

def _clear_llm_env():
    for name in LLM_ENV:
        os.environ.pop(name, None)

# Duplicated rather than imported from tests/tui/ai.py -- test files
# stay self-contained here, none of them import each other.
@contextlib.contextmanager
def _tui_required(test):
    try:
        yield
    except ImportError as e:
        test.cancel("the 'tui' extra (textual) is not installed: %s" % e)

# One theme out of what was originally a single 'ToolTable' class in
# tests/tui/ai.py -- split apart (2026-09-01) once it had grown to
# cover every AI tool at once. This file: sbom-diff, installed-packages,
# issues, cache, vendor, vendor-why, reset-conversation, and the
# build/vendor start-and-cancel controls.
class ToolTable(avocado.Test):
    """
    :avocado: tags=tui
    """
    def setUp(self):
        with _tui_required(self):
            from seine.tui import ai
            from seine.tui.app import SeineApp
        self.ai = ai
        self.SeineApp = SeineApp
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        os.environ["XDG_CONFIG_HOME"] = self.workdir
        os.environ["SEINE_GISTS_DIR"] = os.path.join(self.workdir, "gists")
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "workbench")
        _clear_llm_env()
    def test_sbom_diff_needs_both_paths(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["sbom-diff"].run(app, {})
        self.assertIn("needs both", text)

    def test_installed_packages_needs_a_single_active_group(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["installed-packages"].run(app, {})
        # A real spec but no build yet -- no tarball to read.
        self.assertIn("no tarball", text)

    # A real var/lib/dpkg/status, not just a name-echoing tarball.
    # Same shape as tests/security/sbom.py's status_tarball(), kept local
    # rather than imported -- test files here stay self-contained.
    def _status_tarball(self, status):
        path = os.path.join(self.workdir, "root.tar")
        with tarfile.open(path, "w") as tar:
            payload = status.encode()
            info = tarfile.TarInfo("./var/lib/dpkg/status")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return path

    # 'name' searches every installed package, not just the top 30 --
    # the "is package X in my image" case this exists for, where X may
    # be far smaller than the 30th-largest package.
    def test_installed_packages_name_finds_a_small_package(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        build.image._tarball = self._status_tarball(
            "Package: sudo\nStatus: install ok installed\n"
            "Installed-Size: 10\nVersion: 1.9\n")
        try:
            text = self.ai.TOOLS["installed-packages"].run(app, {"name": "sudo"})
        finally:
            # Avoids 'Image.__del__' trying to unlink this once
            # 'self.workdir' (the tarball's own directory) is already
            # gone -- a test-only ordering issue, not a real 'Image'
            # concern (a real build's tarball outlives the object).
            build.image._tarball = None
        self.assertIn("sudo", text)
        self.assertIn("1.9", text)
        self.assertIn("10 KiB", text)

    def test_installed_packages_name_with_no_match_says_so(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        build.image._tarball = self._status_tarball(
            "Package: bash\nStatus: install ok installed\n"
            "Installed-Size: 7000\nVersion: 5.2\n")
        try:
            text = self.ai.TOOLS["installed-packages"].run(app, {"name": "sudo"})
        finally:
            build.image._tarball = None
        self.assertEqual(text, "no installed package matching 'sudo'")

    # 'name' is a regex, not a plain substring -- alternation finds
    # either of two packages in one call.
    def test_installed_packages_name_is_a_regex(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        build.image._tarball = self._status_tarball(
            "Package: sudo\nStatus: install ok installed\n"
            "Installed-Size: 10\nVersion: 1.9\n\n"
            "Package: doas\nStatus: install ok installed\n"
            "Installed-Size: 5\nVersion: 6.8\n\n"
            "Package: bash\nStatus: install ok installed\n"
            "Installed-Size: 7000\nVersion: 5.2\n")
        try:
            text = self.ai.TOOLS["installed-packages"].run(app, {"name": "sudo|doas"})
        finally:
            build.image._tarball = None
        self.assertIn("sudo", text)
        self.assertIn("doas", text)
        self.assertNotIn("bash", text)

    def test_installed_packages_name_with_bad_regex_reports_it_not_a_crash(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        build.image._tarball = self._status_tarball(
            "Package: bash\nStatus: install ok installed\n"
            "Installed-Size: 7000\nVersion: 5.2\n")
        try:
            text = self.ai.TOOLS["installed-packages"].run(app, {"name": "("})
        finally:
            build.image._tarball = None
        self.assertIn("not a usable pattern", text)

    # A real (tiny) external program configured as settings.json's
    # sbom2cve_program, exercising a real scan() call to populate the
    # cache -- the same "prove the tool reads back what /issues really
    # left behind" choice test_installed_packages_* above make with a real
    # tarball rather than a stand-in.
    def _write_scanned_sbom(self, build, findings):
        import stat
        path = os.path.join(self.workdir, "fake-scanner")
        body = "".join("print(%r)\n" % json.dumps(f) for f in findings)
        with open(path, "w") as f:
            f.write("#!/usr/bin/env python3\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

        from seine import secscan, settings, sbom
        current = settings.load()
        current["sbom2cve_program"] = path
        settings.save(current)

        sbom_path = sbom.output_path(build.image._output)
        os.makedirs(os.path.dirname(sbom_path), exist_ok=True)
        with open(sbom_path, "w") as f:
            f.write("{}")
        secscan.scan(sbom_path)  # populates the cache 'issues' reads back
        return sbom_path

    def test_issues_needs_a_single_active_group(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        text = self.ai.TOOLS["issues"].run(app, {})
        # A real spec but no scan cached yet.
        self.assertIn("no CVE scan cached", text)

    def test_issues_reads_back_a_cached_scan(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        self._write_scanned_sbom(build, [
            {"package": "libssl@3.0", "vulnerability":
                {"id": "CVE-2024-0001", "status": "open", "urgency": "high"}}])
        text = self.ai.TOOLS["issues"].run(app, {})
        self.assertIn("CVE-2024-0001", text)
        self.assertIn("libssl", text)
        self.assertIn("high", text)

    def test_issues_name_narrows_to_a_package(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        self._write_scanned_sbom(build, [
            {"package": "libssl@3.0", "vulnerability":
                {"id": "CVE-1", "status": "open", "urgency": "high"}},
            {"package": "vim@9.0", "vulnerability":
                {"id": "CVE-2", "status": "open", "urgency": "low"}}])
        text = self.ai.TOOLS["issues"].run(app, {"name": "libssl"})
        self.assertIn("CVE-1", text)
        self.assertNotIn("CVE-2", text)

    def test_issues_min_urgency_drops_less_severe_findings(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        self._write_scanned_sbom(build, [
            {"package": "libssl@3.0", "vulnerability":
                {"id": "CVE-1", "status": "open", "urgency": "high"}},
            {"package": "vim@9.0", "vulnerability":
                {"id": "CVE-2", "status": "open", "urgency": "low"}}])
        text = self.ai.TOOLS["issues"].run(app, {"min_urgency": "high"})
        self.assertIn("CVE-1", text)
        self.assertNotIn("CVE-2", text)

    def test_issues_bad_min_urgency_reports_it_not_a_crash(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        self._write_scanned_sbom(build, [
            {"package": "libssl@3.0", "vulnerability":
                {"id": "CVE-1", "status": "open", "urgency": "high"}}])
        text = self.ai.TOOLS["issues"].run(app, {"min_urgency": "critical"})
        self.assertIn("must be one of", text)

    def test_issues_caps_a_large_result_with_a_hint_to_narrow(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        build = app.context.builds[0]
        findings = [{"package": "linux@6.1", "vulnerability":
                    {"id": "CVE-%d" % i, "status": "open", "urgency": "low"}}
                   for i in range(60)]
        self._write_scanned_sbom(build, findings)
        text = self.ai.TOOLS["issues"].run(app, {})
        self.assertIn("... (10 more", text)
        self.assertEqual(text.count("CVE-"), 50)  # capped, not all 60

    # The AI-chat equivalent of 'seine cache info --entries-matching' --
    # (build/chats/20260818T180821689897.json is the transcript that
    # motivated it, mistrusting 'plan' 's "already built" line with no
    # way to check it beyond the digest alone).
    def test_cache_matching_narrows_to_the_entry_and_shows_what_built_it(self):
        from seine import cache_index
        app = self.SeineApp()
        cache_index.Index().made(cache_index.PACKAGE, "bookworm/arm64/linux")
        cache_index.Index().made(cache_index.CHROOT, "bookworm-arm64")
        stamps = os.path.join(self.workdir, "packages", "bookworm", ".stamps")
        os.makedirs(stamps, exist_ok=True)
        open(os.path.join(stamps, "linux_arm64_b41c1f8278e07eb5"), "w").close()
        excerpts = os.path.join(self.workdir, "packages", "bookworm",
                                ".stamps-spec")
        os.makedirs(excerpts, exist_ok=True)
        with open(os.path.join(excerpts,
                               "linux_arm64_b41c1f8278e07eb5.spec"), "w") as f:
            f.write("source: apt://linux\n"
                    "extends:\n"
                    "  kernel:\n"
                    "    configs:\n"
                    "      magic-sysrq:\n"
                    "      - CONFIG_MAGIC_SYSRQ=n\n")

        text = self.ai.TOOLS["cache"].run(app, {"matching": "linux"})
        self.assertIn("bookworm/arm64/linux", text)
        self.assertNotIn("bookworm-arm64", text)
        self.assertIn("CONFIG_MAGIC_SYSRQ=n", text)

    def test_cache_matching_with_bad_regex_reports_it_not_a_crash(self):
        app = self.SeineApp()
        text = self.ai.TOOLS["cache"].run(app, {"matching": "("})
        self.assertIn("not a usable pattern", text)

    # A vendor-only spec (no 'image:') is a real, supported '/use' --
    # BuildCmd.parse()'s own comment -- so this is the shape a real
    # 'vendor'/'vendor-why' call would actually see, not PC_IMAGE plus a
    # workaround.
    def _vendor_spec(self):
        path = os.path.join(self.workdir, "vendor-only.yaml")
        with open(path, "w") as f:
            f.write(
                "distribution:\n"
                "    release: bookworm\n"
                "    architecture: amd64\n"
                "    uri: http://example.com/debian\n"
                "vendor:\n"
                "    - name: openssl\n")
        return path

    def _save_openssl_manifest(self, suite):
        from seine.vendor import save_manifest
        save_manifest(suite, {
            "sources": {
                "openssl": {"version": "3.0.11-1", "direct": True,
                           "binaries": {"libssl3": {"amd64": "3.0.11-1"}}},
                "bar": {"version": "2.0-1", "direct": False,
                       "binaries": {"libbar-dev": {"amd64": "2.0-1"}}}},
            "digest": "d", "graph_version": 1,
            "graph": {"edges": [
                {"from": "openssl", "to": "bar", "via": "libbar-dev",
                 "arch": "amd64", "field": "Build-Depends",
                 "raw": "libbar-dev", "depth": 0}],
                "reverse": {"bar": [
                    {"parent": "openssl", "via": "libbar-dev", "arch": "amd64",
                     "field": "Build-Depends", "depth": 0}]},
                "pruned": {"base_chroot": [], "excluded": []}}})

    def test_vendor_reports_a_real_manifest(self):
        app = self.SeineApp(files=[self._vendor_spec()])
        self._save_openssl_manifest("bookworm")
        text = self.ai.TOOLS["vendor"].run(app, {"suite": "bookworm"})
        self.assertIn("2 source package(s)", text)
        self.assertIn("roots: openssl", text)

    # Same 'lines N-M of T' header spec-dump's own chunking uses --
    # _text_chunk() is shared, not reimplemented for this tool.
    def test_vendor_is_chunked_like_spec_dump(self):
        app = self.SeineApp(files=[self._vendor_spec()])
        self._save_openssl_manifest("bookworm")
        text = self.ai.TOOLS["vendor"].run(app, {"suite": "bookworm", "start": 1, "end": 1})
        self.assertTrue(text.startswith("lines 1-1 of "))

    def test_vendor_why_explains_a_pulled_in_package(self):
        app = self.SeineApp(files=[self._vendor_spec()])
        self._save_openssl_manifest("bookworm")
        text = self.ai.TOOLS["vendor-why"].run(
            app, {"package": "bar", "suite": "bookworm"})
        self.assertIn("extra -- pulled in", text)
        self.assertIn("openssl build-depends on libbar-dev", text)

    def test_vendor_why_requires_a_package_argument(self):
        app = self.SeineApp(files=[self._vendor_spec()])
        text = self.ai.TOOLS["vendor-why"].run(app, {})
        self.assertIn("give 'package'", text)

    def test_reset_conversation_clears_state(self):
        app = self.SeineApp()
        app.ai_state.messages.append({"role": "user", "content": "hi"})
        app.ai_state.prompt_tokens = 5
        text = self.ai.TOOLS["reset-conversation"].run(app, {})
        self.assertEqual(text, "conversation reset")
        self.assertEqual(app.ai_state.messages, [])
        self.assertEqual(app.ai_state.prompt_tokens, 0)

    def test_cancel_build_with_nothing_running(self):
        app = self.SeineApp()
        self.assertEqual(self.ai.TOOLS["cancel-build"].run(app, {}), "no build is running")

    def test_start_build_with_no_active_spec(self):
        app = self.SeineApp()
        self.assertIn("no single active specification",
                      self.ai.TOOLS["start-build"].run(app, {}))

    def test_start_vendor_with_no_active_spec(self):
        app = self.SeineApp()
        self.assertIn("no single active specification",
                      self.ai.TOOLS["start-vendor"].run(app, {}))

    def test_cancel_vendor_with_nothing_running(self):
        app = self.SeineApp()
        self.assertEqual(self.ai.TOOLS["cancel-vendor"].run(app, {}),
                         "no vendor is running")

    # start-vendor's own preview refuses before ConfirmAction ever
    # opens for the same reasons prepare()/start_vendor() would
    # otherwise error on, once approved -- same hardening cancel-build's
    # own preview already has.
    def test_start_vendor_preview_refuses_without_a_vendor_section(self):
        app = self.SeineApp(files=[NATIVE_IMAGE])
        preview = self.ai.TOOLS["start-vendor"].preview(app, {})
        self.assertFalse(preview.ok)
        self.assertIn("no 'vendor:' section", preview.message)

    def test_start_vendor_preview_refuses_an_unknown_suite(self):
        app = self.SeineApp(files=[self._vendor_spec()])
        preview = self.ai.TOOLS["start-vendor"].preview(app, {"suite": "not-a-suite"})
        self.assertFalse(preview.ok)
        self.assertIn("not a suite", preview.message)

    def test_start_vendor_preview_approves_a_real_vendor_section(self):
        app = self.SeineApp(files=[self._vendor_spec()])
        preview = self.ai.TOOLS["start-vendor"].preview(app, {})
        self.assertTrue(preview.ok)

    def test_cancel_vendor_preview_refuses_with_nothing_running(self):
        app = self.SeineApp()
        preview = self.ai.TOOLS["cancel-vendor"].preview(app, {})
        self.assertFalse(preview.ok)
        self.assertIn("no vendor is running", preview.message)

