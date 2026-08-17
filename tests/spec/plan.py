#!/usr/bin/env python3

import avocado
import io
import os
import sys
import yaml

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.build import ADDED, REMOVED, RESET, BuildCmd, PlanCmd
from seine.build import diff, recall, remember
from seine.image import Image

SPEC = """
distribution:
  release: bookworm
  architecture: amd64
image:
  filename: demo.img
  size: 1024MiB
  partitions:
    - label: root
      type: ext4
      where: /
playbook:
  - name: packages
    tasks:
      - name: install vim
        apt: {name: vim, state: present}
"""

OLD = "image:\n  filename: demo.img\n  size: 2G\n"
NEW = "image:\n  filename: demo.img\n  size: 4G\n"

def marks(said):
    return [line for line in said.splitlines() if line[:1] in ["+", "-"]]

# A plan prints the whole specification, with what changed since the last
# build of the same files marked inside it -- not only the hunks around it.
class WhatChangedSinceTheLastBuild(avocado.Test):
    def test_the_whole_specification_is_printed(self):
        said = diff(OLD, NEW, color=False)
        self.assertIn(" image:", said)
        self.assertIn("   filename: demo.img", said)
        self.assertEqual(marks(said), ["-  size: 2G", "+  size: 4G"])

    # A specification whose every line is called new says nothing, so with
    # nothing to compare against nothing is marked.
    def test_no_baseline_marks_nothing(self):
        said = diff(None, NEW, color=False)
        self.assertEqual(marks(said), [])
        self.assertEqual(len(said.splitlines()), len(NEW.splitlines()))
        self.assertNotIn("...", said)

    # Padded to the width of the terminal, so the block has a shape.
    def test_a_changed_line_is_a_full_width_bar(self):
        said = diff(OLD, NEW, color=True, width=40).splitlines()
        added = [line for line in said if line.startswith(ADDED)][0]
        removed = [line for line in said if line.startswith(REMOVED)][0]
        self.assertEqual(added, "%s%s%s" % (ADDED, "+  size: 4G".ljust(40), RESET))
        self.assertIn("2G", removed)
        # And an unchanged line is not a bar at all.
        self.assertIn("image:", said[0])
        self.assertNotIn("\x1b", said[0])

    # What a specification says matters more than the shape of the block.
    def test_a_long_line_is_not_cut(self):
        long = "image:\n  filename: %s.img\n" % ("x" * 100)
        said = diff("", long, color=True, width=40)
        self.assertIn("x" * 100, said)

# Setting by setting rather than line by line: a line diff of YAML calls a
# shifted indentation a change, and a list that grew near the top a rewrite
# from there down.
class ComparedByWhatItSaysAndNotByItsLines(avocado.Test):
    # The items around it did not move, whatever their lines did.
    def test_only_the_setting_that_changed_is_marked(self):
        old = ("image:\n  partitions:\n"
               "  - label: efi\n    size: 16\n"
               "  - label: boot\n    size: 128\n")
        new = old.replace("size: 128", "size: 256")
        self.assertEqual(marks(diff(old, new, color=False)),
                         ["-      size: 128", "+      size: 256"])

    # An item added, not every item from there on rewritten.
    def test_an_item_inserted_first(self):
        old = "image:\n  partitions:\n  - label: boot\n    size: 128\n"
        new = ("image:\n  partitions:\n"
               "  - label: efi\n    size: 16\n"
               "  - label: boot\n    size: 128\n")
        self.assertEqual(marks(diff(old, new, color=False)),
                         ["+    - label: efi", "+      size: 16"])

    # Two settings changed rather than one, so each is marked where the
    # section has it rather than one against the other.
    def test_a_setting_gained_and_a_setting_lost(self):
        old = "image:\n  filename: demo.img\n  table: msdos\n"
        new = "image:\n  filename: demo.img\n  size: 4G\n"
        self.assertEqual(marks(diff(old, new, color=False)),
                         ["+  size: 4G", "-  table: msdos"])

    def test_a_value_that_changed_is_the_old_line_then_the_new(self):
        old = "image:\n  filename: demo.img\n  table: msdos\n"
        new = "image:\n  filename: demo.img\n  table: gpt\n"
        self.assertEqual(marks(diff(old, new, color=False)),
                         ["-  table: msdos", "+  table: gpt"])

    def test_an_item_replaced_is_the_old_one_then_the_new(self):
        old = "image:\n  partitions:\n  - label: efi\n"
        new = "image:\n  partitions:\n  - label: boot\n"
        self.assertEqual(marks(diff(old, new, color=False)),
                         ["-    - label: efi", "+    - label: boot"])

    # An item nothing matches is gone, and says so once.
    def test_an_item_removed(self):
        old = ("image:\n  partitions:\n"
               "  - label: efi\n    size: 16\n"
               "  - label: boot\n    size: 128\n")
        new = "image:\n  partitions:\n  - label: efi\n    size: 16\n"
        self.assertEqual(marks(diff(old, new, color=False)),
                         ["-    - label: boot", "-      size: 128"])

    # Nothing changed is nothing marked, however deep the specification is.
    def test_the_same_specification_twice(self):
        same = ("distribution:\n  release: trixie\n"
                "image:\n  partitions:\n  - label: efi\n    flags:\n    - boot\n")
        self.assertEqual(marks(diff(same, same, color=False)), [])

