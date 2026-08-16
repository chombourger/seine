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
[Rebuilding the kernel](docs/kernels.md#rebuilding-the-kernel) and
[packages](docs/specification.md#packages) for how grafting actually works.

Keeping the two concerns separate also makes an image easier to reason about
after the fact: which binary packages it contains, which source packages
they came from, and which of them were modified rather than taken as
Debian shipped them are three different, individually inspectable questions
-- because system configuration lives in the specification and playbooks,
while package modifications live in a small, explicit set of grafted
packages. seine does not claim bit-for-bit reproducible images; what it
does provide -- pinned feeds, snapshot builds, a fixed `SOURCE_DATE_EPOCH`
for rebuilds -- is described in [Reproducibility](docs/building.md#reproducibility).

### Existing concepts, not a new one

Describing a target system means learning a handful of things seine adds,
not a build language of its own:

 * **Debian packages and APT** decide most of what ends up in the image.
 * **Debian source packages** are what a [graft](docs/specification.md#packages) starts from,
   when a package has to be modified.
 * **Ansible** describes system configuration -- see
   [Why Ansible](#why-ansible) below.
 * **Kernel configuration and Debian's kernel packaging** are what
   [rebuilding or replacing a kernel](docs/kernels.md) is built on.
 * **Partitions, filesystems and LVM** describe the
   [storage layout](docs/specification.md#image) of the resulting image.

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
here. See [playbook](docs/specification.md#playbook) for how playbooks fit into a specification.

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
[Kernels](docs/kernels.md) for the details, and
[What you do not get](docs/kernels.md#what-you-do-not-get) for what a graft deliberately
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
* [Specification files](#specification-files)
* [Kernels](#kernels)
* [Building](#building)
* [Software Bill of Materials](#software-bill-of-materials)

## Getting started

```
sudo apt-get install -y podman passt qemu-kvm crun python3-venv python3-guestfs
sudo adduser $USER kvm
pip install -r requirements.txt
./seine.py build examples/pc-image/main.yaml
```

`passt` is what gives a rootless container a network; `crun` is needed if
`/tmp` is its own mount. Building for an architecture other than the host's
needs that architecture's qemu system emulator too (e.g. `qemu-system-arm`
for `arm64`). Everything else a build does -- bootstrapping, rebuilding
packages, writing the repository index -- happens inside containers, so
none of those tools need to be on the host.

See [docs/getting-started.md](docs/getting-started.md) for the full
installation walkthrough, using seine behind an HTTP proxy, building a
`.deb` of seine itself, and running the test suite.

## Specification files

A specification is one or more YAML files, split with `requires` so common
settings (a release, an architecture) are written once and pulled in by the
files that need them:

```
requires:
    - bookworm
    - amd64

distribution:
    - ...   # feeds a build's apt sees

packages:
    - ...   # what apt installs, and what gets rebuilt from source

playbook:
    - ...   # ansible tasks that configure the assembled system

image:
    - ...   # partitions, filesystems, LVM -- the disk that comes out
```

`distribution` says what Debian release and feeds a build starts from;
`packages` says what apt installs and which of those are rebuilt from
Debian's own source (a graft); `playbook` is the Ansible that configures
what got installed; `image` is the disk layout the result is written to.
A file can also read what another sets, written `[[ distribution.release ]]`,
so a fragment shared across releases or boards stays a single copy.

See [docs/specification.md](docs/specification.md) for every section and
setting: variables, feeds and snapshots, package pinning and grafting,
signing, the imager, and image layout.

## Kernels

seine can reconfigure the distribution's own kernel (a `config` fragment
under `extends: kernel:`) or, when Debian does not package the tree an image
needs, build one from upstream source under Debian's own kernel packaging:

```
packages:
    - source: apt://linux
      extends:
          kernel:
              config:
                  - configs/slim-common.fragment
                  - configs/slim-amd64.fragment
```

Either way the result is still `linux-image`/`linux-headers` built and
installed the way a Debian kernel is; a kernel grafted from a tree Debian
does not package gets its own ABI name rather than posing as one of
Debian's flavours. Out-of-tree kernel modules can be brought in the same
way.

See [docs/kernels.md](docs/kernels.md) for reconfiguring, bringing your
own kernel tree, and bringing your own modules.

## Building

```
seine plan spec.yaml    # what would be built, and what changed since last time
seine build spec.yaml   # build it
```

A build only redoes what the specification changed: package rebuilds,
bootstrap steps and container images from a previous build are reused
where nothing they depend on moved. Builds are pinned to a feed snapshot
and a fixed `SOURCE_DATE_EPOCH`, cross-compile by naming a different
`distribution.architecture`, and need no elevated privileges (no `sudo`,
no bind mounts) after the packages in [Getting started](#getting-started)
are installed.

`--` builds several images together under one scheduler, sharing what
their specifications agree on -- one host bootstrap, one build of a
package two boards both ask for:

```
seine build pc-image.yaml -- rpi4-image.yaml
```

See [docs/building.md](docs/building.md) for what a build actually does,
watching one run, building several together, caching (including moving
a cache to another machine), cross-compiling, and reproducibility, and
[docs/environment.md](docs/environment.md) for every environment variable
seine reads, including where its directories go.

## Software Bill of Materials

```
seine build --sbom spec.yaml
```

writes an SPDX-format SBOM beside the image, listing both the binary
packages installed and the source packages they were built from, from
dpkg's own record. See
[docs/building.md#software-bill-of-materials](docs/building.md#software-bill-of-materials)
for details.
