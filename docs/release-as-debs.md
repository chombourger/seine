## Releasing seine as .deb packages

This builds seine's own Debian packages: `seine`, plus `seine-oci-bookworm`
and `seine-oci-trixie` (the prebuilt base images seine imports into podman
instead of pulling from a registry).

### Build for amd64

On an amd64 machine, with network access (needed once, to pull the pinned
base images):

```
dpkg-buildpackage -b -uc -a amd64
```

The `.deb` files land next to the source directory, e.g. `../seine_<ver>_amd64.deb`.

### Build for arm64 (cross build)

Install the cross toolchain once:

```
sudo dpkg --add-architecture arm64
sudo apt-get update
sudo apt-get install crossbuild-essential-arm64 python3-minimal:arm64 libstdc++6:arm64
```

Then build:

```
dpkg-buildpackage -b -uc -a arm64
```

No qemu or arm64 hardware needed: nothing runs arm64 code during the
build, it only compiles/packages for that architecture.

### Notes

- Each architecture needs its own `dpkg-buildpackage` run — one build only
  ever produces packages for one architecture.
- The base images pulled into `seine-oci-*` are pinned by digest in
  `debian/build-oci.lock`. To pick up newer point releases, refresh the
  lock first: `HOSTARCH=<arch> REFRESH=1 debian/build-oci.py <out-dir>`,
  then commit the updated lock file.
- Before installing new cross-build packages system-wide, run
  `apt-get install -s <package>` first and check the `Remove:` list —
  a foreign-architecture install can pull in an unexpectedly large
  dependency change.

### CI

`.github/workflows/debs.yml` builds both architectures on every push and
pull request, inside a `debian:trixie` container (not the runner's own
Ubuntu) so the packages target the right distro. arm64 runs natively on
GitHub's `ubuntu-24.04-arm` runners rather than cross-building, so no
cross toolchain step is needed there. On a tag push it also publishes the
`.deb` files as release assets on that tag.
