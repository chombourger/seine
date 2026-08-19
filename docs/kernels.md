## Kernels

Debian's kernel is built to boot anything, which makes it large for an
appliance that knows what it runs on -- and the kernel it is built from
is the one Debian happens to package. Both are things a specification
can change: the first by reconfiguring the distribution's own kernel,
the second by building a tree Debian does not package at all, under the
distribution's packaging.

### Rebuilding the kernel

Settings that only mean something for one kind of package go under
`extends`, named after that kind:

```
packages:
    - source: apt://linux
      extends:
          kernel:
              fragments:
                  - configs/slim-common.fragment
                  - configs/slim-amd64.fragment
      profiles:
          - pkg.linux.nokerneldbginfo
          - pkg.linux.notools
```

`upstream-sha256` is what the tarball is checked against, exactly as
`sha256` is for a package's own source:

```
          kernel:
              upstream: https://cdn.kernel.org/…/linux-6.18.43.tar.xz
              upstream-sha256: 9a1c…
```

`fragments` lists kconfig fragments, given relative to the YAML file listing
them, in the ordinary kernel syntax:

```
# CONFIG_MEDIA_SUPPORT is not set
# CONFIG_SOUND is not set
```

The fragments are appended to Debian's own configuration stack for the
target architecture, where they have the last word, and the kernel's
`oldconfig` then turns off whatever the disabled options were holding up
-- so a fragment says what it means instead of listing every symbol
underneath it. This needs no patch: a kernel's configuration lives in
`debian/`, which the source format lets seine edit directly, so a fragment
does not go stale the way a patch against a configuration file would.

`configs` is the same idea, without a fragment file: a dictionary of group
names to a list of `CONFIG_OPTION=value` lines, written directly in the
specification, for the common case of wanting a handful of symbols set
without a `configs/*.fragment` file to hold them:

```
          kernel:
              configs:
                  rtc-and-lpss:
                      - CONFIG_RTC_DRV_CMOS=m
                      - CONFIG_RTC_DRV_RX6110=m
                      - CONFIG_I2C_DESIGNWARE_PCI=m
                      - CONFIG_MFD_INTEL_LPSS_PCI=m
                      - CONFIG_MFD_INTEL_LPSS=m
                      - CONFIG_MFD_INTEL_LPSS_ACPI=m
```

Each group is appended to the same configuration stack as `fragments`'
own files, after them, under a `# <group name>, added by seine` header of
its own -- the group's name is what names the fragment it becomes. Two
groups touching the same symbol settle it the way two fragment files
would: the one named last wins.

A line is either an assignment or kconfig's own way of writing a disabled
symbol -- `CONFIG_OPTION=value` or `# CONFIG_OPTION is not set` -- checked
at parse time rather than left for `oldconfig` to catch. `=n` is accepted
as a value like any other and rewritten to the comment form when the
fragment is written, since kconfig itself does not understand `=n` as an
assignment; a fragment excerpt already written the other way can be
pasted into a group unchanged.

`flavour` cuts the build down to one kernel. Debian builds every kernel an
architecture has -- on amd64 that is a cloud flavour and a realtime kernel
besides the ordinary one -- and each is a full build.

A flavour name only means something within a `featureset`, which defaults
to `none` and is where nearly every kernel wanted lives: amd64's realtime
kernel and its ordinary one are *both* the `amd64` flavour, of the `rt`
and `none` featuresets. Ask for a realtime kernel with:

```
          kernel:
              featureset: rt
              flavour: amd64
```

