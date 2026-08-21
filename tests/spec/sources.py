#!/usr/bin/env python3

import avocado
import contextlib
import io
import json
import os
import shutil
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine import sources
from seine.sources import SourceCmd
from seine.utils import ContainerEngine, HOST_ARCH

DISTRO = {"source": "debian", "release": "bookworm", "architecture": HOST_ARCH,
         "uri": "http://ftp.debian.org/debian"}

PC_IMAGE = os.path.join(path_to_sources, "examples", "pc-image", "main.yaml")

class Workbench(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ.pop("SEINE_WORKBENCH_DIR", None)
        os.environ.pop("SEINE_BUILD_DIR", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_SEINE_WORKBENCH_DIR_overrides_SEINE_BUILD_DIR(self):
        os.environ["SEINE_BUILD_DIR"] = "/should/be/ignored"
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "bench")
        self.assertEqual(ContainerEngine.workbench(),
                         os.path.join(self.workdir, "bench"))

    def test_falls_back_to_SEINE_BUILD_DIR(self):
        os.environ["SEINE_BUILD_DIR"] = self.workdir
        self.assertEqual(ContainerEngine.workbench(),
                         os.path.join(self.workdir, "workbench"))

    def test_the_directory_is_created(self):
        os.environ["SEINE_WORKBENCH_DIR"] = os.path.join(self.workdir, "new")
        ContainerEngine.workbench()
        self.assertTrue(os.path.isdir(os.path.join(self.workdir, "new")))

class Parse(avocado.Test):
    def test_a_bare_name_has_no_version(self):
        self.assertEqual(sources.parse("bash"), ("bash", None))

    def test_an_apt_style_pin_splits_on_the_first_equals(self):
        self.assertEqual(sources.parse("bash=5.4+deb12u2"),
                         ("bash", "5.4+deb12u2"))

class RecordAndList(avocado.Test):
    def test_an_empty_workbench_lists_nothing(self):
        self.assertEqual(sources.list_pulled(self.workdir), [])

    def test_a_recorded_source_is_listed_once_its_directory_exists(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        [(name, entry)] = sources.list_pulled(self.workdir)
        self.assertEqual(name, "bash")
        self.assertEqual({k: v for k, v in entry.items()
                          if k not in ("pulled_at", "accessed_at")},
                         {"dir": "bash-5.14", "version": "5.14-3",
                          "release": "trixie", "requested": "bash"})
        self.assertIsInstance(entry["pulled_at"], int)
        # Pulling counts as a first use, so housekeeping has a real
        # signal to start from rather than nothing at all.
        self.assertEqual(entry["accessed_at"], entry["pulled_at"])

    # 'sources' is its own key in index.json, not the whole file -- room
    # for something else under the workbench later without a reshuffle.
    def test_the_index_file_nests_entries_under_a_sources_key(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        with open(os.path.join(self.workdir, sources.INDEX_FILE)) as f:
            raw = json.load(f)
        self.assertEqual(raw["sources"]["bash"]["dir"], "bash-5.14")

    # The index can outlive its own directory -- a manual 'rm -rf', not
    # 'source rm' -- and list_pulled() drops the stale entry rather than
    # claiming a source is still there.
    def test_an_entry_whose_directory_is_gone_is_not_listed(self):
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        self.assertEqual(sources.list_pulled(self.workdir), [])

class Touch(avocado.Test):
    def test_touch_bumps_accessed_at_past_pulled_at(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        pulled_at = dict(sources.list_pulled(self.workdir))["bash"]["pulled_at"]
        sources.touch("bash", directory=self.workdir)
        entry = dict(sources.list_pulled(self.workdir))["bash"]
        self.assertGreaterEqual(entry["accessed_at"], pulled_at)

    def test_touch_of_an_unknown_name_does_nothing(self):
        sources.touch("nope", directory=self.workdir)
        self.assertEqual(sources.list_pulled(self.workdir), [])

    def test_touch_path_finds_the_owning_source_from_a_file_inside_it(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        before = dict(sources.list_pulled(self.workdir))["bash"]["accessed_at"]
        sources.touch_path(os.path.join(self.workdir, "bash-5.14", "control"),
                           directory=self.workdir)
        after = dict(sources.list_pulled(self.workdir))["bash"]["accessed_at"]
        self.assertGreaterEqual(after, before)

    def test_touch_path_outside_any_source_does_nothing(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        before = dict(sources.list_pulled(self.workdir))["bash"]["accessed_at"]
        sources.touch_path(self.workdir, directory=self.workdir)
        after = dict(sources.list_pulled(self.workdir))["bash"]["accessed_at"]
        self.assertEqual(after, before)

class Remove(avocado.Test):
    def test_removes_the_directory_and_the_index_entry(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        sources.remove("bash", directory=self.workdir)
        self.assertFalse(os.path.isdir(os.path.join(self.workdir, "bash-5.14")))
        self.assertEqual(sources.list_pulled(self.workdir), [])

    # No index entry (dropped in by hand, or the index lost) still
    # removes, by the same 'name-' prefix dpkg-source itself would have
    # used -- as long as it names exactly one directory.
    def test_falls_back_to_a_directory_prefix_match_with_no_index_entry(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.remove("bash", directory=self.workdir)
        self.assertFalse(os.path.isdir(os.path.join(self.workdir, "bash-5.14")))

    def test_an_ambiguous_prefix_is_refused(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        os.makedirs(os.path.join(self.workdir, "bash-5.15"))
        self.assertRaises(ValueError, sources.remove, "bash", directory=self.workdir)

    def test_removing_an_unknown_name_raises(self):
        self.assertRaises(ValueError, sources.remove, "nope", directory=self.workdir)

# Stands in for SourceBootstrap so pull()'s own orchestration -- the feed
# script, the before/after diff, refusing an already-pulled name -- is
# tested without a container, the same swap-in-place idiom
# tests/spec/sbom.py's Engine uses. 'effect' does whatever a real
# 'apt-get source' would have left in 'workdir'; tests set it per case.
class FakeSourceBootstrap:
    instances = []
    effect = None
    result = (0, "")

    def __init__(self, distro, options):
        self.distro, self.options = distro, options
        self.calls = []
        FakeSourceBootstrap.instances.append(self)

    def create(self):
        return self

    def exec(self, args, volumes=None, workdir=None):
        self.calls.append({"args": args, "volumes": volumes, "workdir": workdir})
        if FakeSourceBootstrap.effect:
            FakeSourceBootstrap.effect(workdir)
        return FakeSourceBootstrap.result

def leaves(*names):
    def effect(workdir):
        for name in names:
            path = os.path.join(workdir, name)
            if name.endswith("/"):
                os.makedirs(path)
            else:
                open(path, "w").close()
    return effect

class Pull(avocado.Test):
    def setUp(self):
        self.saved = sources.SourceBootstrap
        sources.SourceBootstrap = FakeSourceBootstrap
        FakeSourceBootstrap.instances = []
        FakeSourceBootstrap.effect = None
        FakeSourceBootstrap.result = (0, "")
        self.addCleanup(self._restore)

    def _restore(self):
        sources.SourceBootstrap = self.saved

    def test_configures_the_distro_s_own_feeds_before_apt_get_source(self):
        FakeSourceBootstrap.effect = leaves("bash-5.14/")
        sources.pull("bash", DISTRO, directory=self.workdir)
        script = FakeSourceBootstrap.instances[0].calls[0]["args"][-1]
        self.assertIn("deb http://ftp.debian.org/debian bookworm main", script)
        self.assertIn("deb-src http://ftp.debian.org/debian bookworm main", script)
        self.assertIn("apt-get update -qqy && apt-get source bash", script)

    def test_a_pinned_version_is_passed_to_apt_get_source(self):
        FakeSourceBootstrap.effect = leaves("bash-5.14/")
        sources.pull("bash=5.14-3", DISTRO, directory=self.workdir)
        script = FakeSourceBootstrap.instances[0].calls[0]["args"][-1]
        self.assertIn("apt-get source bash=5.14-3", script)

    def test_the_new_directory_is_recorded_and_returned(self):
        FakeSourceBootstrap.effect = leaves("bash-5.14/")
        dirname = sources.pull("bash", DISTRO, directory=self.workdir)
        self.assertEqual(dirname, "bash-5.14")
        [(name, entry)] = sources.list_pulled(self.workdir)
        self.assertEqual(name, "bash")
        self.assertEqual(entry["dir"], "bash-5.14")
        self.assertEqual(entry["version"], "5.14")
        self.assertEqual(entry["release"], "bookworm")
        self.assertEqual(entry["requested"], "bash")

    # The .dsc/orig tarball apt-get source leaves beside the unpacked
    # directory serve no purpose once dpkg-source has read them -- left
    # in place they would only accumulate, unlike the directory itself.
    def test_loose_files_beside_the_new_directory_are_removed(self):
        FakeSourceBootstrap.effect = leaves("bash-5.14/", "bash_5.14-3.dsc",
                                            "bash_5.14.orig.tar.xz")
        sources.pull("bash", DISTRO, directory=self.workdir)
        self.assertEqual(os.listdir(self.workdir),
                         ["bash-5.14", sources.INDEX_FILE])

    # apt's own failure text reaches the caller instead of an exception
    # with nothing useful in it -- the model needs this on a failed
    # source-pull, same as any other tool result.
    def test_a_failed_fetch_raises_with_apts_own_output(self):
        FakeSourceBootstrap.result = (1, "E: Unable to locate package bash\n")
        with self.assertRaises(ValueError) as caught:
            sources.pull("bash", DISTRO, directory=self.workdir)
        self.assertIn("Unable to locate package bash", str(caught.exception))

    def test_no_new_directory_is_an_error(self):
        FakeSourceBootstrap.effect = leaves("bash_5.14-3.dsc")
        self.assertRaises(ValueError, sources.pull, "bash", DISTRO,
                          directory=self.workdir)

    def test_more_than_one_new_directory_is_an_error(self):
        FakeSourceBootstrap.effect = leaves("bash-5.14/", "extra/")
        self.assertRaises(ValueError, sources.pull, "bash", DISTRO,
                          directory=self.workdir)

    def test_refuses_a_name_already_pulled(self):
        FakeSourceBootstrap.effect = leaves("bash-5.14/")
        sources.pull("bash", DISTRO, directory=self.workdir)
        self.assertRaises(ValueError, sources.pull, "bash", DISTRO,
                          directory=self.workdir)
        # Refused before anything ran a second time.
        self.assertEqual(len(FakeSourceBootstrap.instances), 1)

# 'SourceCmd' reads/writes through ContainerEngine.workbench() like a
# real user would run it -- SEINE_WORKBENCH_DIR points that at the
# test's own directory, same as tests/spec/gists.py's Cli class does
# for SEINE_GISTS_DIR. 'pull' still swaps in FakeSourceBootstrap: a real
# fetch is 'ASourceIsActuallyPulled' below, not every case here.
class Cli(avocado.Test):
    def setUp(self):
        self.environment = dict(os.environ)
        os.environ["SEINE_WORKBENCH_DIR"] = self.workdir
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self.environment)

    def test_ls_with_nothing_yet_says_so(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            SourceCmd().main(["ls"])
        self.assertIn("no sources pulled yet", out.getvalue())

    def test_ls_lists_name_version_and_release(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "bookworm",
                       "bash", directory=self.workdir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            SourceCmd().main(["ls"])
        self.assertIn("bash", out.getvalue())
        self.assertIn("5.14-3", out.getvalue())
        self.assertIn("bookworm", out.getvalue())
        self.assertIn("just now", out.getvalue())

    def test_rm_removes_it(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "bookworm",
                       "bash", directory=self.workdir)
        SourceCmd().main(["rm", "bash"])
        self.assertEqual(sources.list_pulled(self.workdir), [])

    def test_rm_of_an_unknown_name_fails_with_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                SourceCmd().main(["rm", "nope"])
        self.assertEqual(caught.exception.code, 1)

    def test_pull_needs_a_package_and_a_spec_file(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                SourceCmd().main(["pull", "bash"])
        self.assertEqual(caught.exception.code, 1)

    def test_pull_fetches_against_the_spec_s_own_distribution(self):
        saved = sources.SourceBootstrap
        sources.SourceBootstrap = FakeSourceBootstrap
        FakeSourceBootstrap.effect = leaves("bash-5.14/")
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                SourceCmd().main(["pull", "bash", PC_IMAGE])
        finally:
            sources.SourceBootstrap = saved
        self.assertIn("pulled bash", out.getvalue())
        self.assertIn(os.path.join(self.workdir, "bash-5.14"), out.getvalue())

    def test_no_action_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                SourceCmd().main([])
        self.assertEqual(caught.exception.code, 1)

    def test_unknown_action_is_an_error(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                SourceCmd().main(["frobnicate"])
        self.assertEqual(caught.exception.code, 1)

# HostBootstrap.create() and ContainerEngine.run_captured() are patched
# directly on the class (both are called through the class, never an
# instance, so a plain function works either way) rather than swapped
# module references -- bash() reaches ContainerEngine through
# 'from seine.utils import ContainerEngine', the same class object, so
# patching the attribute on the class is what actually takes effect.
class Bash(avocado.Test):
    def setUp(self):
        self.saved_create = sources.HostBootstrap.create
        self.saved_run = ContainerEngine.run_captured
        sources.HostBootstrap.create = lambda self: self
        self.addCleanup(self._restore)

    def _restore(self):
        sources.HostBootstrap.create = self.saved_create
        ContainerEngine.run_captured = self.saved_run

    def _fake(self, output="", returncode=0):
        seen = {}
        def run_captured(cmd):
            seen["cmd"] = cmd
            return returncode, output
        ContainerEngine.run_captured = staticmethod(run_captured)
        return seen

    def test_refuses_a_cwd_that_escapes_the_workbench(self):
        self.assertRaises(ValueError, sources.bash, "true", DISTRO,
                          cwd="../escape", directory=self.workdir)

    def test_refuses_a_cwd_that_is_not_a_directory(self):
        self.assertRaises(ValueError, sources.bash, "true", DISTRO,
                          cwd="nope", directory=self.workdir)

    def test_runs_against_the_workbench_by_default(self):
        seen = self._fake("hello\n")
        text = sources.bash("echo hello", DISTRO, directory=self.workdir)
        self.assertEqual(text, "hello")
        self.assertEqual(seen["cmd"][seen["cmd"].index("-w") + 1], self.workdir)

    def test_cwd_narrows_the_working_directory(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        seen = self._fake()
        sources.bash("true", DISTRO, cwd="bash-5.14", directory=self.workdir)
        self.assertEqual(seen["cmd"][seen["cmd"].index("-w") + 1],
                         os.path.join(self.workdir, "bash-5.14"))

    def test_cwd_under_a_pulled_source_bumps_its_accessed_at(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        sources.record("bash", "bash-5.14", "5.14-3", "trixie",
                       "bash", directory=self.workdir)
        before = dict(sources.list_pulled(self.workdir))["bash"]["accessed_at"]
        self._fake()
        sources.bash("true", DISTRO, cwd="bash-5.14", directory=self.workdir)
        after = dict(sources.list_pulled(self.workdir))["bash"]["accessed_at"]
        self.assertGreaterEqual(after, before)

    def test_a_nonzero_exit_is_appended_not_swallowed(self):
        self._fake("oops\n", returncode=1)
        text = sources.bash("false", DISTRO, directory=self.workdir)
        self.assertIn("oops", text)
        self.assertIn("(exit status 1)", text)

    def test_output_past_the_cap_keeps_only_the_tail(self):
        many = "\n".join("line %d" % i for i in range(300))
        self._fake(many)
        text = sources.bash("seq 300", DISTRO, directory=self.workdir)
        self.assertIn("truncated", text)
        self.assertIn("line 299", text)
        self.assertNotIn("line 0\n", text)

class ABashCommandIsActuallyRun(avocado.Test):
    """
    :avocado: tags=container
    """
    def setUp(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to run a real container")

    def test_reads_a_file_under_the_bind_mounted_workbench(self):
        os.makedirs(os.path.join(self.workdir, "bash-5.14"))
        with open(os.path.join(self.workdir, "bash-5.14", "control"), "w") as f:
            f.write("Source: bash\n")
        text = sources.bash("cat bash-5.14/control", DISTRO, directory=self.workdir)
        self.assertEqual(text, "Source: bash")

    # A host path outside the one bind mount (the workbench) is simply
    # absent from the container's own filesystem -- not a permission
    # refusal, proof the trust boundary is the bind mount itself and
    # nothing wider.
    def test_cannot_reach_a_host_path_outside_the_bind_mount(self):
        import tempfile
        marker = tempfile.NamedTemporaryFile(
            prefix="seine-source-bash-test-", delete=False)
        try:
            marker.write(b"host-only\n")
            marker.close()
            text = sources.bash("cat %s" % marker.name, DISTRO,
                                directory=self.workdir)
            self.assertIn("No such file or directory", text)
        finally:
            os.remove(marker.name)

class ASourceIsActuallyPulled(avocado.Test):
    """
    :avocado: tags=container
    """
    # Building the host-arch source image on top of a fresh pull is what
    # this has to leave room for.
    timeout = 600

    def test(self):
        if shutil.which("podman") is None:
            self.cancel("podman is needed to fetch a real source package")

        # 'hello' -- small, Priority: optional, always in main -- so this
        # stays a quick, reliable check rather than a real workload.
        dirname = sources.pull("hello", DISTRO, directory=self.workdir)
        self.assertTrue(os.path.isfile(
            os.path.join(self.workdir, dirname, "debian", "control")))
        [(name, entry)] = sources.list_pulled(self.workdir)
        self.assertEqual(name, "hello")
        self.assertEqual(entry["dir"], dirname)

if __name__ == "__main__":
    avocado.main()
