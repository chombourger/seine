# Building

This guide is for people who use seine to build an image. It explains the
commands you are likely to run, how to inspect a build, and how to recover
disk space. See [the specification reference](specification.md) for YAML
settings.

## Build an image

Run `seine build` with one or more specification files. Seine merges the
files before it builds the image.

```
seine build spec.yaml
seine build common.yaml board.yaml image.yaml
```

The first build can take a while. Later builds reuse downloads, package
builds, chroots, and container images when their inputs have not changed.

Use these options for common variations:

| Command | Use it when you want to |
| --- | --- |
| `seine build --dry-run spec.yaml` | Check what would run without changing anything. |
| `seine build --packages-only spec.yaml` | Build packages only; do not create a root file system or image. |
| `seine build --rootfs-only spec.yaml` | Create a root file-system tarball, but not a disk image. |
| `seine build --rebuild spec.yaml` | Rebuild packages even when cached results exist. |
| `seine build --sbom spec.yaml` | Write an SPDX software bill of materials beside the image. |
| `seine build --sign-key KEY spec.yaml` | Sign rebuilt packages with your GPG key. |

`--target TASK` is useful while working on one part of a build. It builds that
task and what it needs. Find task names with `seine plan --tasks-only spec.yaml`.

## Check the plan

Before a slow or important build, inspect its plan:

```
seine plan spec.yaml
seine build --dry-run --jobs 4 spec.yaml
```

The plan shows:

- The merged specification.
- Settings changed since the last successful build of these files.
- Packages already built and therefore reused.
- Remaining build steps and their dependencies.

Use `--spec-only` or `--tasks-only` to show one part. Use `--no-color`, or set
`NO_COLOR`, for plain output.

`--dry-run` accepts the normal build options. It never fetches, builds, or
writes files.

## Follow a running build

The standard display shows completed steps, active steps, and progress. Seine
writes each step's full output to a separate log file and prints that file when
a step fails.

For full command output, use:

```
seine build --verbose spec.yaml
```

This is the best first step when diagnosing a failure. In CI or when output is
redirected, seine writes regular log lines instead of an interactive display.

### Keep a failed container for inspection

Normally seine removes a failed step's container. To inspect it with podman,
keep it:

```
SEINE_KEEP_DEAD_CONTAINERS=1 seine build spec.yaml
```

At the end, seine prints the container IDs and the exact `podman ... rm`
command to remove them. Unset the variable after debugging: kept containers
consume disk space.

## Build faster with parallel jobs

By default, seine runs one build step at a time. Run independent steps in
parallel with `--jobs`:

```
seine build --jobs 4 spec.yaml
```

| Option | Meaning | Default |
| --- | --- | --- |
| `--jobs N` | Up to `N` build steps run at once. | `1` |
| `--parallel N` | Up to `N` CPU cores per package build. | Automatically divided across jobs. |
| `--resource CLASS=N` | Limit a resource class such as `net=2` or `io=4`. | Same limit as `--jobs`. |

Start with `--jobs` equal to the number of CPU cores you want the build to
use. Seine adjusts package parallelism so that adding jobs does not normally
overload the machine. Set `--parallel` only when you need a specific per-package
limit, for example for a package that cannot build in parallel.

Some steps wait for the network or disk rather than CPU. If a busy build is
limited by one of those resources, tune it explicitly:

```
seine build --jobs 4 --resource net=2 --resource io=4 spec.yaml
```

Dependencies still apply. A package that needs another package waits until the
required package is available.

## Build several images together

Separate image configurations with `--` to build them under one scheduler:

```
seine build --jobs 4 \
  common/trixie.yaml common/amd64.yaml pc-image/main.yaml \
  -- \
  common/trixie.yaml common/arm64.yaml rpi4-image/main.yaml
```

Seine shares compatible work automatically, such as a host bootstrap or a
package build. Each group still produces its own image. `seine plan
--tasks-only` shows which tasks are shared.

Use this for separate images. Use `multiconfig:` when several root file systems
must be placed on one disk image.

## Put several root file systems on one disk

`multiconfig:` names sub-builds that become separate root file systems in the
outer specification's image. This is useful for a main system and recovery
system on the same disk.

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

