## Building

What a specification says is one thing; how the build that reads it
runs is another. These apply whatever is being built.

### What a build would do

`seine plan` prints the specification the files merge into, and then the
steps a build of it would run, in the order it would run them and with what
each waits for, and the packages it would leave alone:

```
$ seine plan spec.yaml
 distribution:
   architecture: amd64
   release: trixie
 image:
   filename: pc-image.img
-  size: 2048MiB
+  size: 3072MiB
   table: gpt
 ...

would build 'pc-image.img' for trixie/amd64

already built, and not built again:
  linux                          linux_3ffc6a528133f4bf

'--rebuild' builds them anyway.

steps:
  bootstrap-host
  bootstrap-target         after bootstrap-host
  ...
```

The specification is the whole of it, not the part that changed, since that
is what would be built. What changed is marked against the specification
these same files last built: on a terminal, added lines sit on a green bar
and removed lines on a red one; anywhere else they carry a `+` or a `-`, so
a plan saved to a file or piped into something reads the same. Only a build
records one, so files that have never built here have nothing to compare
against: their specification is printed as it is, with a line on stderr
saying why nothing in it is marked.

The comparison is setting by setting rather than line by line, and list
items are matched by the name they go by -- a partition's `label`, a
playbook's `name`, a feed's `suite`. A partition inserted at the top of the
list is one partition added, not every partition below it rewritten, and a
size that changed is that one line. What is marked is what changed.

Once something has changed, what did not is folded away: the lines around a
change stay, and so do the keys it sits under, so that what is printed can
still be read as a place in the specification.

```
 image:
   partitions:
     ... (13 lines unchanged)
     - label: system
       size: 3040870400
-  size: 2048MiB
+  size: 3072MiB
   table: gpt
 ... (56 lines unchanged)
```

A plan with nothing to point at -- files that have never built here, or
files that would build what they last built -- folds nothing and prints the
specification whole. `seine build --dump` prints it whole in any case,
without comparing it to anything.

Its options say how the plan is printed rather than what is in it:
`--spec-only` and `--tasks-only` for one half of it, `--no-color` (or
`NO_COLOR` in the environment) for a specification without the bars. For the
plan of a build with particular options -- `--jobs`, `--rebuild`,
`--packages-only`, `--rootfs-only` -- `seine build --dry-run` is the same
thing and takes them all:

```
$ seine build --dry-run --jobs 4 spec.yaml
would build 'pc-image.img' for trixie/amd64, 4 steps at a time

already built, and not built again:
  linux                          linux_3ffc6a528133f4bf

'--rebuild' builds them anyway.

steps:
  bootstrap-host
  bootstrap-target         after bootstrap-host
  packages                 after bootstrap-host
  rootfs                   after bootstrap-target, packages
  ...
```

Nothing is fetched, built or written. A package that was already built
from exactly these inputs has no steps at all -- which is the useful part
of the answer, and why the steps that are listed are the work that
remains rather than the work in general.

### While a build runs

A build says what it is doing rather than printing everything it does.
Each step's output goes to a file of its own, under a directory named
when the build starts, and the terminal shows what is running, for how
long, and how much is left:

```
✔ bootstrap-host               0s
✔ packages-prepare             6s
  ⠹ package:linux              12m04s
  ⠋ rootfs                     1m22s
  6/13 done, 2 running
```

A step that finishes leaves its line behind; only what is still running
is redrawn. A step that fails prints its output, since nobody saw it go
by.

`--verbose` puts the raw output back and turns the display off: when a
build is going wrong, that is what is wanted and no summary replaces it.
Output that is not a terminal -- a pipe, a file, a CI log -- gets one
line per step instead of a redrawn area, and a terminal that cannot print
the characters above gets plain ones.

### Keeping a failed build's containers

A step that fails removes the container it was working in, and that
container is where podman kept what it recorded about the commands run
inside it. `SEINE_KEEP_DEAD_CONTAINERS` leaves them alone instead, and
seine names the ones it kept when it exits:

