# seine: Slim Embedded Images Now Easy

## Introduction

seine is a command-line tool that builds images for embedded systems by
composing a Debian-based system from a YAML specification: Debian packages,
configuration, a kernel, and a storage/image layout go in; a bootable disk
image comes out. The specification may be split across several files and
may include Ansible playbooks for installing and configuring packages.

The mental model is deliberately small:

```
Debian + composition = target system
```

seine does not invent a new distribution, a new package format, or a new
build-time scripting language. It starts from Debian, because Debian already
answers the questions an embedded image has to answer -- what packages exist,
how they depend on each other, how they are updated -- and composes a target
system from what Debian already provides, adding only what a specific board
or image genuinely needs on top.

### Composition, not a bespoke package universe

Debian is fundamentally a *binary* distribution: a `.deb` is built once,
independently of any particular system that will install it, and can then be
installed on any system whose dependencies it satisfies. seine preserves that
property wherever practical. It composes systems primarily at *image* build
time -- taking existing Debian binary packages and arranging them, through
configuration, into a target system -- rather than treating every image
configuration as a reason to rebuild an otherwise identical package. The
common case is not "one custom binary universe per board"; it is packages,
selected and configured, becoming a system:

```
packages + configuration + kernel + storage layout -> target system
```

This is a different concern from *producing* a package in the first place:

```
source -> Debian binary package
```

