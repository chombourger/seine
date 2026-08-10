# seine: Slim Embedded Images Now Easy

## Introduction

seine is a command-line tool to build images for embedded systems based on the
Debian operating system. The system is specified in YAML (either as a single
file or several) and may include Ansible playbooks to install packages or
configure them.

The tool was designed to not require elevated privileges after its installation
(sudo isn't used or required, no bind mounts, etc.). The root file-system is
first assembled in a container (seine uses podman because it is daemon-less and
very similar to docker). It is then exported as a tarball and a throwaway
[libguestfs](https://libguestfs.org/) appliance (running under qemu/kvm) is
used to create the disk images including partitions and logical volumes that
were specified. Installation of the boot-loader also happens there since it
may require disks/partitions to be created.

## Getting started

### Installation

The easiest way to get started is to install the following packages, all
available from your distribution:

```
sudo apt-get install -y podman qemu-kvm crun python3-venv python3-guestfs
sudo adduser $USER kvm
```

Building for an `architecture` other than the host's (e.g. `arm64` images on
an `amd64` host) additionally needs that architecture's qemu system emulator,
e.g.:

```
sudo apt-get install -y qemu-system-arm
```

If `/tmp` is its own mount (e.g. tmpfs with `nosuid,nodev`), rootless podman's
default `runc` fails bind-mounting it with `operation not permitted`; `crun`
does not have this issue. Make it the default runtime:

```
mkdir -p ~/.config/containers
printf '[engine]\nruntime = "crun"\n' > ~/.config/containers/containers.conf
```

Python dependencies (`pyyaml`, `ansible-core`) can be installed in a virtual
environment instead of system-wide:

```
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install containers.podman
```

You may then either use seine in place (use the `seine.py` script from the top
level directory of this source tree) or generate a binary package. To build
a sample image without installing `seine` on your system, use:

```
./seine.py build examples/pc-image/main.yaml
```

To produce a binary package, use the `dpkg-buildpackage` command as follows:

```
sudo mk-build-deps -i -r
dpkg-buildpackage -b -uc
```

And install it with:

```
sudo dpkg -i ../seine_0.1-1_all.deb
```

The `seine` tool should then be usable from anywhere (since installed in
`/usr/bin/`) and used as follows:

```
seine build spec.yaml
```

### Running the tests

The tests under `tests/spec/` use
[avocado](https://avocado-framework.github.io/), which Debian does not
package -- install it in a virtual environment that can still see the
system's `python3-guestfs`, which pip cannot install:

```
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt avocado-framework 'setuptools<81'
avocado run tests/spec/*.py
```

`--system-site-packages` is what makes `guestfs` importable in there.
`setuptools<81` is for avocado itself, which still imports `pkg_resources`;
without it every test errors out before it runs.

Most of them parse a specification and check what comes out of it, needing
neither containers nor a kvm-capable machine. The ones that do build
something -- the SBOM test bootstraps a root file-system and runs debsbom on
it for real -- are tagged `container`, take minutes on a cold cache, and
cancel themselves where podman is missing. Leave them out with:

```
avocado run --filter-by-tags='-container' --filter-by-tags-include-empty tests/spec/*.py
```

`--filter-by-tags-include-empty` is needed because avocado otherwise drops
every test that carries no tag at all, which is all the others.

#### The full plan

`tests/spec/images.py` builds images for real -- `pc-image` and
`rpi4-image`, each for bookworm and for trixie, each with the 6.18 kernel
of `examples/linux-6.18/`. Four kernels and four images is a long run,
so they cancel themselves unless asked for:

```
SEINE_TEST_PLAN=full avocado run --filter-by-tags=full tests/spec/images.py
```

They build the examples as shipped rather than copies of them: the files
under `examples/common/` composed on the command line the way a
specification is composed, with the release chosen at the front and the
kernel fragment at the end. The only thing the tests add is where to
write the image, so four builds do not agree on one filename. A test that
duplicated the metadata would pass while the examples rotted.

The kernel is part of the plan on purpose. It is what exercises the graft
against two different packagings, the flavour arriving from the
architecture file, the cross toolchain an arm64 build selects, and the
component `raspi-firmware` is fetched from -- none of which a build
taking the distribution's own kernel would touch. Each test checks the
image was produced *and* that the kernel behind it was the grafted one,
since an image comes out either way.

Expect the arm64 pair to dominate the runtime: the kernel is
cross-compiled, and the imager appliance runs under emulation.

### Using an HTTP proxy

If `http_proxy` / `https_proxy` (and optionally `no_proxy`) are set in your
environment, they are used for every network access a build makes: base image
pulls, the host bootstrap's `apt-get`, `mmdebstrap` fetching the target
root file-system, and the packages your playbooks install. Nothing needs to be
set in the specification file, and the proxy is not baked into the image that
is produced.

```
export http_proxy=http://proxy.example.com:3128
export https_proxy=$http_proxy
seine build spec.yaml
```

Note that the proxy must be reachable from inside a container: a proxy on
`localhost` will not work, use the address the host is known by on the network
(or the container gateway) instead.

### Specification files

A system specification may be written in one or several YAML files comprised
of the following sections:

 * distribution
 * packages
 * playbook
 * image

The specification may be broken down into smaller files to ease maintenance and
readability. This can be done using `requires` as shown below:

```
requires:
    - bookworm
    - amd64

distribution:
    - ...

playbook:
    - ...

image:
    - ...
```

For each module listed in the `requires` section, a corresponding file with
either the `.yml` or `.yaml` suffix shall be found in the folder of the yaml
file requiring them.

#### distribution

The `distribution` section will be used to specify the primary source of the
packages that will make the end system. The following attributes are supported:

 * source: either `debian` or `ubuntu`
 * release: codename of the version to be used (e.g. `bookworm`)
 * architecture: one of `amd64`, `arm64` or `armhf`
 * uri: base location of the distribution packages
 * components: archive components every feed carries (`main` by default)
 * feeds: apt feeds to build from (see below)

##### feeds

Without `feeds`, a system is built from the `release` alone. That is
rarely what is wanted: it leaves out the updates accumulated since the
release and the security suite, so the image is built from packages known
to be superseded and cannot be brought up to date on the target either.
List them:

```
distribution:
    release: bookworm
    feeds:
        - suite: bookworm
        - suite: bookworm-updates
        - suite: bookworm-security
          uri: http://security.debian.org/debian-security
```

Each feed takes `suite`, and optionally `uri` (the distribution's `uri`
by default), `components` (the distribution's `components`, itself `main`
by default) and `sources`. They are
listed rather than assumed because which suites a release has, and where
they are served from, differs between distributions and between a release
and its development version -- and a suite that does not exist fails
every build that follows.

The same feeds are used to build the image, to make the chroot packages
are rebuilt in, and to fetch their sources. Rebuilding a package against
different feeds than the image installs from means rebuilding a different
version than the one it would have had, and for the security suite that
means rebuilding a source without the fixes apt would otherwise deliver.

A component that every feed carries is said once, on the distribution,
rather than per feed:

```
distribution:
    components: main non-free-firmware
```

That is what a file describing a board says when it needs firmware from
`non-free-firmware`: it reaches the release, the updates and the security
suite alike, so the file names no suite and works on whichever release
the specification is built for. A feed that says `components` itself
still decides for itself, which is what a vendor archive carrying one
component needs.

Asking here rather than writing a `sources.list` into the image is what
keeps the component under the same rules as everything else: the same
URIs, the same pinning, and a build from a snapshot takes those packages
from the snapshot too.

Each feed is assumed to carry sources as well as binaries, which is what
`apt://` sources are fetched from. `sources: false` says a feed carries
none, for vendor archives that ship binaries alone:

```
        - suite: vendor
          uri: https://packages.example.com/apt
          components: non-free
          sources: false
```

##### Building from a snapshot

A suite moves: the same specification built a week apart is built from
different packages. To pin what a build sees, point the feeds at an
archive frozen in time, such as
[snapshot.debian.org](https://snapshot.debian.org/). There is no separate
setting for it -- a snapshot is a `uri` like any other, and Debian serves
its security archive from a path of its own:

```
distribution:
    release: bookworm
    uri: https://snapshot.debian.org/archive/debian/20260801T000000Z
    feeds:
        - suite: bookworm
          valid-until: false
        - suite: bookworm-updates
          valid-until: false
        - suite: bookworm-security
          uri: https://snapshot.debian.org/archive/debian-security/20260801T000000Z
          valid-until: false
```

`valid-until: false` is what makes it work. A snapshot serves the Release
file as it stood at that timestamp, `Valid-Until` included, so apt sees an
archive that expired long ago and refuses it. That refusal is right for an
archive that is meant to be current and wrong for one deliberately held in
the past, and the setting says which of the two this is. It is not
specific to snapshots: any frozen or archived mirror needs it.

The feeds pin the image and the rebuilds alike -- the bootstrap, the buildd
chroot the packages are built in, and the sources fetched by `apt://` all
come from them. Moving the timestamp changes the digest every rebuild is
stamped with, so the packages are rebuilt against the new snapshot rather
than kept from the old one.

Snapshots are slow and rate-limited compared to a mirror. The downloads
cache under `~/.cache/seine/downloads` takes most of that cost off the
second build.

When multiple YAML files are parsed, the last parsed value will be used.

#### imager

Producing the disk image (partitioning, formatting, installing the boot
loader) is done by booting a throwaway [libguestfs](https://libguestfs.org/)
appliance, which needs a kernel of its own. This is unrelated to the kernel
package installed into the produced image by the `playbook` section -- a
board needing a custom/vendor kernel still installs it via its own playbook
as usual, while the imager itself is happy with a stock Debian kernel.

 * kernel: Debian kernel package to boot the imager appliance with (e.g.
   `linux-image-amd64`). Defaults to a sensible package for the target
   `architecture` if not specified.
 * hypervisor: path to the qemu system emulator to boot the appliance
   with (e.g. `/usr/bin/qemu-system-aarch64`). Only needed when
   cross-building for an `architecture` other than the host's; defaults
   to a sensible binary for the target `architecture` if not specified.

When cross-building (target `architecture` different from the host's),
seine automatically builds a libguestfs "fixed appliance" for the target
architecture instead of relying on the host's own kernel (which supermin,
libguestfs's appliance builder, cannot cross-build). This runs the target
architecture under emulation to build the appliance once, then caches it --
the first cross-arch build is noticeably slower than same-arch builds, but
that cost isn't paid again on subsequent builds.

#### packages

The `packages` section lists Debian source packages to rebuild before the
image is composed, for instance to carry a patch the distribution does not
have. Each entry says where its source comes from:

```
packages:
    - source: apt://busybox
      profiles:
          - nocheck
      patches:
          - patches/0001-mark-the-banner-as-rebuilt.patch
```

The following attributes are supported:

| Attribute         | Required | Description                                     |
| ----------------- |:--------:| ----------------------------------------------- |
| source            | yes      | URI the source is fetched from (see below)      |
| after             | no       | Packages that shall be built before this one    |
| before            | no       | Packages that shall be built after this one     |
| cross             | no       | Cross-compile (see below), defaults to the sane |
| options           | no       | Debian build options (`DEB_BUILD_OPTIONS`)      |
| patches           | no       | Patches to apply, relative to this YAML file    |
| priority          | no       | Build order, `0`-`999`, `500` by default        |
| profiles          | no       | Debian build profiles (`--profiles`)            |
| revision          | no       | Local version suffix, `mod1` by default         |
| source_date_epoch | no       | Date to build at, seconds since the epoch       |

Three kinds of `source` are understood:

 * `apt://<package>[=<version>]` takes the distribution's own source
   package, at the version specified or the current one.
 * `https://.../<package>_<version>.dsc` takes a source package published
   elsewhere. It has to be a `.dsc`: an upstream tarball on its own has no
   `debian/` directory to build from.
 * `git://<host>/<path>[;branch=<branch>][;rev=<commit>][;protocol=<proto>]`
   takes a packaging tree that carries its own `debian/` directory, in the
   same notation bitbake uses. The remote is reached over https unless
   `protocol` says otherwise, and `rev` is required: a branch name moves,
   and a build that cannot be repeated is not worth calling reproducible.

##### Fetching over ssh

A `git://` source whose remote wants an ssh key says so with
`;protocol=ssh`, and names the user to log in as -- the clone happens in a
container, where nothing else says who you are:

```
packages:
    - source: git://git@example.com/team/busybox.git;protocol=ssh;rev=1e4a0c9
```

seine needs no option for this. It forwards the ssh agent of the user it
runs as into the container that clones, along with `~/.ssh/known_hosts` so
the remote can be recognised, and does so only for the packages that asked
for ssh. Your private keys stay on the host: the container asks the agent
to sign and never sees a key itself, which matters because that container
runs build scripts fetched from elsewhere with more namespace privileges
than the others seine builds.

The consequence is that a key the agent does not hold is a key seine cannot
use, whatever `~/.ssh/config` says about it. Load it first:

```sh
ssh-add ~/.ssh/id_example
ssh-add -l          # what the build will be able to authenticate with
```

Building with no agent running at all fails with `SSH_AUTH_SOCK is unset`
rather than hanging on a password prompt no one can answer.

A package that build-depends on another has to be built after it. Say so
with `after` (or `before`), naming the other package:

```
packages:
    - source: apt://application
      after:
          - library
    - source: apt://library
```

Naming a package that the specification does not build is an error rather
than a constraint that is quietly ignored, as is a set of packages whose
constraints depend on each other in a circle.

`priority` decides the order of packages that no `before`/`after` separates,
between `0` and `999`, `500` by default, lowest first -- as it does for
playbooks. Constraints win over it, so adding one does not rearrange the
packages around it.

Two files naming the same source package are describing one package
rather than asking for two builds of it: the entries are merged, setting
by setting, as partitions with the same label are. A setting already
given wins, so a fragment can carry the parts that do not change while
the specification including it settles the rest. Files given on the
command line are merged in the order they appear, and a `requires` is
loaded after the file that asked for it.

A file that wants to describe a package *without* asking for it to be
built -- an architecture file naming a kernel flavour, say -- puts the
entry under [`defaults`](#defaults) instead.

Patches are applied in the way the source format calls for. A
`3.0 (quilt)` package gets them added to `debian/patches/series`; anything
else has them applied to the tree, and committed if that tree came from
git, so that packaging which records its own revision keeps working.

Rebuilt packages are made available to the rest of the build through a
local apt repository under `~/.cache/seine/packages/`: the playbooks
install them with an ordinary `apt` task, later packages build against
them, and the imager can boot a kernel from them. They are preferred over
the distribution's own copies, and carry a local revision that sorts above
its versions, so a rebuild is what gets installed. The repository exists
only on the machine that builds the image and is removed from the image's
apt configuration before it is packed up.

A package is not rebuilt again while nothing about it has changed --
including the content of its patches, and including anything it is built
after. A package compiled and linked against another has to be rebuilt
when that one changes, so a change to a library rebuilds what `before`
and `after` say is built on it, however many packages down the chain.
Use `--rebuild` to force one.

##### Local versions

Every rebuilt package is given a version of its own: a changelog entry is
added marking the source `UNRELEASED` and appending `revision` to the
version, `mod1` unless the specification says otherwise. So a rebuilt
`busybox 1:1.37.0-6` is installed as `1:1.37.0-6+mod1`.

It is there so that what a machine is running can be read off `dpkg -l`
rather than guessed at, and so that apt prefers the rebuild on version
rather than only because of the pin. The entry is dated at
`SOURCE_DATE_EPOCH`, so it does not change between rebuilds of the same
source.

For a kernel it is not optional. Debian's packaging refuses to disable
signed code in a build that claims to be a release, and without disabling
it the rebuild is named `linux-image-<abi>-<flavour>-unsigned` -- while
the metapackage everything installs depends on
`linux-image-<abi>-<flavour>`, which is built by a *different* source
package that signs it with a key nobody else has. A kernel rebuilt
without this is a kernel nothing installs, and the image comes out with
the distribution's own, looking exactly as it should.

Marking the source unreleased also earns the kernel an ABI name of its
own -- Debian derives it from the changelog, so `6.1.0-50` becomes
`6.1.0-51` -- which is what keeps a reconfigured kernel from being
mistaken for the distribution's.

##### Rebuilding the kernel

Debian's kernel is built to boot anything, which makes it large for an
appliance that knows what it runs on. Settings that only mean something
for one kind of package go under `extends`, named after that kind:

```
packages:
    - source: apt://linux
      extends:
          kernel:
              config:
                  - configs/slim.fragment
      profiles:
          - pkg.linux.nokerneldbginfo
          - pkg.linux.notools
```

`config` lists kconfig fragments, given relative to the YAML file listing
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
`examples/common/amd64.yaml` says so under [`defaults`](#defaults). A
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
of a long build rather than the start. `examples/pc-image/configs/slim.fragment`
documents what it leaves enabled, and why, for this reason.

A kernel is also where the rebuild cache earns its keep: the fragments are
part of what decides whether a package needs rebuilding, by content, so
editing one is enough to ask for a new kernel -- and not editing one means
the hours are paid once.

##### Bring your own kernel

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

###### What is kept of Debian's patches

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

###### Bringing your own patches

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

###### What you do not get

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
   for the distribution's kernel, which is the intent.
 * **`linux-libc-dev` from the new tree.** It is built and it is in the
   local repository, above the distribution's, so packages built after
   the kernel and the image itself get its headers. Usually what you
   want; worth knowing when it is not.

##### Cross-compiling

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

##### Reproducibility

Builds are given a `SOURCE_DATE_EPOCH`, taken from the package's changelog
unless `source_date_epoch` says otherwise, and sbuild builds under a fixed
path. Patches committed to a git tree are committed at that same date under
a fixed identity, so their commit hashes -- and anything embedding them --
do not change from one rebuild to the next.

What is left is the archive the source and the build dependencies come
from, which moves under an unpinned rebuild the same way it moves under an
unpinned image. Pin the versions in the `apt://` sources, or pin the whole
archive by building from a snapshot (see
[Building from a snapshot](#building-from-a-snapshot)).

##### A note on privileges

Packages are built by `sbuild` in its `unshare` mode, inside a container
that is given `CAP_SYS_ADMIN` and an unmasked `/proc`, which the nested
user namespaces it creates need. This container is more privileged than
the others seine builds and is used only to build packages. It remains
unprivileged as far as the kernel is concerned: uid 0 inside it is the
unprivileged user running seine, so what it can reach is that user's own
files rather than the machine's. No `sudo` is used or required, as
elsewhere in seine.

#### defaults

An entry under `packages` means *build this*. `defaults` holds package
entries that only describe a package, and are used if something else asks
for it to be built:

```
# examples/common/amd64.yaml
defaults:
    packages:
        - source: apt://linux
          extends:
              kernel:
                  flavour: amd64
```

That file now says which kernel `amd64` means without every amd64 image
rebuilding a kernel it never asked for. Two rules:

 * **A default never creates a package.** If nothing under `packages`
   names it, it describes nothing and is dropped.
 * **The last default loaded wins**, and anything under `packages` beats
   all of them. Files are listed from the general to the particular --
   an architecture, then a board, then what is being built -- so the
   particular file gets the last word, while the file actually asking
   for the build outranks every description of it.

Settings are merged one at a time, so a board naming a `featureset` keeps
the `flavour` its architecture gave it. Entries are matched by source
package, as under `packages`, so a default written `apt://linux` applies
to a specification that pinned `apt://linux=6.12.101-1`.

A default is parsed whether or not anything uses it: a misspelt setting
in an architecture file is reported by the file that holds it rather than
waiting for the one image that rebuilds a kernel.

`defaults` holds package entries only. Playbooks already append rather
than replace, and the other sections are merged by key.

#### playbook

Ansible playbooks will be used to add packages to the system or configure them.
The `playbook` section is a list of `name` / `tasks` pairs:

```
playbook:
    - name: first playbook
      tasks:
          ...
    - name: second playbook
      tasks:
          ...
```

Playbooks may be given a priority between `0` and `999` with `0` being the
highest priority and `500` the default:

```
playbook:
    - name: first playbook but apply towards the end
      priority: 900
      tasks:
          ...
    - name: second playbook but apply early
      priority: 100
      tasks:
          ...
```

Frequently used tasks include:
 * `apt`
 * `debconf`

Additional packages may be installed as follows:

```
playbook:
    - name: install essential packages
      tasks:
          - name: base set
            apt:
                state: present
                name:
                    - ssh
                    - vim
```

and here is how the `locales` package may be configured:

```
playbook:
    -  name: configure locales to French
       tasks:
        - name: set default locale to fr_FR.UTF-8
          debconf:
              name: locales
              question: locales/default_environment_locale
              value: fr_FR.UTF-8
              vtype: select
```

A minimal image that includes `apt` is used as starting point; `seine` adds
just `python3`/`python3-apt`/`attr` to it (removed again once the build is
done) and runs `ansible-playbook` from the host, connecting into the
container instead of installing `ansible` there -- this keeps ansible
itself off the (possibly foreign-architecture, emulated) target entirely.
Playbooks execute according to their `priority`. A different starting point
may be specified with the `baseline` keyword in the `playbook`:

```
playbook:
    - baseline: debian:bookworm
      tasks:
          - name: ...
            apt:
                ...
```

As `seine` uses `podman` behind the scene to create the root file-system in
a container, the `image` specified as `baseline` may be anything that can be
fetched from the `podman` or `docker` registries. The `image` shall however
have `apt` pre-installed (and `qemu-user-static` binaries for the host
architecture when building images for a foreign architecture).
 
#### image

Last but not least, the 'image' section defines the partition and volumes to be
created in the disk image. The following top-level attributes are supported:

 * `filename`
 * `bootlets`
 * `partitions`
 * `size`
 * `table`
 * `volumes`

An `image` shall have at least one partition defined and an output `filename`
specified. The `size` of the disk `image` may be omitted and it will then be
estimated (as the sum of the various partition sizes plus some overhead). The
partition `table` may either be `gpt` or `msdos`.

#### bootlets

Bootlets are binary firmware files placed at specific locations on the boot
media so they can be found by the hardware boot ROM. Examples include: u-boot,
Arm Trusted Firmware (ATF), etc.

The following attributes are supported:

| Attribute | Required | Description                              |
| --------- |:--------:| ---------------------------------------- |
| align     | no       | Expected alignment in Kilobytes (KiB)    |
| file      | yes      | Path to the binary to be copied (*)      |

(*) The specified file will be copied from the image created by the `playbook`,
    a package should therefore install it.

#### partitions

Disk partitions are defined with the following attributes:

| Attribute | Required | Description                              |
| --------- |:--------:| ---------------------------------------- |
| label     | yes      | Name of the partition                    |
| flags     | no       | Partition flags (see below)              |
| group     | no       | Name of the LVM group to join            |
| size      | no       | Size of the partition                    |
| type      | no       | File-system type (e.g. `ext4`)           |
| where     | yes*     | Where to mount the partition file-system |

(*) Required unless the partition is a LVM physical volume

A partition may have the following flags:

| Flag     | Description                                          |
| -------- | ---------------------------------------------------- |
| boot     | system may boot from this partition                  |
| lvm      | partition will be used as a physical volume for LVM  |

When using a `msdos` partition table, the following flags are also available
(but are mutually exclusive):

 * primary
 * extended
 * logical

A `group` shall be defined for every single partition using the `lvm` flag and
may have one or several partitions attached to it. Groups implicitly defined
in the `partitions` section may be referenced by `volumes` (see below).

#### volumes

Logical volumes share many of the attributes defined above for `partitions` but
more specifically:

| Attribute | Required | Description                              |
| --------- |:--------:| ---------------------------------------- |
| label     | yes      | Name of the volume                       |
| group     | yes      | Name of the LVM group to join            |
| size      | no       | Size of the partition                    |
| type      | no       | File-system type (e.g. `ext4`)           |
| where     | yes      | Where to mount the volume file-system    |

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