```
kept 1 container(s) SEINE_KEEP_DEAD_CONTAINERS asked for:
  414410d4702b7a188ae587257da0a1da35cdf1eca92815997ae252028b210a58
  remove them with: podman --root ./build/containers rm -f 414410d...
```

The removal command names seine's own storage, since a plain `podman rm`
looks in the default one and reports no such container.

Only containers a *failed* step left behind: one a step is simply
finished with is evidence of nothing, and keeping those would fill the
disk. Nothing reaps what this keeps -- it is for reading a failure that
has just happened, not something to leave set.

### Building in parallel

A build is a handful of steps -- bootstraps, one task per package, the
root file-system, the imager appliance, the image -- and most of them do
not depend on each other. `--jobs N` runs up to N of them at once:

```
seine build --jobs 4 spec.yaml
```

Every package is four of those steps: fetching its source, preparing it
(patches, configuration, the source package itself), building it, and
publishing what came out into the local repository.
A fetch waits on a server rather than on the machine, so a large download
runs beside the compiles of other packages instead of holding a slot to
wait -- and a source that cannot be fetched says so early, rather than
once everything before it has been built. A kernel's upstream tree, which
is the largest thing seine downloads, comes down with the packaging it
will be grafted into.

Two packages naming the same source share one fetch between them --
seen in `seine plan` as one `fetch:` task feeding two `prepare:` ones --
so listing a kernel twice under different names does not pay for the
download twice.

Publishing is separate because a package built against another needs that
one's `.deb` in the repository, which sbuild installs from -- a later
moment than its build finishing, and the one a dependency actually waits
for.

One by default, which is the order and the output seine has always had.
Two knobs decide how the machine is divided between what is running:

| flag | what it sets | default |
| ---- | ------------ | ------- |
| `--jobs N` | steps of the build running at once | `1` |
| `--parallel N` | cores one package build may use | the machine divided by `--jobs` |

The default for the second is what keeps the first from being a trap.
Raising `--jobs` alone divides the cores rather than multiplying them:
four builds each helping themselves to every core is a load average of
four times the machine and a build that finishes nothing. Set
`--parallel` explicitly to oversubscribe on purpose -- for builds that
wait on I/O rather than on the CPU -- and set `parallel=1` in a
package's own `options` for packaging whose build is broken in parallel,
which beats both.

While more than one step is running, each writes to a file of its own,
under a directory the build names when it starts, so two kernels do not
interleave into something unreadable. A step that fails has its output
printed, since nobody saw it go by.

When a step fails, nothing new is started -- not what depended on it, not
what was merely queued -- and what is already running is left to finish
rather than killed. seine tidies up at the end of a step: a stamp is
written only once a package built, a half-made chroot deletes itself, a
partial disk image is removed. Killing a task skips all of that to save
minutes of a build that has already failed, and leaves the mess for the
next one. The failure is reported with the steps that never ran.

### Building several images together

`--` separates groups of specification files, each the same thing `seine
build` already takes one of, and runs them together under one scheduler
instead of one `seine build` per image:

```
seine build --jobs 4 \
    common/trixie.yaml common/amd64.yaml common/pc-image.yaml pc-image/main.yaml \
    -- \
    common/trixie.yaml common/arm64.yaml common/rpi4-image.yaml rpi4-image/main.yaml
```

What is shared between groups is worked out from what their
specifications say, not declared. Two groups agreeing on a release and
an architecture share one build of the packages they both ask for; two
groups merely agreeing on a release still share the host bootstrap,
which does not vary by architecture. Naming the same package
differently between two groups is refused rather than built once and
handed to both -- give one of them a different `name`. Everything else
-- the target bootstrap onward -- is built once per image, though two
images that happen to bootstrap the same root file-system still share
that work through the cache, the same as two separate `seine build`s
would.