# A specification is hundreds of lines and a change to it is a few, so what
# did not change is folded away once there is something to point at.
class WhatDidNotChangeIsFoldedAway(avocado.Test):
    def spec(self, size):
        return yaml.dump({"image": {"filename": "demo.img", "size": size,
                                    "table": "gpt",
                                    "partitions": [{"label": "p%d" % n,
                                                    "size": n, "type": "ext4",
                                                    "where": "/p%d" % n}
                                                   for n in range(20)]}})

    def test_a_run_of_unchanged_lines_is_counted(self):
        said = diff(self.spec("2G"), self.spec("4G"), color=False)
        folds = [line for line in said.splitlines() if "..." in line]
        self.assertGreater(len(folds), 0)
        self.assertIn("unchanged", folds[0])
        # And it is shorter than the specification it stands for.
        self.assertLess(len(said.splitlines()),
                        len(self.spec("4G").splitlines()) / 2)

    # Every change, the lines around it, and the keys it sits under stay.
    def test_a_change_keeps_its_context_and_its_keys(self):
        said = diff(self.spec("2G"), self.spec("4G"), color=False).splitlines()
        self.assertEqual(marks("\n".join(said)),
                         ["-  size: 2G", "+  size: 4G"])
        self.assertIn(" image:", said)
        self.assertIn("   table: gpt", said)

    # A change deep in a list still says which list it is in.
    def test_a_change_in_a_list_keeps_the_key_above_it(self):
        old = self.spec("2G")
        new = old.replace("label: p9", "label: p9x")
        said = diff(old, new, color=False).splitlines()
        self.assertIn("   partitions:", said)
        self.assertIn("+      label: p9x", said)

    # What is marked is the setting, not the '-' introducing the item.
    def test_an_item_is_not_marked_for_what_is_inside_it(self):
        old = self.spec("2G")
        new = old.replace("label: p9", "label: p9x")
        said = diff(old, new, color=False).splitlines()
        self.assertIn("     -", said)
        self.assertEqual(marks("\n".join(said)),
                         ["-      label: p9", "+      label: p9x"])

    # Nothing to point at, so every line is printed.
    def test_nothing_changed_is_not_folded(self):
        same = self.spec("2G")
        said = diff(same, same, color=False)
        self.assertNotIn("...", said)
        self.assertEqual(len(said.splitlines()), len(same.splitlines()))

