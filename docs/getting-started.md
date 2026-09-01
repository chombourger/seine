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
container ever sees it. See [Signing](specification.md#signing).

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
ansible-galaxy collection install containers.podman ansible.posix community.general
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

The tests under `tests/`, one directory per theme, use
[avocado](https://avocado-framework.github.io/), which Debian does not
package -- install it in a virtual environment that can still see the
system packages pip cannot install:

```
sudo apt-get install -y python3-guestfs python3-libarchive-c
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -r requirements.txt avocado-framework 'setuptools<81'
avocado run tests/*/*.py
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
avocado run --filter-by-tags='-container' --filter-by-tags-include-empty tests/*/*.py
```

`--filter-by-tags-include-empty` is needed because avocado otherwise drops
every test that carries no tag at all, which is all the others.

### The full plan

`tests/image/images.py` builds images for real -- `pc-image` and
`rpi4-image`, each for bookworm and for trixie, each with the 6.18 kernel
of `examples/linux-6.18/`. Four kernels and four images is a long run,
so they cancel themselves unless asked for:

```
SEINE_TEST_PLAN=full avocado run --filter-by-tags=full tests/image/images.py
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