`seine plan`/`--dry-run` shows the sharing in the step names themselves:
a step shared between groups is unprefixed or named for what it is
shared by, a step that is not is prefixed with the image that owns it
-- `pc-image:rootfs`, not `rootfs`, once there is more than one image to
tell apart:

```
$ seine plan --tasks-only spec-a.yaml -- spec-b.yaml
steps:
  bootstrap-host
  trixie-amd64:packages    after bootstrap-host
  trixie-arm64:packages    after bootstrap-host
  pc-image:bootstrap-target after bootstrap-host
  rpi4-image:bootstrap-target after bootstrap-host
  pc-image:rootfs          after pc-image:bootstrap-target, trixie-amd64:packages
  rpi4-image:rootfs        after rpi4-image:bootstrap-target, trixie-arm64:packages
  ...
```

A single group -- no `--` anywhere -- builds exactly as it always has,
with none of these names: the sharing only shows up once there is
something to share.

### Two operating systems on one disk

`--`-separated groups (above) each write their own image. `multiconfig:`
is different: it names sub-builds *inside* one specification, so several
independently-packaged root file-systems land side by side on the one
disk that specification's own `image:` describes.

```yaml
multiconfig:
    main:
        - examples/main-recovery-image/main.yaml
    recovery:
        - examples/main-recovery-image/recovery.yaml

image:
    partitions:
        - label: esp
          type: vfat
          source: main
          where: /efi
        - label: main-root
          source: main
          where: /
        - label: recovery-root
          source: recovery
          where: /
```

Each group's file list loads exactly like any other specification -- its
own `distribution:`/`packages:`/`playbook:`, built and cached the same
way. `source:` on a partition or volume routes that mount's content to a
declared group's own root file-system instead of this specification's
own (`source:` absent, today's only behaviour); a specification with no
`multiconfig:` key is untouched. A group needs exactly one mount naming
it with `where: "/"` -- zero or more than one is a parse-time error,
since groups are side-by-side operating systems, not partitions of one.
A group's own `image:` section, if it has one, is ignored when pulled in
this way -- the outer specification's `image:` is the only one that
describes the disk, which is also why the same file works both ways:
`seine build examples/main-recovery-image/main.yaml` builds `main` alone
(a tarball, since read this way it has no `image:` of its own), and
`disk.yaml`'s `multiconfig:` pulls that same file in as one of the
disk's two groups.

Booting a disk built this way gets one GRUB menu entry per group,
authored by seine itself rather than left to `update-grub`'s own
auto-discovery: each entry searches for its own group's root partition
by label and boots straight off that group's own `/boot`, reading across
partitions -- a group's kernel/initrd are never copied onto the shared
EFI System Partition. One group is the boot-owner (`main` by default,
`imager: boot: <name>` to name another); it is the only one `grub-install`
runs for, and its entry is the static default -- there is no seine-side
logic to switch it later. This is EFI-only for now, the same as seine's
own single-rootfs GRUB support.

`examples/main-recovery-image/` is a working `main`/`recovery` pair
built this way -- `seine build examples/main-recovery-image/disk.yaml`
produces the whole disk.

### Ordering groups that are not side-by-side OSes

The dual-OS pair above needs no ordering between `main` and `recovery`:
neither reads anything the other produces. A pipeline does -- building a
Unified Kernel Image needs its `initrd:` deployed first, and installing
that UKI into a root file-system needs the UKI package built first.
Write each stage as its own group, and say which one has to come first:

```yaml
multiconfig:
    initrd:
        - examples/minimal-initrd/main.yaml
    uki:
        files:
            - examples/minimal-uki/main.yaml
        after:
            - initrd
```

A group's value is still a bare file list when it names no ordering (as
`main`/`recovery` above); naming one switches it to a mapping with
`files:` and `after:` and/or `before:`, each a list of other groups'
names -- the same two words, and the same "both directions say one
thing" relationship, that `packages: after:`/`before:` already has
between packages within one `packages:` list. Naming a group that is not
one of this specification's own `multiconfig:` keys, naming a group
itself, or a circle of groups naming each other, is a parse-time error.