# The baseline is filed under the files the build was asked for, so that a
# plan of the same command compares against what that command last built.
class WhatTheseFilesLastBuilt(avocado.Test):
    def test_it_is_read_back_for_the_same_files(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        files = [os.path.join(self.workdir, "demo.yml")]
        self.assertIsNone(recall(files))
        remember(files, NEW)
        self.assertEqual(recall(files), NEW)

    # Other files are another plan, whatever they build.
    def test_other_files_are_another_plan(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        remember([os.path.join(self.workdir, "demo.yml")], NEW)
        self.assertIsNone(recall([os.path.join(self.workdir, "other.yml")]))

    # An unreadable baseline is no baseline: the specification still
    # prints, with nothing marked in it.
    def test_a_baseline_that_cannot_be_read(self):
        said = diff("image: [", NEW, color=False)
        self.assertEqual(marks(said), [])
        self.assertIn("   filename: demo.img", said)

# A build records what the next plan is read against, so build, edit, plan
# has to answer with the edit. The build itself is containers and is stood
# in for; what is under test is what it leaves behind.
class ABuildRecordsWhatTheNextPlanReadsAgainst(avocado.Test):
    def setUp(self):
        os.environ["SEINE_CACHE_DIR"] = self.workdir
        built, pruned = Image.build, BuildCmd._prune
        # A build that finished returns nothing -- a stub returning 0 would
        # agree with a check that is wrong -- and writes into every playbook
        # as it goes, as the ansible runner does.
        Image.build = lambda image, reporter=None: self.building(image)
        BuildCmd._prune = lambda command: None
        self.addCleanup(setattr, Image, "build", built)
        self.addCleanup(setattr, BuildCmd, "_prune", pruned)
        self.spec = os.path.join(self.workdir, "demo.yml")
        self.written(SPEC)

    # A build, as far as the specification is concerned.
    def building(self, image, failing=None):
        for playbook in image.spec.get("playbook") or []:
            playbook["environment"] = {"INITRD": "No"}
        return failing

    def written(self, text):
        with open(self.spec, "w") as f:
            f.write(text)

    def ran(self, command, argv):
        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            command().main(argv)
        except SystemExit:
            pass
        finally:
            sys.stdout = stdout
        return out.getvalue()

    # A build that finished leaves one behind, whatever it returned.
    def test_a_build_leaves_a_baseline(self):
        self.ran(BuildCmd, [self.spec])
        self.assertIsNotNone(recall([self.spec]))

    # And a build that failed does not: what it half-built is not what the
    # next plan reads against.
    def test_a_build_that_failed_leaves_none(self):
        Image.build = lambda image, reporter=None: self.building(image, failing=4)
        self.ran(BuildCmd, [self.spec])
        self.assertIsNone(recall([self.spec]))

    def test_the_edit_between_a_build_and_a_plan(self):
        self.ran(BuildCmd, [self.spec])
        self.written(SPEC.replace("vim", "emacs"))
        said = self.ran(PlanCmd, ["--spec-only", self.spec])
        self.assertIn("+          name: emacs", said.splitlines())
        self.assertIn("-          name: vim", said.splitlines())

    # The same files unedited would build what they built.
    def test_nothing_edited_is_nothing_marked(self):
        self.ran(BuildCmd, [self.spec])
        self.assertEqual(marks(self.ran(PlanCmd, ["--spec-only", self.spec])), [])

    # A plan compares against what was built, not what was last asked about.
    def test_a_plan_is_not_a_baseline(self):
        self.ran(PlanCmd, ["--spec-only", self.spec])
        self.assertIsNone(recall([self.spec]))

# A specification holds passwords and keys, and printing it is showing them
# to whoever is looking at the terminal. What a fragment says to redact is
# taken out of what is printed, and out of that only.
class WhatAFragmentSaysNotToPrint(avocado.Test):
    SECRET = "$6$X1SbKPWJ2tkpDFZb$khtcnptnTxWEYA4"
    SPEC = ("redact:\n  - \\$6\\$\\S+\n"
            "playbook:\n  - name: accounts\n    tasks:\n"
            "      - name: set root password\n"
            "        user: name=root update_password=always password=%s\n") % SECRET

    def dumped(self, text=None):
        build = BuildCmd()
        build.loads(text or self.SPEC)
        return build, build.dump(build.spec)

    def test_a_secret_is_not_printed(self):
        build, said = self.dumped()
        self.assertNotIn(self.SECRET, said)
        self.assertIn("password=<redacted:", said)
        # and the build still has it: what is hidden is what is printed.
        self.assertIn(self.SECRET, yaml.dump(build.spec))

    # The rest of the value is worth reading, so only the match goes.
    def test_the_value_around_it_stays(self):
        self.assertIn("name=root update_password=always", self.dumped()[1])

    # A plan compares one dump against another, so a constant would have a
    # changed password read as no change at all.
    def test_a_changed_secret_is_a_changed_line(self):
        said = self.dumped()[1]
        other = self.dumped(self.SPEC.replace("X1Sb", "X2Sb"))[1]
        marked = marks(diff(said, other, color=False))
        self.assertEqual(len(marked), 2)
        # what the line changed to is still not the secret.
        self.assertNotIn("X2Sb", "\n".join(marked))

    # And the same secret twice is the same line: the digest is of the
    # value, not of where it sits.
    def test_the_same_secret_is_the_same_line(self):
        self.assertEqual(self.dumped()[1], self.dumped()[1])

    # The patterns say what a reader is not being shown, and one of them
    # matching itself would hide that too.
    def test_the_section_itself_is_printed(self):
        self.assertIn("\\$6\\$\\S+", self.dumped()[1])

    # The fragment holding a secret is rarely the file a build names, so
    # what it says to redact is gathered from every file rather than taken
    # from the last one to say it.
    def test_it_is_merged_from_every_file(self):
        build = BuildCmd()
        build.loads("distribution:\n  release: trixie\n")
        build.loads(self.SPEC)
        build.loads("redact:\n  - AKIA[0-9A-Z]+\n"
                    "image:\n  filename: AKIA0123456789.img\n")
        said = build.dump(build.spec)
        self.assertNotIn(self.SECRET, said)
        self.assertIn("filename: <redacted:", said)
        self.assertNotIn("AKIA0123456789", said)

    # An expression that is not one is reported against the section that
    # holds it rather than as a traceback out of the middle of a dump.
    def test_a_pattern_that_is_not_one(self):
        with self.assertRaises(ValueError) as raised:
            self.dumped("redact:\n  - '['\nimage:\n  filename: demo.img\n")
        self.assertIn("redact:", str(raised.exception))

    # Nothing said, nothing hidden.
    def test_a_specification_without_the_section(self):
        self.assertIn("filename: demo.img", self.dumped(SPEC)[1])

if __name__ == "__main__":
    avocado.main()
