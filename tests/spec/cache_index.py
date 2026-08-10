#!/usr/bin/env python3

import avocado
import json
import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

from seine.cache_index import Index, since, CHROOT, IMAGE, PACKAGE

class AnIndex(avocado.Test):
    def index(self):
        return Index(os.path.join(self.workdir, "index.json"))

class WhatWasTakenAndWhatWasMade(AnIndex):
    def test(self):
        index = self.index()
        index.made(CHROOT, "bookworm-amd64")
        index.hit(CHROOT, "bookworm-amd64")
        index.hit(CHROOT, "bookworm-amd64")

        (kind, key, entry), = index.entries()
        self.assertEqual((kind, key), (CHROOT, "bookworm-amd64"))
        # Making it is not using it: a build that made a chroot did not
        # find one, which is the whole distinction being recorded.
        self.assertEqual(entry["uses"], 2)
        self.assertIsNotNone(entry["made"])

    # Making it again starts the count over: it is not the same chroot.
    def test_making_it_again_forgets_the_uses(self):
        index = self.index()
        index.made(PACKAGE, "linux")
        index.hit(PACKAGE, "linux")
        index.made(PACKAGE, "linux")
        (_, _, entry), = index.entries()
        self.assertEqual(entry["uses"], 0)

class TheOldestUseComesFirst(AnIndex):
    def test(self):
        index = self.index()
        for key in ["first", "second", "third"]:
            index.made(IMAGE, key)
        # Reached for in the other order, which is what decides it.
        for key in ["third", "second"]:
            index.hit(IMAGE, key)

        keys = [key for _, key, _ in index.entries()]
        self.assertEqual(keys[0], "first")

    def test_what_is_gone_is_not_reported(self):
        index = self.index()
        index.made(IMAGE, "kept")
        index.made(IMAGE, "removed")
        keys = [key for _, key, _ in
                index.entries(present=lambda kind, key: key == "kept")]
        self.assertEqual(keys, ["kept"])

# A build decides nothing from the index, so an index that cannot be read
# is an index with nothing in it rather than a build that stops.
class AnIndexThatCannotBeReadIsEmpty(AnIndex):
    def test_not_there(self):
        self.assertEqual(self.index().entries(), [])

    def test_not_json(self):
        index = self.index()
        with open(os.path.join(self.workdir, "index.json"), "w") as f:
            f.write("this is not an index")
        self.assertEqual(index.entries(), [])
        # And it is written over rather than complained about.
        index.made(CHROOT, "trixie-arm64")
        self.assertEqual(len(index.entries()), 1)

    def test_not_a_dictionary(self):
        with open(os.path.join(self.workdir, "index.json"), "w") as f:
            json.dump(["not", "a", "dictionary"], f)
        self.assertEqual(self.index().entries(), [])

class WhatTravelsToAnotherMachine(AnIndex):
    def test(self):
        index = self.index()
        index.made(CHROOT, "bookworm-amd64")
        index.hit(CHROOT, "bookworm-amd64")

        carried = index.stripped()
        # When it was made travels; what this machine did with it does not.
        self.assertEqual(list(carried[CHROOT]), ["bookworm-amd64"])
        self.assertEqual(list(carried[CHROOT]["bookworm-amd64"]), ["made"])

    def test_what_arrives_has_been_used_by_nobody_here(self):
        theirs = {CHROOT: {"bookworm-amd64": {"made": 1000, "used": 2000,
                                              "uses": 41}}}
        index = self.index()
        index.merge(theirs)

        (_, _, entry), = index.entries()
        self.assertEqual(entry["made"], 1000, "when it was made was not kept")
        self.assertEqual(entry["uses"], 0, "someone else's uses were taken")
        self.assertGreater(entry["used"], 2000,
                           "someone else's last use was taken")

    def test_an_index_that_did_not_travel_is_no_error(self):
        index = self.index()
        index.merge(None)
        self.assertEqual(index.entries(), [])

class HowLongAgoItWas(avocado.Test):
    def test(self):
        self.assertEqual(since(None), "never")
        self.assertEqual(since(1000, now=1000), "just now")
        self.assertEqual(since(1000, now=1000 + 90), "1m ago")
        self.assertEqual(since(1000, now=1000 + 7200), "2h ago")
        self.assertEqual(since(1000, now=1000 + 3 * 86400), "3d ago")
        # A clock that went backwards is not a negative age.
        self.assertEqual(since(2000, now=1000), "just now")

if __name__ == "__main__":
    avocado.main()

# What a build reused and what it made, which is the answer to 'is the cache
# working' and the thing a build was never able to say.
class WhatABuildReusedAndMade(AnIndex):
    def setUp(self):
        from seine import cache_index
        cache_index._counted.clear()

    def test(self):
        from seine import cache_index
        index = self.index()
        index.made(CHROOT, "bookworm-amd64")
        index.hit(IMAGE, "builder/debian/bookworm")
        index.hit(IMAGE, "bootstrap/debian/bookworm/all")
        index.hit(PACKAGE, "bookworm/amd64/linux")

        said = cache_index.summary()
        self.assertIn("reused: 2 images, 1 package", said)
        self.assertIn("made: 1 chroot", said)

    # Nothing decided, nothing to report: a build that touched no cache has
    # no line to end with.
    def test_a_build_that_decided_nothing(self):
        from seine import cache_index
        self.assertIsNone(cache_index.summary())

    # One of a thing is not '1 images'.
    def test_one_of_something(self):
        from seine import cache_index
        self.index().hit(CHROOT, "trixie-arm64")
        self.assertIn("reused: 1 chroot;", cache_index.summary())

    # 'downloads' is already plural and stays that way.
    def test_what_is_already_plural(self):
        from seine import cache_index
        from seine.cache_index import DOWNLOADS
        self.index().hit(DOWNLOADS, "bookworm")
        self.assertIn("1 downloads", cache_index.summary())