Each group needs exactly one partition or volume with `where: /`. The outer
`image:` section defines the disk; an `image:` section inside a group is
ignored. Build the disk normally:

```
seine build examples/main-recovery-image/disk.yaml
```

For a build pipeline rather than side-by-side systems, make a group wait for
another group:

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

`after:` and `before:` take group names. They order whole groups, not
individual files or package steps.

## Review build time

After a build, use `seine analyze` to find slow work. These commands only read
the build record.

| Command | Shows |
| --- | --- |
| `seine analyze blame [SPEC...]` | Steps, longest first. |
| `seine analyze critical-chain [SPEC...]` | The path that limited total build time. |
| `seine analyze plot [SPEC...] > build.svg` | A timeline chart as SVG. |

Without `SPEC`, the command reports the latest build. With `SPEC`, it reports
the latest build of that exact specification set.

The critical chain is usually the most useful report. Improving a step outside
that chain may not make the full build faster because it already ran alongside
the slowest work.

## Cross-compile packages

When the target architecture differs from the host architecture, seine
cross-compiles packages by default.

If a package cannot cross-compile, set `cross: false` in that package's
specification. Seine then builds it for the target architecture through QEMU.
This is slower, and requires `qemu-user-static` with binfmt support on the
host.

## Reproducible builds

Seine supplies stable build dates and paths where package tooling supports
them. To make the input archive stable too, pin package versions or build from
a Debian snapshot. See [Building from a snapshot](specification.md#building-from-a-snapshot).

## Privileges

Seine does not require `sudo` to build an image. Package builds run inside a
rootless container. The container has the permissions needed for nested build
namespaces, but root inside it is still your unprivileged host user.

## Manage cache space

Seine keeps reusable data below `./build` by default. Check how much space it
uses:

```
seine cache info
seine cache info --entries
```

| Command | Effect |
| --- | --- |
| `seine cache clear` | Remove all caches. |
| `seine cache clear chroots` | Remove only build chroots. |
| `seine cache clear --older-than 30d` | Remove entries unused for at least 30 days. |
| `seine cache info --entries-matching linux` | Find cached entries matching a regular expression. |

Clearing a cache is safe, but the next build must recreate its contents. Do
not clear caches while a build is running. Use `seine cache clear images` to
remove container images; do not remove the container-storage directory by
hand.

To keep build data on another drive, set `SEINE_BUILD_DIR`:

```
export SEINE_BUILD_DIR=/mnt/build-disk/seine
seine build spec.yaml
```

See [Environment variables](environment.md) for separate cache and download
locations.

## Share caches with CI or another machine

Export reusable caches to a tar file, then import it on the other machine:

```
seine cache export caches.tar
seine cache import caches.tar
```

Useful variants:

| Command | Use it when you want to |
| --- | --- |
| `seine cache export --spec common.yaml,board.yaml caches.tar` | Export only caches needed by one image configuration. |
| `seine cache export caches.tar all` | Include downloads as well. |
| `seine cache export --with-image-rootfs caches.tar` | Copy the bootstrapped image root file system too. |
| `seine cache import --replace caches.tar` | Start a disposable runner from the imported cache. |
| <code>seine cache export - &#124; ssh builder seine cache import -</code> | Stream the cache directly to another machine. |

The normal export includes built packages, chroots, and container images.
It leaves out distribution downloads because the receiving machine still needs
access to its package archive. An import adds to existing caches unless you use
`--replace`.

## Create and scan an SBOM

Build with `--sbom` to write an SPDX SBOM beside the image:

```
seine build --sbom spec.yaml
```

Scan it for Debian security-tracker findings:

```
seine issues spec.yaml
seine issues --sbom=image-sbom.spdx.json --min-urgency=high
```

Use `--rescan` to ignore a cached scan. `--filter=PKG` narrows results to
matching package names.

## Related documentation

| Topic | Documentation |
| --- | --- |
| Write or change a specification | [Specification reference](specification.md) |
| Install prerequisites | [Getting started](getting-started.md) |
| Configure build paths and debugging variables | [Environment variables](environment.md) |
| Understand cache behavior in detail | [Caching](caching.md) |