Neither has to be written where the kernel is: which flavour an
architecture means is a property of the architecture, and
`examples/common/amd64.yaml` says so under [`defaults`](specification.md#defaults). A
file that wants another one -- a realtime kernel, a cloud flavour --
says so the same way, in the file that wants it, and the kernel entry
itself stays architecture-agnostic.

One thing that does not follow along: the metapackage a playbook
installs. Debian names it `linux-image-[<featureset>-]<flavour>`, so an
`rt` kernel is installed by `linux-image-rt-amd64`, and a playbook still
asking for `linux-image-amd64` gets the distribution's ordinary kernel
rather than the rebuild -- which is a working image containing the wrong
kernel, the failure that does not announce itself. A specification that
changes flavour or featureset changes that name too.

Naming a featureset or flavour the architecture does not have is an error
listing the ones it does.

One `linux` source package builds three kinds of kernel this way, all
from the same one-sbuild-run-per-architecture rebuild. Debian's own
flavour covers every machine of that architecture, at the cost of
carrying every machine's drivers. A flavour derived from it with nothing
board-specific yet -- `slim-arm64`, in the examples below -- narrows
that once, to "any appliance of this architecture", and is what every
board sharing that architecture derives from rather than each repeating
the same trim. A flavour derived from *that* -- `rpi4` -- narrows it
again, to one board, built in what only that board needs and stripped
of what only some other board would have wanted. Three flavours, three
things known about the machine running each, and still the one rebuild.

`derived-flavours` gives a Debian flavour a name of its own, with
configuration of its own, rather than building it as Debian's own name
and packaging:

```
          kernel:
              derived-flavours:
                  arm64:
                      rpi4:
                          - configs/rpi4.fragment
```

`linux-image-arm64` comes out `linux-image-rpi4` instead, with the
headers and `-dbg` packages beside it following -- everything but the
ABI, which is still read off the kernel version. The key is the Debian
flavour being derived from, not the architecture: they usually read the
same (`amd64`, `arm64`), but not always -- `armhf` builds `armmp` -- and
which architecture is being built is already answered by the
specification's own `architecture`, so naming it again here would only
be able to disagree. `flavour` names a bare rebuild of one of Debian's
own flavours, unrenamed; `derived-flavours` is a replacement for it
rather than something written beside it, and takes precedence if both
somehow end up set -- an architecture file's `defaults` commonly gives
every kernel a plain `flavour`, for a module built against it, which a
package also deriving inherits whether it means to use it or not.

A single name under one base, as above, is a plain rename. Several are
several flavours out of the one source package this builds, sharing one
sbuild run rather than costing one apiece:

```
          kernel:
              derived-flavours:
                  arm64:
                      rpi4:
                          - configs/rpi4.fragment
                      rpi5:
                          - configs/rpi5.fragment
```

Each name's fragment list is appended to `config`'s on top of the base
flavour's own -- Debian's own mechanism for telling `cloud-amd64` apart
from `amd64`, not something seine adds beside it, so a name already in
use by the architecture (`cloud-arm64`, say) works the same way a fresh
one does. A name's list may be left empty or out entirely: a derived
flavour with nothing beyond the common `config` is still a flavour of
its own -- which is all a plain rename is.

A base does not have to be one of Debian's own flavours -- it may be
another name derived here, one flavour built on one already derived:

```
          kernel:
              derived-flavours:
                  arm64:
                      slim-arm64:
                          - configs/slim-common.fragment
                          - configs/slim-arm64.fragment
                  slim-arm64:
                      rpi4:
                          - configs/rpi4.fragment
```

`rpi4` comes out with both fragment lists, `slim-arm64`'s and its own,
in that order -- and `slim-arm64` is still built too, under its own
name: naming it as another flavour's base does not take it away, only
`arm64` -- what it was itself derived from -- loses to it. Names may be
given across several files this way, each adding to what an earlier one
started rather than replacing it -- one file deriving the generic
flavour, a board file naming it as the base for one of its own, which is
what `examples/slim-flavours.yml`, `examples/pc-kernel.yml` and
`examples/rpi4-kernel.yml` do.

The whole dictionary may name bases from more than one architecture at
once -- `examples/slim-flavours.yml` derives `slim-amd64` from `amd64`
and `slim-arm64` from `arm64` in the one entry, since a shared fragment
covering every architecture is more useful than one file per
architecture repeating the same shape. Only the entries for the
architecture actually being built are ever reached; the rest are
silently not this build's problem, the same as a base naming nothing at
all or a cycle of two names deriving from each other -- none of them is
an error on their own, since seine cannot tell a genuine mistake from a
board file composed without the one that derives the base it names.
What catches a real mistake is that nothing gets built under the name
asked for: a working image containing the wrong kernel is exactly the
failure that must not pass quietly, so debian/control is still checked
against what was actually derived for this architecture.

It needs the newer `defines.toml` an architecture carries, not the ini
format: the ini flavour list is names only, with nowhere to point a
fragment of its own.

Its default revision follows the reasoning [above](specification.md#local-versions) for
an ordinary rebuild, widened the same way as the check that everything
asked for came out: every name actually derived for this build, joined
with `.` (not `-`, which a Debian revision cannot hold), so `rpi4` and
`rpi5` together default to `rpi4.rpi5` rather than `mod1`. A name that
cannot be a Debian revision on its own -- `cloud-edge`, say, or the
joined result of several -- needs `revision` written down instead.

Build profiles matter more here than elsewhere. `noudeb` is effectively
required once a fragment removes drivers: the debian-installer udebs are
checked against the list of modules the stock configuration produces, and
that check fails at the very end, once the kernel has already been built.
The `pkg.linux.*` ones are savings rather than necessities:
`nokerneldbginfo` drops a debug package several times the size of the
kernel itself, and `notools` and `nosource` drop packages an image does
not install. Documentation is dropped by the standard `nodoc`.

That distinction is worth care, because a profile a package does not
define is accepted and ignored rather than reported -- a misspelt one
costs a full kernel build to notice, and only if you were watching what
came out of it.

Two things to keep in mind when writing a fragment. The rebuilt kernel is
pinned above the distribution's, so seine's own imager boots it as well --
it needs virtio and 9p to reach its disks and the host's files, and an
image whose kernel cannot mount its root file-system will fail at the end
of a long build rather than the start. `examples/configs/slim-common.fragment`
and the architecture's own beside it document what they leave enabled,
and why, for this reason.

A kernel is also where the rebuild cache earns its keep: the fragments are
part of what decides whether a package needs rebuilding, by content, so
editing one is enough to ask for a new kernel -- and not editing one means
the hours are paid once.

### Bring your own kernel

`upstream` builds a tree the distribution does not package -- a stable
release from kernel.org, a release candidate, or a vendor's BSP -- under
the distribution's own packaging:

```
packages:
    - source: apt://linux
      extends:
          kernel:
              upstream: https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.43.tar.xz
              flavour: amd64
```

`source` says whose `debian/` to build with, `upstream` says what to build.
Two forms are understood, the same two a package `source` takes:

 * a release tarball, `https://.../linux-<version>.tar.{xz,gz,bz2}`
 * `git://<host>/<path>;rev=<commit>`, in the notation described above,
   `rev` required for the same reason

Both are unpacked, Debian's `debian/` directory is moved onto the result,
and the orig tarball is regenerated from it -- so what comes out is an
ordinary source package that sbuild builds in the ordinary way. The
packages it produces carry Debian's names: `linux-image-<abi>-<flavour>`,
the headers packages and their `-common` half, `linux-libc-dev`, the
metapackages, and the maintainer scripts that drive the initramfs and the
boot loader. They replace the distribution's kernel rather than sitting
beside it, which is the point of doing this the hard way.

Naming a version is only needed when the packaging wanted is not the one
the suite would hand you -- which is the case for bookworm below, and not
the case when the release packages a kernel recent enough to build with.
An unpinned `apt://linux` follows the suite's point releases; the patches
that are kept have been in every one of them, and what changes between
them is dropped whatever it is called. Pin it, or build from a snapshot,
when the build has to be reproducible rather than merely repeatable.

The packaging has to be one the tree can be built by, which is a weaker
requirement than it sounds -- Debian's kernel packaging changes slowly --
but not an empty one. Two things to weigh: the further apart the two
versions are, the more of the series has to be dropped (see below), and
the packaging's build dependencies have to be satisfiable in the chroot,
which is the suite being built for. That is what `examples/linux-6.18/`
shows: trixie takes its packaging from the release, bookworm takes the
*same* packaging from `bookworm-backports`, because a backport
build-depends on what the suite it was backported to has. Taking
trixie's copy into a bookworm chroot would ask it for a newer compiler
than bookworm carries.

Which is also why that directory is two files rather than one per suite.
`kernel.yml` holds everything that is a property of the packaging and the
tree -- the upstream, the flavour, the patches to drop -- and is what
trixie uses directly. `bookworm.yml` `requires` it and adds one line, the
version that reaches into backports. Packages are merged across files by
the source package they name, the way partitions are merged by label, so
a file may say *what* to build without restating *how*, and the suite
keeps only what is genuinely its own:

```
# examples/linux-6.18/bookworm.yml, in full
requires:
    - kernel

packages:
    - source: apt://linux=6.12.95-1~bpo12+1
```

A setting already given wins, and `requires` loads what it names after
the file that asked for it -- so the specification reaching for a
fragment is the one that gets to override it.

#### What is kept of Debian's patches

Debian's series is not applied whole. What is taken automatically is the
*packaging*: the patches without which the build does not work. What is
left behind is Debian's kernel *policy* -- unprivileged user namespaces
off, yama off, autoloading of half a dozen protocols disabled, a warning
on mounting raid5 -- along with everything under `bugfix/`, which is a
backport a newer tree already has, and everything under `features/`,
which is keyed to config symbols that `oldconfig` drops along with the
patch.

The series says nothing about which is which, so it is decided by what
each patch touches: a patch under `debian/` that only touches build files
-- `Makefile`, `Kbuild`, `.gitignore`, `scripts/` -- is packaging, and a
patch reaching into C source is a change to the kernel. By content rather
than by name, because the set differs between releases, so a list of
names would be locked to one baseline.

Which files count is data, not code: `seine/data/kernel.yml` holds the
patterns, and is the one place to edit when the packaging moves under us.
Its content is part of what decides whether a grafted kernel needs
rebuilding, so editing it asks for one, the way editing a kconfig
fragment does -- and only for grafted kernels, since an ordinary rebuild
never consults it.

A specification whose packaging builds through files those patterns do
not name says so itself, without editing anything seine ships:

```
          kernel:
              upstream: git://git.example.com/bsp/linux.git;rev=1e4a0c9
              build-files:
                  - '(^|/)Kconfig[^/]*$'
```

They are Python regular expressions, matched against every path a patch
changes, and they are **added** to the shipped ones rather than replacing
them: what makes a kernel build is the same wherever the tree came from,
and a packaging that reaches somewhere else reaches there *as well*. A
pattern that is not a valid expression is reported by the specification
that wrote it rather than by the first patch it fails on, and the list
counts in the digest, so extending it asks for the rebuild it deserves.

`drop-patches` is the same arrangement seen from the other side: the
`drop-patches` in `seine/data/kernel.yml` is a floor that a specification
adds to, never something it has to restate.

That line is drawn where it is for two reasons, and the second is the
practical one. A specification that asked for another tree did not ask
for another distribution's opinions about it -- but more to the point, a
patch to C source is a patch that has to *apply*, and the whole premise
here is a tree far enough from Debian's that it packages a different
kernel. Build files are the part of the kernel that barely moves, which
is why taking them automatically works at all; `net/`, `security/` and
`fs/` are not, and a policy patch written against 6.12 more often than
not fails against 6.18. Automatic selection would turn every one of them
into a rebase seine cannot do for you.

**So preserving Debian's semantics is the developer's job, not the
tool's.** If the goal is a kernel that behaves like Debian's rather than
merely one packaged like it, take the policy patches from the source
package the packaging came from, rebase them onto your tree, and list
them under `patches` (see below). Which ones matter is a decision about
the product -- an appliance that runs no untrusted code has different
answers than a general-purpose image -- and it is a decision seine
deliberately leaves to whoever is making it. `debian/patches/series` in
the source package, minus the patches listed below, is where that work
starts.

Of the patches that selection keeps from the packaging the examples use,
these apply to a 6.18 tree and are what it is built with:

| Patch                                            | What it does                                    |
| ------------------------------------------------ | ----------------------------------------------- |
| `kernelvariables.patch`                          | carries `ARCH`, `CROSS_COMPILE` and `KERNELRELEASE` into the kernel's makefiles -- the one that matters most |
| `uname-version-timestamp.patch`                  | builds `uname -v` from `SOURCE_DATE_EPOCH`      |
| `makefile-make-compiler-version-comparison-optional.patch` | lets a build use a compiler Debian did not test |
| `perf-traceevent-support-asciidoctor-for-documentatio.patch` | builds perf's documentation with asciidoctor |
| `tools-perf-install-python-bindings.patch`       | installs perf's python bindings where the packaging looks for them |
| `tools-perf-perf-read-vdso-in-libexec.patch`     | moves `perf-read-vdso` out of `/usr/bin`        |
| `arch-sh4-fix-uimage-build.patch`                | sh4 only                                        |
| `mips-boston-disable-its.patch`                  | mips only                                       |

The rest are dropped by the example files, upstream having since taken
what they fixed: `gitignore.patch`,
`documentation-drop-sphinx-version-check.patch`,
`kbuild-look-for-module.lds-under-arch-directory-too.patch`,
`kbuild-abort-build-if-subdirs-used.patch` and
`fixdep-allow-overriding-hostcc-and-hostld.patch`.

`drop-patches` is a list of globs subtracted from what was selected, and
finding what belongs on it costs one build attempt rather than one per
patch: seine applies the series itself before `dpkg-source` does, and
reports *every* patch that fails at once, with the files each one wanted
-- which is what says whether the tree has the change already. A glob
matching nothing is an error rather than a no-op, so a list does not
quietly go stale when the packaging is moved forward.

#### Bringing your own patches

`keep-patches` takes the decision over. It is a list of globs matched
against the series, and naming it replaces the content-derived selection
rather than adding to it:

```
          kernel:
              upstream: git://git.example.com/bsp/linux.git;rev=1e4a0c9
              flavour: arm64
              keep-patches:
                  - debian/kernelvariables.patch
                  - debian/uname-version-timestamp.patch
```

Setting it to nothing at all drops Debian's series entirely, which is
what to do when the packaging patches have been ported into the tree
already, or when you would rather carry your own:

```
          kernel:
              upstream: git://git.example.com/bsp/linux.git;rev=1e4a0c9
              flavour: arm64
              keep-patches: []
      patches:
          - patches/0001-kernelvariables-ported.patch
```

`patches` is the ordinary package attribute, applied after the graft, so
what it adds ends up in the series of the source package that is built.
It is where two rather different jobs are done. One is the escape hatch
above: a tree far enough from Debian's that the automatic selection has
nothing useful to say about it. The other is putting Debian's policy back
-- `yama-disable-by-default.patch`,
`add-sysctl-to-disallow-unprivileged-CLONE_NEWUSER-by-default.patch`,
the `*-disable-auto-loading-as-mitigation-*` set, the lockdown series --
rebased onto your tree, one at a time, keeping what the image actually
wants. seine takes them as it takes any other patch; what it will not do
is rebase them for you, or pretend a patch that no longer applies did.

Note that `kernelvariables.patch` in some form is not optional whichever
route is taken: without it the build does not pass `ARCH` down, and
cross-building is the case that fails first.

Two things are dropped whatever these say. `debian/dfsg/*` disables code
Debian removed from its own orig tarball, which an upstream tree still
has, so those patches are neither needed nor applicable -- that list is
`drop-patches` in `seine/data/kernel.yml`, alongside the patterns above,
and is a consequence of the tree being built from rather than a default a
specification overrides. And signed code is turned off for a grafted
kernel, since the Secure Boot signature is issued by a key nobody outside
Debian holds.

#### What you do not get

Worth being plain about, since the aim here is a true replacement:

 * **No Secure Boot lockdown.** The `features/all/lockdown/*` patches
   change C source, so they are not among the packaging, and
   `CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT` goes with them. The signature
   was never obtainable, but the enforcement is a real difference. An
   image that needs it has to carry those patches rebased onto its tree,
   through `patches`.
 * **No Debian kernel policy.** Nothing is silently changed -- the
   kernel is simply upstream's, with upstream's defaults, and what
   Debian would have changed is listed above. Rebase those patches and
   list them under `patches` if the image wants them.
 * **No featureset that Debian carries patches for.** Featuresets other
   than the one being built are disabled, and `features/*` is not among
   the patches kept, so asking a graft for `rt` gets the featureset's
   *configuration* and none of Debian's realtime patches. For a 6.12 or
   newer tree that may be enough -- `PREEMPT_RT` is upstream since 6.12
   -- but seine has not been used that way, and against an older tree it
   would quietly build something that is not a realtime kernel.
 * **A distinct ABI name.** The rebuild is marked `UNRELEASED`, so a
   6.18.43 tree comes out as `6.18+unreleased-amd64` rather than
   borrowing an ABI number the distribution assigns. Nothing mistakes it
   for the distribution's kernel, which is the intent. `abi-suffix`
   replaces `+unreleased` with a suffix of your own:

   ```
          kernel:
              abi-suffix: "+acme1"
   ```
 * **`linux-libc-dev` from the new tree.** It is built and it is in the
   local repository, above the distribution's, so packages built after
   the kernel and the image itself get its headers. Usually what you
   want; worth knowing when it is not.

### Bring your own modules

A driver that lives outside the kernel tree is a package like any
other, built by `extends: module:`:

```
packages:
    - source: git://github.com/NVIDIA/open-gpu-kernel-modules.git;rev=c8e6998…
      name: nvidia-open
      version: "580.178.04"
      extends:
          module:
              build: .
              modules: [nvidia, nvidia-drm, nvidia-modeset, nvidia-uvm]
              make-vars:
                  SYSSRC: $KERNEL_SRC
                  SYSOUT: $KERNEL_OBJ
                  ARCH: $KERNEL_ARCH
                  TARGET_ARCH: $KERNEL_MACHINE
              amd64-kernels:
                  - apt://linux-headers-amd64
```

Such a tree carries a makefile and, usually, no `debian/` directory, so
seine writes the packaging. That is what `name` and `version` are for:
the repository a clone came from is not what the driver is called, and a
tree with no changelog cannot say what version is being built. A
version is a string -- yaml reads an unquoted `1.10` as `1.1`.

A tree that does carry packaging has it replaced rather than refused.
What upstream packaging exists for an out-of-tree module tends to ship
the module as *source*, for dkms to build on whichever machine installs
it -- a compiler and a kernel's headers on the device, and a build at
first boot. `extends: module:` asks for the other thing, so it writes
its own packaging over the tree's.

Two examples are shipped. `examples/nvidia-open.yml` builds NVIDIA's
open GPU modules for whichever kernel a flavour is at, and
`examples/bcachefs/` builds bcachefs -- which left the kernel at 6.18
and is maintained beside it -- against the kernel the specification
builds, since the filesystem needs one newer than trixie ships.

| Setting          | Required | Description                                  |
| ---------------- |:--------:| -------------------------------------------- |
| `<arch>-kernels` | yes      | Kernels to build against, per architecture   |
| build            | no       | Directory whose makefile to run, `.` by default |
| build-depends    | no       | What the tree needs to compile                |
| make-vars        | no       | Variables to pass to make                     |
| modules          | no       | The `.ko` files that should come out          |
| runtime-depends  | no       | What the modules need once installed          |
| target           | no       | Make target, `modules` by default             |

The tree is built through its own makefile rather than by driving
kbuild at it. Packaging that has something to do first, or that works
out what kbuild needs, is skipped entirely otherwise -- and skipped
quietly, leaving a build that succeeded and a package with no modules
in it.

#### Naming the kernels

Kernels are named per architecture, by their **headers** package:

```
              amd64-kernels:
                  - apt://linux-headers-amd64
                  - apt://linux-headers-6.12.101+deb13-rt-amd64
              arm64-kernels:
                  - linux
```

Three ways to name one. A metapackage (`linux-headers-amd64`) follows
whichever kernel that flavour is at, and seine rebuilds the modules
when the answer moves -- which is what a specification wants, since an
ABI changes under it with every kernel update. An ABI written out names
one kernel exactly. A bare name is a kernel this specification builds,
and saying so is all that is needed: the module is built after it, and
a kernel that changes for any reason rebuilds the modules on it.

Naming an *image* package is refused. A module is compiled against
headers, and which headers belong to an image is a question with more
than one answer once featuresets exist.

Per architecture, rather than one flat list, so that a specification
building for amd64 never has to make sense of arm64's kernels. A module
that names no kernels for an architecture it is built for is refused
when the specification is parsed, rather than producing an image that
boots and carries none of the modules asked for.

#### What comes out

One binary package per kernel, named for the kernel the modules will
load into, and a metapackage beside it named for the flavour:

```
nvidia-open-modules-6.12.101+deb13-amd64
nvidia-open-modules-amd64
```

A playbook installs the second. The first is renamed by every kernel
update; the flavour is not.

#### What the build is told

A tree left to itself asks `uname` which machine it is building for,
which answers for the builder rather than for the package. So the
rules set these, and a `make-vars` value may name them:

| Variable          | What it is                                        |
| ----------------- | ------------------------------------------------- |
| `$KERNEL_SRC`     | The kernel's generic sources (`-common`)           |
| `$KERNEL_OBJ`     | Its configuration and `Module.symvers` (flavour)   |
| `$KERNEL_RELEASE` | `<abi>-<flavour>`                                  |
| `$KERNEL_ARCH`    | What the kernel calls the architecture             |
| `$KERNEL_MACHINE` | What `uname -m` calls it                           |

The last two are not the same thing. The kernel says `arm64` where
`uname` says `aarch64` -- and both say `x86_64`, which is how a
specification that confuses them looks correct until it is built for
something else.

`$KERNEL_SRC` and `$KERNEL_OBJ` are two paths because Debian splits a
kernel's headers into two packages. A tree that tests the kernel by
compiling against it needs the first; kbuild needs the second. Given
only one, such a tree concludes the kernel is missing headers it has,
and fails much later and somewhere else.

#### Cross-compiling modules

Modules cross-compile like everything else. It takes some arranging,
which seine does without being asked: a kernel's headers reach for
`linux-kbuild`, whose `fixdep` and `modpost` are compiled for the
kernel's architecture and cannot run on a machine of another -- and a
kernel seine grafted has no such package in any archive at all.

So seine builds one: that kernel's headers with the tools rebuilt for
the machine doing the building, from the kernel's own source. It is
built once per kernel however many modules are built against it, and
it lands in the local repository like anything else.

Nothing about a specification changes for it. `cross: false` still
means what it means elsewhere -- build under emulation, for packaging
that has to run what it just built.