seine keeps the two apart on purpose. Package production is Debian's job,
done by Debian's own tools; system composition is seine's job. When an
existing package is genuinely not enough -- a patch the distribution does not
carry, a kernel built from a tree Debian does not package -- seine can graft
Debian's own packaging onto a modified source and rebuild it, but this is an
explicit, visible exception, not the normal way packages end up in an image.
The result of a graft is an ordinary Debian package fed back into the same
composition step as everything else, so grafting a handful of packages does
not change how the rest of the system is built. See
[Rebuilding the kernel](#rebuilding-the-kernel) and
[packages](#packages) for how grafting actually works.

Keeping the two concerns separate also makes an image easier to reason about
after the fact: which binary packages it contains, which source packages
they came from, and which of them were modified rather than taken as
Debian shipped them are three different, individually inspectable questions
-- because system configuration lives in the specification and playbooks,
while package modifications live in a small, explicit set of grafted
packages. seine does not claim bit-for-bit reproducible images; what it
does provide -- pinned feeds, snapshot builds, a fixed `SOURCE_DATE_EPOCH`
for rebuilds -- is described in [Reproducibility](#reproducibility).

### Existing concepts, not a new one

Describing a target system means learning a handful of things seine adds,
not a build language of its own:

 * **Debian packages and APT** decide most of what ends up in the image.
 * **Debian source packages** are what a [graft](#packages) starts from,
   when a package has to be modified.
 * **Ansible** describes system configuration -- see
   [Why Ansible](#why-ansible) below.
 * **Kernel configuration and Debian's kernel packaging** are what
   [rebuilding or replacing a kernel](#kernels) is built on.
 * **Partitions, filesystems and LVM** describe the
   [storage layout](#image) of the resulting image.

None of these are seine inventions. The specification format ties them
together and adds the settings particular to composing an image -- what feeds
to build from, what to graft, what the disk should look like -- but
deliberately does not grow into a general-purpose build system: a small
number of understandable concepts, applied consistently, is the point.

### Why Ansible

Ansible plays the same role a specification-only DSL would, but is already
familiar to anyone who configures Linux systems, and it stays a build-time
tool: seine drives `ansible-playbook` from the host against the container
being assembled, so Ansible itself is never installed into the target
system. Using an existing, well-understood configuration language was
preferred over inventing a seine-specific one that would only ever be used
here. See [playbook](#playbook) for how playbooks fit into a specification.

### The kernel

seine's kernel handling stays inside Debian's kernel packaging model rather
than replacing it: a rebuilt kernel is still built by Debian's packaging,
still produces `linux-image`, `linux-headers` and the rest under Debian's
naming, and still installs the way a Debian kernel does. Reconfiguring or
grafting a kernel are extensions to that packaging, applied for cases where
the distribution's own kernel is not what an image needs -- not a separate
kernel build system living beside it.

That packaging distinguishes **architecture**, **featureset** and
**flavour**, and seine keeps that distinction rather than collapsing it: a
flavour identifies a real, meaningful configuration (`amd64`'s ordinary
kernel is not the same flavour as its realtime one), and a kernel grafted
from a tree Debian does not package is given a distinct ABI name rather than
allowed to masquerade as one of Debian's own flavours. See
[Kernels](#kernels) for the details, and
[What you do not get](#what-you-do-not-get) for what a graft deliberately
does not carry over.

### How a build actually runs

Mechanically: seine requires no elevated privileges after installation (no
`sudo`, no bind mounts). The root file-system is first assembled in a
container -- seine uses podman, which is daemon-less -- then exported as a
tarball. A throwaway [libguestfs](https://libguestfs.org/) appliance, run
under qemu/kvm, partitions and formats the disk image and installs the boot
loader, since that step needs to create the disks/partitions themselves.

### The trade-off

seine intentionally does not try to expose every build-time mechanism a
package or an image might conceivably need. It favours a small,
understandable system-composition model, backed by ordinary Debian
packaging, over arbitrary build-time flexibility. That is a real
constraint -- some things are out of scope, or have to go through the
graft escape hatch rather than a setting of their own -- traded for a
model where what an image is built from, and why, stays legible.

## Contents

* [Introduction](#introduction)
  * [Composition, not a bespoke package universe](#composition-not-a-bespoke-package-universe)
  * [Existing concepts, not a new one](#existing-concepts-not-a-new-one)
  * [Why Ansible](#why-ansible)
  * [The kernel](#the-kernel)
  * [How a build actually runs](#how-a-build-actually-runs)
  * [The trade-off](#the-trade-off)
* [Getting started](#getting-started)
  * [Installation](#installation)
  * [Using an HTTP proxy](#using-an-http-proxy)
* [Running the tests](#running-the-tests)
  * [The full plan](#the-full-plan)
* [Specification files](#specification-files)
  * [Variables](#variables)
  * [distribution](#distribution)
  * [defaults](#defaults)
  * [packages](#packages)
  * [imager](#imager)
  * [playbook](#playbook)
  * [image](#image)
* [Kernels](#kernels)
  * [Rebuilding the kernel](#rebuilding-the-kernel)
  * [Bring your own kernel](#bring-your-own-kernel)
  * [Bring your own modules](#bring-your-own-modules)
    * [Naming the kernels](#naming-the-kernels)
    * [What comes out](#what-comes-out)
    * [What the build is told](#what-the-build-is-told)
    * [Cross-compiling modules](#cross-compiling-modules)
* [Building](#building)
  * [What a build would do](#what-a-build-would-do)
  * [While a build runs](#while-a-build-runs)
  * [Keeping a failed build's containers](#keeping-a-failed-builds-containers)
  * [Building in parallel](#building-in-parallel)
  * [Where the time went](#where-the-time-went)
  * [Cross-compiling](#cross-compiling)
  * [Reproducibility](#reproducibility)
  * [A note on privileges](#a-note-on-privileges)
  * [What a build keeps, and getting the space back](#what-a-build-keeps-and-getting-the-space-back)
  * [Moving a cache to another machine](#moving-a-cache-to-another-machine)
  * [Carrying only what a project needs](#carrying-only-what-a-project-needs)
  * [Putting seine's directories on another drive](#putting-seines-directories-on-another-drive)
* [Software Bill of Materials](#software-bill-of-materials)

## Getting started


### Installation

The easiest way to get started is to install the following packages, all
available from your distribution:

```
sudo apt-get install -y podman passt qemu-kvm crun python3-venv python3-guestfs
sudo adduser $USER kvm
```

`passt` provides `pasta`, which podman uses to give a rootless container a
network. Without it every `podman run` fails with `could not find pasta` and
a build stops at its first container.

What has to be on the machine seine runs on is only this, plus
`ansible-playbook` below, and `gnupg` for a build that signs what it
produces. Everything a build does to a distribution happens inside a
container: `mmdebstrap` bootstraps the root file-system, `sbuild` rebuilds
packages, `apt-ftparchive` writes the repository index, `debsbom` reads the
SBOM. None of those need installing here, and looking for them on the host
says nothing about whether a build will work.

`gnupg` is the exception that proves it: signing runs on the host on
purpose, so that the key stays with the agent already holding it and no
container ever sees it. See [Signing](#signing).

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
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install containers.podman
```

`--system-site-packages` is needed for `python3-guestfs`, which pip cannot
install and seine imports.

seine runs `ansible-playbook` as a command rather than importing it, so it
has to be on `PATH` -- from the activated environment, or installed
system-wide with `apt-get install -y ansible`. The `containers.podman`
collection is what connects it to the target container; `ansible-galaxy` is
part of the `ansible` package rather than `ansible-core`, so a machine with
only the latter installs the collection with a system `ansible-galaxy` or
`pip install ansible`.

You may then either use seine in place (use the `seine.py` script from the top
level directory of this source tree) or generate a binary package. To build
a sample image without installing `seine` on your system, use:

```
./seine.py build examples/pc-image/main.yaml
```

That image boots the distribution's own kernel. To rebuild one cut down to
what the machine actually has -- hours the first time, and cached
afterwards -- name the kernel fragment as well:

```
./seine.py build examples/pc-image/main.yaml examples/slim-kernel.yml
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

`seine --help` lists the commands it has, and `seine COMMAND --help` says
what one of them takes.

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

## Running the tests

The tests under `tests/spec/` use
[avocado](https://avocado-framework.github.io/), which Debian does not
package -- install it in a virtual environment that can still see the
system packages pip cannot install:

```
sudo apt-get install -y python3-guestfs python3-libarchive-c
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt avocado-framework 'setuptools<81'
avocado run tests/spec/*.py
```

`--system-site-packages` is what makes `guestfs` and `libarchive`
importable in there. `libarchive` is what reads a kernel module out of the
package it was shipped in, whichever compression that kernel chose.
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

### The full plan

`tests/spec/images.py` builds images for real -- `pc-image` and
`rpi4-image`, each for bookworm and for trixie, each with the 6.18 kernel
of `examples/linux-6.18/`. Four kernels and four images is a long run,
so they cancel themselves unless asked for:

```
SEINE_TEST_PLAN=full avocado run --filter-by-tags=full tests/spec/images.py
```

Each of the four builds with `--jobs 4`, and the four may run beside each
other -- what they share is guarded by the same locks that let two
developers build on one machine.

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

The plan also carries a cache from one machine to another, since the
promise of `seine cache export` is not testable in pieces. It builds the
busybox rebuild for the host's own architecture in a space of its own,
exports what that build kept, imports it into a second space and builds the
same specification there -- then checks the second build did none of the
first one's work: no package rebuilt, no buildd chroot unpacked again, and
the container images the same ones by id. The busybox rebuild rather than a
kernel: it is a real package build, patch and stamp included, without
spending an evening compiling. Both spaces are the test's own, so nothing
it does touches the caches of whoever runs it.

## Specification files

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

### Variables

A file may read what the specification sets, written `[[ ... ]]`, so that
what is true of any architecture or any release is written once instead of
being copied once per each. `examples/common/debian.yaml` is the whole of
what a Debian release's feeds look like:

```
distribution:
    feeds:
        - suite: [[ distribution.release ]]
        - suite: [[ distribution.release ]]-updates
        - suite: [[ distribution.release ]]-security
          uri: http://security.debian.org/debian-security
```

and `trixie.yaml` beside it is the name and nothing else:

```
requires:
    - debian

distribution:
    release: trixie
```

What can be read is the specification itself, by the path to the setting:
`distribution.architecture`, `distribution.release`, `imager.kernel`. Where a
setting is written makes no difference -- the files are all read before any
of them is loaded, so a fragment sees what a fragment listed after it says,
and the file doing the asking sees its own settings. A name the specification
never sets is an error rather than an empty value, and every one of them is
reported at once, with the file that asked for it.

Two things are deliberately not supported:

 * `[% if %]` and the rest of jinja's blocks. Which fragments apply is what
   `requires` already says, in a file that can be read without being run.
 * a `requires` entry that names a file through a variable, which would make
   the files a specification is built from depend on what those files say.

The delimiters are `[[ ]]` rather than jinja's own `{{ }}` because a
specification carries ansible tasks, and ansible templates those itself, on
the target, when the playbook runs. `{{ ansible_facts.hostname }}` in a task
is passed through untouched.

### Redacting what should not be printed

`seine plan` and `seine build --dump` print the specification these files
merge into, and a specification holds passwords, tokens and keys. A file
that carries one says so, in a `redact` section of its own:

```
redact:
    - \$6\$\S+

playbook:
    - name: configure user accounts
      tasks:
        - name: set root password
          user: name=root password=$6$X1SbKPWJ2tkpDFZb$khtcnptnTxWEYA4...
```

which is printed as:

```
    user: name=root password=<redacted:45db4289>
```

Each entry is a regular expression, and every string in the specification is
matched against all of them. What matches is replaced, not the value holding
it, so a pattern can name the secret inside a larger string -- the password
of an ansible task whose other arguments are worth reading.

The digest is of the value that was taken out. A plan compares one
specification against another, and both are printed this way, so a constant
would have a changed password read as no change at all. It says nothing
about the secret beyond whether it is the one that was there before.

The section is merged from every file the way the rest of a specification
is, so the fragment holding a secret is the fragment that declares it, and
declaring it there covers every image built from that fragment. It only
governs what is printed: the build itself still sees the value, and so does
the target.

### distribution

The `distribution` section will be used to specify the primary source of the
packages that will make the end system. The following attributes are supported:

 * source: either `debian` or `ubuntu`
 * release: codename of the version to be used (e.g. `bookworm`)
 * architecture: one of `amd64`, `arm64` or `armhf`
 * uri: base location of the distribution packages
 * components: archive components every feed carries (`main` by default)
 * feeds: apt feeds to build from (see below)

#### feeds

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

#### Building from a snapshot

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

A second build of the same snapshot fetches nothing: what the first one
downloaded is in the cache under `~/.cache/seine/downloads`, and a snapshot
serves the same bytes for a timestamp for ever, so nothing there can go
stale.

When multiple YAML files are parsed, the last parsed value will be used.

### defaults

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

### packages

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
| apt-preferences   | no       | What this build may install (see [Pinning a build](#pinning-a-build)) |
| before            | no       | Packages that shall be built after this one     |
| cross             | no       | Cross-compile (see [Cross-compiling](#cross-compiling)) |
| extends           | no       | Settings for a kind of package (see [Bring your own modules](#bring-your-own-modules)) |
| name              | no       | The source package this builds, when the URI does not say |
| options           | no       | Debian build options (`DEB_BUILD_OPTIONS`)      |
| patches           | no       | Patches to apply, relative to this YAML file    |
| priority          | no       | Build order, `0`-`999`, `500` by default        |
| profiles          | no       | Debian build profiles (`--profiles`)            |
| revision          | no       | Local version suffix, `mod1` by default         |
| scope             | no       | Who the rebuild is for (see [Host packages](#host-packages)) |
| source_date_epoch | no       | Date to build at, seconds since the epoch       |
| version           | no       | Upstream version, for a tree seine packages itself |

Anything fetched over http is checked against a hash the specification
declares, since nothing else vouches for it:

```
packages:
    - source: https://example.com/busybox_1.37.0-6.dsc
      sha256: 3f5e…
```

The file is hashed where it lands, on the machine running seine rather
than in the container that fetched it -- a container asked to verify
itself proves nothing. A mismatch stops the build and prints both hashes.
The hash is part of what decides whether a package needs rebuilding, so a
file that changes under the same URL is a different package rather than a
cached one.

A download with no hash to check against is not an error, but it says so,
with the hash it just computed and the file to put it in:

```
warning: nothing vouches for 'linux-6.18.43.tar.xz'
  it hashes to 9a1c…
  add 'upstream-sha256: 9a1c…' to examples/linux-6.18/kernel.yml
```

The file it names is the one that carries the URI, which for a
specification assembled from several is not necessarily the one that
named the package.

`--require-hashes` turns that warning into a refusal, for a build that
should not fetch anything nobody vouched for:

```
seine build --require-hashes spec.yaml
```

It is answered when the specification is parsed rather than after a
download, since whether a source has anything vouching for it is knowable
without fetching it, and it names every source at fault in one go rather
than one per attempt. seine's own image tests build this way, so an
example that loses its hash fails them.

`apt://` and `git://` need no hash and take none: an apt source is
checked against the archive's signed index, and a git revision is the
hash of what it names. Both answer for themselves, more strongly than a
hash written down beside the URL would.

Three kinds of `source` are understood:

 * `apt://<package>[=<version>]` takes the distribution's own source
   package, at the version specified or the current one.
 * `https://.../<package>_<version>.dsc` takes a source package published
   elsewhere. It has to be a `.dsc`: an upstream tarball on its own has no
   `debian/` directory to build from.
 * `git://<host>/<path>[;branch=<branch>][;rev=<commit>][;protocol=<proto>]`
   takes a packaging tree that carries its own `debian/` directory. The
   remote is reached over https unless
   `protocol` says otherwise, and `rev` is required: a branch name moves,
   and a build that cannot be repeated is not worth calling reproducible.

#### Fetching over ssh

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
entry under [`defaults`](#defaults) instead. Such an entry may name the
package by `name` alone, since where the source comes from is the job of
whichever file asks for the build.

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

#### What the repository holds

Everything a rebuild produced: the binary packages, the `.changes` and
`.buildinfo` that say how they were made, sbuild's build log, and the
source package they were built from -- the `.dsc` and the tarballs it
names. `apt-ftparchive` writes a `Packages` index over the binaries and a
`Sources` index over the sources.

The source package is published once per package, however many
architectures were built from it: it is one set of files named by one
`.dsc`, and a Debian source package is not built for an architecture.
Every build's stamp names it all the same, so it is retired when the last
of those builds is, rather than when the first one is superseded.

Nothing seine runs reads the `Sources` index -- a rebuild fetches its
source from the distribution, not from here. It is written for the
machine handed a cache, and for anything asking what a modified binary
was built from, which for a package under a copyleft licence is a
question with an answer someone is entitled to.

It is not free: the orig tarball is carried with them, which for busybox
is a couple of megabytes and for a kernel is a couple of hundred.
`seine cache clear packages` is what takes it back.

#### Local versions

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

#### Signing

`--sign-key` signs what a build produced, with a key seine never sees:

```
seine build --sign-key FA5C9FECC03529BF spec.yaml
```

The key is named however gpg will take it -- a short id, a long one, a
fingerprint, an email address -- and `SEINE_SIGN_KEY` says the same
thing. It is not a specification setting on purpose: which key a machine
signs with belongs to that machine and the person at it, so a
specification naming one would not build for anybody else.

Three things are signed. The `.dsc` and the `.changes` carry their
signature inside them, so it travels with them into the repository and on
to whoever is handed the cache. The repository itself gets a `Release`
file signed both ways -- `InRelease` and `Release.gpg` -- which is what
apt verifies.

Every `gpg` runs on the machine seine was started on and talks to the
agent already running there. Nothing gnupg-related goes into a container:
not the key, not the agent's socket. That matters most for the builder,
which is the most privileged container seine makes and runs build scripts
fetched from an archive -- handing it an agent socket would let anything
it runs ask for a signature over anything at all. Whether the agent
prompts for a passphrase, caches it, or keeps the key on a smartcard
stays between you and your agent.

The public half is exported into the repository, named for the key
(`5B89F388.gpg`), and installed as `/etc/apt/keyrings/` in everything
that reads from it. A repository that is signed is then read with
`signed-by` rather than `trusted=yes`, so apt verifies it wherever it is
used -- the sbuild chroots, the container composing the root file-system,
and the imager. Unsigned, it stays trusted for having been made here a
moment ago.

The keyring stays in the image. The sources.list entry and the pin do
not: the repository they name is on the machine that did the building.
What is left is a trust anchor, so an image later pointed at an update
server signed by the same key can verify it.

Who signed a package is part of what says whether it needs building
again. The `.dsc` and the `.changes` are different files when they are
signed by a different key, or by none, however identical the `.debs`
beside them -- so changing the key, or adding one to a specification
already built, rebuilds. It follows that a cache built by somebody else
is rebuilt here rather than adopted, which is the honest answer: their
signature is not ours to publish.

Signing needs `gnupg` on the host, alongside the other host
prerequisites.

#### Pinning a build

A rebuild is compiled against whatever apt hands its chroot, which is not
always what it should be. The clearest case is a specification that
rebuilds a kernel: that puts a `linux-libc-dev` in the repository which
sorts above the release's own, and every package built afterwards is then
compiled against kernel headers its source never expected. busybox stops
outright, on the CBQ definitions the newer headers no longer have.

`apt-preferences` says what a package's own build may install, in apt's
language, copied verbatim into a fragment under
`/etc/apt/preferences.d/` in the chroot that builds it:

```
packages:
    - source: apt://busybox
      apt-preferences: |
          Package: linux-libc-dev
          Pin: origin ""
          Pin-Priority: -1
```

That refuses the rebuilt copy for this build alone -- a `file://`
repository is the one with an empty origin -- and leaves apt to take
whatever the archive offers, security updates included.

Prefer it to pinning a suite. `Pin: release n=bookworm` at a priority
above 1000 forces the *base* suite's version over the newer one in
`bookworm-security`, which is a downgrade, and apt stops rather than
perform one:

```
E: Packages were downgraded and -y was used without --allow-downgrades
```

What a pin names is often particular to a release -- a version, or a
suite by name -- so it may be keyed by release instead:

```
packages:
    - source: apt://busybox
      apt-preferences:
          bookworm: |
              Package: linux-libc-dev
              Pin: version 6.1.*
              Pin-Priority: 1001
          trixie: |
              Package: linux-libc-dev
              Pin: version 6.12.*
              Pin-Priority: 1001
```

A release the mapping does not name gets none, so a package needing a
pin for one release alone names only that one. A plain string is for
every release.

It is taken as written rather than parsed: what can be said in that file
is [apt_preferences(5)][]'s to define, and a setting that understood it
would be a second, smaller language to keep up to date.

It reaches that package's build and no other. A buildd chroot is
unpacked for one build and thrown away, so naming a version here decides
nothing for the package beside it -- which is what makes it usable at
all, since the pin that a kernel rebuild needs is the opposite of the
one busybox needs.

What a build is allowed to install decides what comes out of it, so the
setting is part of what says whether a package needs rebuilding: change
the pin and the package is built again. What counts is the pin for the
release being built -- changing another release's does not rebuild
anything here.

[apt_preferences(5)]: https://manpages.debian.org/stable/apt/apt_preferences.5.en.html

#### Host packages

A rebuild is for the image by default. `scope` says otherwise:

| `scope`  | Built for                                                  |
| -------- | ---------------------------------------------------------- |
| `target` | The image's architecture. The default.                     |
| `host`   | The machine running seine.                                 |
| `both`   | Both, from one source.                                     |

`host` is for what is used while the image is being made rather than
inside it: a code generator a later package build-depends on, a tool the
imager runs. Those have to run on the machine doing the building, which
for a cross build is not the image's architecture.

```
packages:
    - source: apt://busybox
      scope: [host, target]
```

A single role may be written on its own; `scope: host` is `scope: [host]`.

There is one repository per release, holding every architecture built
for, the way a distribution's archive does. It is offered to the sbuild
chroots, to the container composing the root file-system and to the
imager, and apt takes from its index what the architecture it was asked
about can use -- so a package can build-depend on a host rebuild in the
ordinary way, while what is installed into the image stays the image's
own architecture.

A dependency inherits the roles of what is built on it: a package built
for the host is linked against what its dependencies installed, so those
are built for the host too, however far down the chain. A dependency that
names its own `scope` is not widened -- it is an error naming both
entries, since an explicit scope is an answer rather than a default.

`Architecture: all` binaries are built by exactly one of a package's
builds, the way Debian builds them on one buildd. Two builds producing
them would write one filename twice, and which of them landed last would
decide what the image installs.

The job goes to a native build for preference -- sbuild hands a cross
build `-B` of its own accord, since an architecture-independent binary is
sometimes made by running something that was just built -- and between
two native builds the machine's own architecture takes it.

A preference is all it is. When every build of a package is a cross
build, which is the ordinary shape of building for one board on a laptop,
the cross build is asked for them anyway: the alternative is not getting
them, and an image installing one would take the distribution's copy of a
package it asked to have rebuilt, looking exactly as it should. Most
packaging manages it, since an arch-indep binary is usually documentation
or configuration. Packaging that does not now fails rather than quietly
producing less, and `cross: false` is how to say so -- it builds under
emulation, natively, where the question does not arise.

The source is fetched, patched and packed into a `.dsc` once, and that
one source package is built twice. A Debian source package is not built
for an architecture -- it describes every one its packaging supports --
so both builds are demonstrably of the same source rather than of two
trees prepared the same way.

With two roles, the build steps carry the architecture they are for:

```
fetch:busybox
package:busybox:amd64
package:busybox:arm64
deploy:busybox
```

A package built for one architecture keeps the plain `package:busybox`,
so `--dry-run` on a specification that says nothing about `scope` reads
as it always has. `before` and `after` go on naming a package rather than
a build of one.

Publishing is one step for every architecture rather than one each. What
comes out of the builds is not a repository per architecture: an arch-all
binary belongs to all of them and is built by one of them, so the step
that decides which of an earlier build's files are superseded has to hold
the whole picture.

On a machine of the image's own architecture, `[host, target]` is one
build -- same source, same chroot, same repository -- rather than the
same work done twice.

A kernel takes one role. It is configured per architecture, down to the
name of its flavour, so one entry cannot describe two; list the
architectures as separate packages, each with the flavour that
architecture has.

### imager

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

### playbook

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
 

### image

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
              config:
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
  remove them with: podman --root ~/.local/share/seine rm -f 414410d...
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

Every package is three of those steps: fetching its source, building it,
and publishing what came out into the local repository.
A fetch waits on a server rather than on the machine, so a large download
runs beside the compiles of other packages instead of holding a slot to
wait -- and a source that cannot be fetched says so early, rather than
once everything before it has been built. A kernel's upstream tree, which
is the largest thing seine downloads, comes down with the packaging it
will be grafted into.

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
[Building from a snapshot](#building-from-a-snapshot)).

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
downloads   253.7 MiB  /home/user/.cache/seine/downloads
packages      1.1 GiB  /home/user/.cache/seine/packages
chroots     278.4 MiB  /home/user/.cache/seine/chroots
bootstraps  485.3 MiB  /home/user/.cache/seine/bootstraps
images        9.3 GiB  /home/user/.local/share/seine
analyze      24.0 KiB  /home/user/.cache/seine/analyze
scratch     365.5 KiB  /var/tmp/seine
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
chroots     278.4 MiB  /home/user/.cache/seine/chroots
packages      1.1 GiB  /home/user/.cache/seine/packages
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

seine keeps two kinds of large thing outside the working directory: the
caches that spare the next build the work this one did, and what a build
makes for itself -- the container images, in podman storage of its own,
and the scratch space sources and images are assembled in. A home
directory is commonly the smallest filesystem on a build machine, and a
couple of kernels fill it. Two optional environment variables move them
elsewhere; unset, everything stays where it has always been.

| Variable          | Default                  | What it moves |
|-------------------|--------------------------|---------------|
| `SEINE_CACHE_DIR` | `~/.cache/seine`         | The caches kept between builds: downloaded packages, rebuilt packages, buildd chroots |
| `SEINE_BUILD_DIR` | see below                | What a build makes for itself: podman storage (`storage/`) and the scratch space (`tmp/`) |

```
export SEINE_CACHE_DIR=/drive/seine/cache
export SEINE_BUILD_DIR=/drive/seine/build
```

`XDG_CACHE_HOME` is honoured for the caches when `SEINE_CACHE_DIR` is not
set, so a machine that has already moved every cache does not leave this
one behind. Without `SEINE_BUILD_DIR`, the images stay in
`~/.local/share/seine` and the scratch space follows `TMPDIR`, or
`/var/tmp/seine` when that is unset -- never `/tmp`, which is usually a
tmpfs, and unpacking a kernel tree into memory takes the machine with it.

The two are separate on purpose: caches are worth keeping, while what a
build makes for itself is not. Setting one does not move the other.

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
