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

The specification tests under `tests/spec/` parse YAML and check what comes
out of it, so they need neither containers nor a kvm-capable machine and run
in about a second. They use
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
by default), `components` (`main` by default) and `sources`. They are
listed rather than assumed because which suites a release has, and where
they are served from, differs between distributions and between a release
and its development version -- and a suite that does not exist fails
every build that follows.

The same feeds are used to build the image, to make the chroot packages
are rebuilt in, and to fetch their sources. Rebuilding a package against
different feeds than the image installs from means rebuilding a different
version than the one it would have had, and for the security suite that
means rebuilding a source without the fixes apt would otherwise deliver.

Each feed is assumed to carry sources as well as binaries, which is what
`apt://` sources are fetched from. `sources: false` says a feed carries
none, for vendor archives that ship binaries alone:

```
        - suite: vendor
          uri: https://packages.example.com/apt
          components: non-free
          sources: false
```

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
              flavour: amd64
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

##### Cross-compiling

When the target `architecture` differs from the host's, packages are
cross-compiled by default: sbuild builds them in a chroot of the host's
architecture, having pulled in `crossbuild-essential` for the target.

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

##### A note on privileges

Packages are built by `sbuild` in its `unshare` mode, inside a container
that is given `CAP_SYS_ADMIN` and an unmasked `/proc`, which the nested
user namespaces it creates need. This container is more privileged than
the others seine builds and is used only to build packages. It remains
unprivileged as far as the kernel is concerned: uid 0 inside it is the
unprivileged user running seine, so what it can reach is that user's own
files rather than the machine's. No `sudo` is used or required, as
elsewhere in seine.

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