Ordering a group `after:` another only serializes the two groups' own
task graphs against each other -- coarse, not "wait for this one file",
so a heavier predecessor still costs the same wall-clock time it would
running alone. `extends: uki: initrd:` is deliberately lenient about
*when* its named `initrd:` artifact has to exist: a group with no
declared predecessor still has this checked as soon as the specification
is parsed, the same fail-fast behaviour `extends: kernel`/`module` get;
a group `after:` another is trusted to have it by the time it actually
builds instead, and gets the same error then if it does not.

One `seine build a-multiconfig-disk.yaml` runs the whole pipeline, each
group in the order its `after:`/`before:` declares.

### Where the time went

Every build records what each of its steps cost, and `seine analyze`
reads that back once the build is over -- which is when the question is
asked. Nothing is fetched, built or written by any of it.

```
seine analyze blame
seine analyze blame demo-image.yml
```

Named specifications are loaded the way a build loads them and the
report is of the last build of exactly those. Named none, it reports on
the last build of anything, so the build that has just finished needs no
naming:

```
plan a1b2c3d4e5f6, built 2026-02-02 05:36, 4 steps at a time

    25m00s  package:linux/arm64
     6m40s  rootfs
     3m20s  tarball
       ...
  42m40s of step time in 25m00s of build
  the machine was 46% busy, load 4.4 of 8 cpus
```

After a group of images built together (see "Building several images
together"), naming one group's own files reports that group's own slice
alone -- its own steps and whichever shared ones they stood on, not its
siblings' -- so naming `pc-image`'s files alone still answers for it even
though it built alongside `rpi4-image`.

Those last two lines are the pair worth reading together. Step time is
the work the build did and build time is how long it took to do it, so
the difference between them is what `--jobs` bought. And the machine's
own numbers say whether raising it would buy any more: a build burning a
third of the cores has room, while one whose load sits well above what it
is burning is waiting for a disk or a mirror and will not go faster for
any number of steps at once. They are the whole machine's numbers, not
seine's -- anything else running lands in them too.

Which step to make quicker is a different question again, and
`critical-chain` answers it: the longest way through the build, weighted
by what each step cost.

```
seine analyze critical-chain demo-image.yml
```

```
38m20s of the 45m00s the build took is on this path

image +2m20s
  └─disk +1m00s
    └─tarball +3m20s
      └─rootfs +6m40s
        └─package:linux/arm64 +25m00s
```

A root file-system that took twenty minutes and finished while the
kernel was still going is not on that path, and making it quicker would
buy nothing. At `--jobs 1` the chain is the whole build, and the report
says so instead.

`plot` draws the same build as a chart -- a bar per step on one clock,
with the load and the cores being burnt underneath them -- as SVG on
stdout:

```
seine analyze plot demo-image.yml > build.svg
```

A build that failed is recorded too, since it is the one worth reading.
When it is fixed and run again, the second run does what the first did
not -- the packages the first built are built no more -- so seine joins
the two: every step keeps what it cost the last time it ran, the runs
are laid end to end, and the hours between the failure and the fix are
in no number. The report says how many runs it joined.

The records live with the caches, under `analyze`, and `seine cache`
shows and clears them with the rest. They are advisory: a record that
could not be written costs a report, never a build.

### Cross-compiling

When the target `architecture` differs from the host's, packages are
cross-compiled by default: sbuild builds them in a chroot of the host's
architecture, having pulled in `crossbuild-essential` for the target.

A cross build is also given dpkg's `cross` build profile, alongside
whatever `profiles` the specification named. Packaging that builds
differently when cross-built selects on that profile, and the kernel is
the clearest case: Debian's build-depends on `gcc-<version>` under
`<!cross>` and on `gcc-<version>-<triplet>` under `<cross>`, so without
it a cross build asks a host-architecture chroot for a target-architecture
compiler and stops before it starts.

Not every package can be cross-built. Setting `cross: false` builds it in a
chroot of the target's own architecture instead, running its binaries under
`qemu-user-static` -- much slower, but it works for anything. This needs the
host's binfmt registration to use the `F` (fix-binary) flag, which is what
Debian's `qemu-user-static` package sets up.

### Reproducibility

Builds are given a `SOURCE_DATE_EPOCH`, taken from the package's changelog
unless `source_date_epoch` says otherwise, and sbuild builds under a fixed
path. Patches committed to a git tree are committed at that same date under
a fixed identity, so their commit hashes -- and anything embedding them --
do not change from one rebuild to the next.

What is left is the archive the source and the build dependencies come
from, which moves under an unpinned rebuild the same way it moves under an
unpinned image. Pin the versions in the `apt://` sources, or pin the whole
archive by building from a snapshot (see
[Building from a snapshot](specification.md#building-from-a-snapshot)).

### A note on privileges

Packages are built by `sbuild` in its `unshare` mode, inside a container
that is given `CAP_SYS_ADMIN` and an unmasked `/proc`, which the nested
user namespaces it creates need. This container is more privileged than
the others seine builds and is used only to build packages. It remains
unprivileged as far as the kernel is concerned: uid 0 inside it is the
unprivileged user running seine, so what it can reach is that user's own
files rather than the machine's. No `sudo` is used or required, as
elsewhere in seine.

### What a build keeps, and getting the space back

A build keeps what it would otherwise make again: the packages it fetched,
the packages the container image builds fetched, the packages it rebuilt,
the buildd chroots sbuild unpacks, the container images it builds, and the
scratch space sources and images are assembled in -- and, beside them, what
each of its steps cost. `seine cache info` says how much each of them is
holding:

```
$ seine cache info
downloads   253.7 MiB  /home/user/project/build/downloads
packages      1.1 GiB  /home/user/project/build/cache/packages
chroots     278.4 MiB  /home/user/project/build/cache/chroots
bootstraps  485.3 MiB  /home/user/project/build/cache/bootstraps
vendor          0.0 B  /home/user/project/build/cache/vendor
images        9.3 GiB  /home/user/project/build/containers
analyze      24.0 KiB  /home/user/project/build/cache/analyze
scratch     365.5 KiB  /home/user/project/build/tmp
total        11.4 GiB
```

`seine cache clear` removes them, and naming one clears only that:

```
seine cache clear             # all of them
seine cache clear chroots     # only the buildd chroots
```

`--older-than` clears one object at a time instead of a whole cache: what
was last wanted longer ago than the span given, in days, hours or weeks.

```
seine cache clear --older-than 30d          # anything untouched for a month
seine cache clear --older-than 2w chroots   # only the buildd chroots
```

It asks the record and nothing else, so anything seine kept no record of --
a cache from before it kept one -- is left alone rather than removed on a
guess about file timestamps. Removing a rebuilt package takes the `.deb`s its
stamp named and the index describing them, so the next build writes one that
matches what is there.

None of it is needed for a build to succeed, so clearing any of it costs
time on the next build and nothing else. Do it between builds: a build
running while a cache is removed under it may fail rather than refetch.

`--entries` lists what the caches hold one object at a time, least recently
used first, which is the order to read it in when the question is what to
remove:

```
$ seine cache info --entries chroots packages
chroots     278.4 MiB  /home/user/project/build/cache/chroots
packages      1.1 GiB  /home/user/project/build/cache/packages
total         1.4 GiB

cache      entry                              last used    made
chroot     bookworm-amd64                     12d ago      12d ago
package    bookworm/amd64/linux               12d ago      12d ago
chroot     trixie-arm64                       2h ago       9d ago
package    trixie/arm64/linux                 2h ago       3d ago
```

An entry appears the first time a build makes or takes the thing it names,
so a cache nothing has been built against lists nothing. Removing the record
costs a report and never a build: what decides whether a package needs
rebuilding is still its stamp, and whether an image is current is still its
label.

`--entries-matching PATTERN` (a regex, and implies `--entries`) narrows the
listing to entries whose key matches, and, for a package entry, also prints
the specification content that stamp was actually built from -- what tells
whether a cached kernel really has the config option someone asked for,
without re-reading the specification and hoping it still says what it did
when the stamp was written:

```
$ seine cache info --entries-matching linux packages
packages      1.1 GiB  /home/user/project/build/cache/packages
total         1.1 GiB

cache      entry                              last used    made
package    bookworm/amd64/linux               12d ago      12d ago  (linux_amd64_b41c1f8278e07eb5)
    source: apt://linux
    revision: mod1
    extends:
      kernel:
        flavour: amd64
        configs:
          magic-sysrq:
          - CONFIG_MAGIC_SYSRQ=n
```

A path in it -- a patch, a kernel fragment -- is relative to whichever
specification file declared it, not the absolute path a build actually
reads: the excerpt is meant to travel with `cache export`/`import`, where
an absolute path would only ever be true on the machine that wrote it.

A build ends by saying what the caches spared it:

```
$ seine build spec.yml
...
reused: 1 downloads, 1 image, 1 package; made: 1 chroot
```

which is the short answer to whether caching is working. `--verbose` makes a
build say each of those decisions as it makes them,
which is where caching that has quietly stopped working becomes visible:

```
$ seine build --verbose spec.yml
cache: image bootstrap/debian/bookworm/all reused, made 9d ago
cache: chroot bookworm-amd64 reused, made 9d ago
cache: package bookworm/amd64/linux made
```

`images` is the odd one: the bootstraps, the builder and the imager
appliance live in podman storage of seine's own rather than in a directory,
so its size is what podman reports for that storage and clearing it removes
the images by name. Do not `rm -rf` that directory -- what is in it belongs
to uids a rootless user cannot unlink without a user namespace, so the
removal fails halfway and leaves a storage that is neither there nor
usable. `seine cache clear images` is how it is emptied.

### Moving a cache to another machine

`seine cache export` writes the caches to a tar and `seine cache import`
reads one back, so a machine that has never built anything -- a fresh CI
runner, a colleague joining the project -- can start with the caches of one
that has:

```
seine cache export caches.tar          # everything worth carrying
seine cache export chroots.tar chroots # only the buildd chroots
seine cache import caches.tar
```

An import spares work rather than network. The receiving machine reaches the
mirror or the snapshot whatever it was sent: `apt` reads its package lists
from there, and seine caches none of them. So the `downloads` cache is left
out of an export unless it is named -- what carrying it saves is a re-fetch
of the `.deb`s the playbooks install, for a machine that has to talk to the
archive anyway.

```
seine cache export caches.tar downloads   # carry them anyway
seine cache export caches.tar all         # every cache a tar can hold
```

### Carrying only what a project needs

A cache holds what a machine has built for every board and release it was
ever asked for. `--spec` scopes an export to what a build of those
specifications would want -- the chroot it unpacks, the `.deb`s its own
packages produced, the images it runs in -- and leaves the rest where it is:

```
seine cache export --spec common/amd64.yaml,pc-image/main.yaml caches.tar
```

One specification per `--spec`, its files separated by commas as `seine
build` would take them, and the flag may be given more than once for a
project built for several boards.

Nothing is fetched or built to work this out: the release and architecture
name the directories, a package's stamp names the `.deb`s its build
produced, and the image classes name themselves. The record of what was
cached is scoped with it, since a record of what was not sent describes
nothing the other machine has.

Two limits worth knowing. The `downloads` cache can only be scoped per
release, because which `.deb` apt took out of it was decided by apt inside
the container. And base images pulled from a registry are carried whatever
was asked for: nothing here built them, no specification names them, and
everything else is built on them.

`scratch` is left out too: what is in it belongs to a build that is either
running or has died. The tar is uncompressed unless its name ends in `.gz`
or `.tgz`, since `.deb` and the chroot tarballs are compressed already.

Container images go with them, which is what spares the importing machine
every container a build runs: the bootstrap tooling, the builder holding the
buildd chroot, the kernel libguestfs boots, the appliance it runs for a cross
build, the transport bootstrap ansible connects through, and the base images
those are made from. podman writes that part itself -- an archive it wrote is
what another podman will read back -- and seine gzips it on the way in, since
it is the one thing in the tar not compressed already.

One image is left behind: the image's own root file-system, the target
bootstrap as `mmdebstrap` made it. It is what the archive held on the day it
ran, so it is stale as soon as the archive moves, and a machine that imports
a stale one spends minutes making its own regardless.

The images built on that one still travel, which is worth knowing because the
appliance is the largest and slowest thing in a storage -- cross-built under
emulation. What decides whether one of them is current is what its base was
built *from*, not which bytes that base came out as: two machines
bootstrapping the same root file-system from the same specification produce
different images and the same inputs, so what stands on it is current on
either. It is the rule a package's stamp and a chroot's digest already
follow.

`--with-image-rootfs` carries the root file-system too, for a machine that
wants a copy of another's storage rather than one it can build with:

```
seine cache export --with-image-rootfs caches.tar
```

The root file-system ansible augments is not a candidate either way: it is a
container, exported straight to a tarball and never committed, so podman has
no image of it.

An image is rebuilt when the label recording what it was built from stops
matching, and both the labels and the image ids survive the round trip. So
an imported bootstrap is *current*, and so is everything standing on it,
whose label carries that image's id. A machine that imports a full tar
bootstraps nothing, builds no appliance and rebuilds no package -- it
assembles the root file-system and writes the image.

The `packages` cache is what makes this worth doing for a kernel. It holds
the `.deb`s a rebuild produced *and* the stamps that say what they were
built from, so the machine that imports it does not rebuild them:

```
$ seine cache import caches.tar
$ seine build --dry-run pc-image.yml
already built, and not built again:
  linux                          linux_1d68f0bc3eba4bb0
```

A stamp is the digest of the specification, the patches and the feeds --
nothing about the machine that built it -- so it means the same thing on
both. Change any of them, or pass `--rebuild`, and the package is built
again as usual.

What is left out is what the other machine has no use for: `sbuild`'s build
logs, which describe a build that happened elsewhere and which for a kernel
are the larger part of the cache; `apt-ftparchive`'s hash cache, which a
build rewrites from the `.deb`s it finds; the lock files; and apt's
`partial` directory of half-finished downloads. The `.changes` and
`.buildinfo` are kept -- small, and what says how the `.deb`s beside them
were made.

`-` stands for stdout or stdin, so the two ends can be piped into each
other and nothing is written to disk twice:

```
seine cache export - | ssh builder seine cache import -
```

A machine whose job is to fill that tar for everyone else has no use for
the image at the end of the build. `--packages-only` builds the packages of
the `packages` section and stops, without assembling a root file-system or
writing an image:

```
seine build --packages-only pc-image.yml
seine cache export caches.tar
```

`--rootfs-only` stops a step later, at the tarball: a root file-system to
look inside rather than a disk to boot, without the appliance libguestfs
would otherwise prepare to write one.

An import extends what is already here rather than replacing it, and a
second import of the same tar changes nothing. Where the two machines built
the same source package differently, the build that arrives wins and the one
that was here is removed with the stamp that named it -- a flat repository
offering two versions of one package is one apt installs the higher of,
which is neither what the specification pinned nor what either build
describes.

```
seine cache import --replace caches.tar   # this machine, as that tar
seine cache import --force caches.tar     # tidy up .debs no stamp names
```

`--replace` empties the caches named before reading the tar, which is what a
runner starting from nothing wants. Neither form removes a `.deb` that no
stamp names -- that is a leftover rather than something superseded, and
removing it would be guesswork -- unless `--force` says to.

The repository index is not carried, and an import removes the one that is
there: it describes what the directory held a moment ago, and a build writes
one from what the directory really holds. The record of what was cached
travels as well, carrying when each thing was made but nothing about the
other machine's use of it -- on the machine receiving it, none of it has
been used yet.

A tar seine did not write is not trusted: every member has to be a file, a
directory or a link under a cache seine knows, and has to stay inside it --
both where it is written and, for a link, what it points at. A member that
does not fails the import rather than being quietly skipped.

### Putting seine's directories on another drive

A home directory is commonly the smallest filesystem on a build machine,
and a couple of kernels fill it. `SEINE_BUILD_DIR` moves everything seine
creates -- container storage, caches, downloads, logs, scratch space -- to
another drive at once; a variable of its own moves any one of them
elsewhere instead. See [Environment variables](environment.md) for the
full layout and every variable that touches it.

### Gists

A fragment written for one image is often worth reusing in another --
a gist is a plain spec fragment kept outside any one project's own
source tree, so it survives moving between them. It lives under
`XDG_DATA_HOME` (`SEINE_GISTS_DIR` overrides that outright), not under
`SEINE_BUILD_DIR`: unlike a cache, nothing here is safe to delete just
to get space back.

```
$ seine gist ls
a-kernel   page-ref debugging
```

`seine gist show NAME` prints the fragment as-is (its own first line is
the description above), and `seine gist rm NAME` deletes it. Creating
one by hand is no different from any other spec fragment -- drop a
`.yaml` file into the directory `seine gist ls` names when there is
nothing in it yet, and it is a gist. The AI chat's own `gist-create`
tool is the other way in -- see [ai.md](ai.md).

## Software Bill of Materials

`--sbom` makes a build write a Software Bill of Materials next to the image
it produced:

```
./seine.py build --sbom examples/pc-image/main.yaml
```

leaves `pc-image-sbom.spdx.json` beside `pc-image.img`, in SPDX format.

The list is taken from dpkg's own record of what was installed rather than
from a scan of the files in the image, so it says what the package manager
says. It is produced by [debsbom](https://github.com/siemens/debsbom), which
reports the source package every binary package was built from as well as
the binary packages themselves -- which is what a Debian CVE is filed
against, and what makes the SBOM answerable to a security advisory.

debsbom runs from the container image its authors publish, pulled on first
use. Only the few files it reads are taken out of the root file-system, so
nothing else about the image is exposed to it.

## Vulnerability scanning

`seine issues` scans an SBOM (above) for known CVEs, against Debian's own
security tracker:

```
$ seine issues --sbom=pc-image-sbom.spdx.json
CVE-2018-12928   linux     low                open
CVE-2016-2781    coreutils low                resolved
```

or, given a specification instead, scans the SBOM a previous
`seine build --sbom` of it left behind:

```
seine issues examples/pc-image/main.yaml
```

`--filter=PKG` narrows to packages matching PKG (a regex); `--min-urgency=`
drops anything less severe than the level given (`high`, `medium`, `low`,
`unimportant`, `end-of-life`, or `not-yet-assigned`, the default -- everything).
`--rescan` ignores a cached scan and runs a fresh one; without it, a scan
against the same SBOM is read back from a cache beside it rather than
rerun.

The urgency shown is Debian's own triage label, not a CVSS score --
debsbom's own scan carries no numeric severity at all.

By default this also runs from debsbom's own container image (its
`sec-scan` subcommand). Setting `sbom2cve_program` (`seine tui`'s `/set`,
or by hand in `settings.json`) to the path of an external program
replaces it outright: seine runs `PROGRAM SBOM_PATH` and reads the same
JSON-lines shape back from its stdout that `debsbom sec-scan -f json`
would have written.
