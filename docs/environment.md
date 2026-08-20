# Environment variables

## Where seine's own files go

seine creates two kinds of thing outside the working directory: what a
build makes for itself -- container storage, downloaded and rebuilt
packages, buildd chroots, step logs, scratch space -- and a spec's own
deliverable, the `.img` it names. A home directory is commonly the
smallest filesystem on a build machine, and a couple of kernels fill it.

`SEINE_BUILD_DIR` is the one variable that moves all of it to another
drive. Unset, it defaults to `./build` under the working directory:

```
export SEINE_BUILD_DIR=/drive/seine
```

which lays out:

```
/drive/seine/
├── containers/   podman storage: the images seine built, and (containers/run)
│                 the state of what is running
├── cache/        rebuilt packages and buildd chroots, worth keeping between
│                 builds
├── downloads/    packages fetched from the distribution's feeds
├── logs/         a build's step-by-step output, one directory per run
├── deploy/       a spec's own image, when its 'filename' is a bare name,
│                 one directory per release (deploy/bookworm, deploy/trixie)
└── tmp/          scratch space: sources and images assembled mid-build
```

Each has a variable of its own too, which moves that one thing on its own
and wins over `SEINE_BUILD_DIR` when both are set:

| Variable           | What it moves | Under `SEINE_BUILD_DIR` |
|---------------------|---------------|-------------------------------------|
| `SEINE_CACHE_DIR`   | Rebuilt packages, buildd chroots | `cache/` |
| `SEINE_DL_DIR`      | Packages fetched from the feeds  | `downloads/` |
| `SEINE_LOG_DIR`     | A build's step output            | `logs/`, as `logs/<digest>/<run>/` |
| `SEINE_DEPLOY_DIR`  | A spec's own image, if its `filename` is relative | `deploy/`, as `deploy/<release>/` |
| `SEINE_TMP_DIR`     | Scratch space                    | `tmp/` |

```
export SEINE_CACHE_DIR=/drive/seine/cache
export SEINE_DL_DIR=/drive/seine/downloads
```

Storage (podman's, containing the images and the running state) has no
variable of its own; it only follows `SEINE_BUILD_DIR`. Scratch space is
never `/tmp`, which is usually a tmpfs, and unpacking a kernel tree into
memory has been known to take the machine down with it.

A spec's `image: filename:` is a deliverable, not build output, the way a
compiler writes to the `-o` it was given: it is redirected under
`deploy/<release>/` only when it is a bare relative name, so two releases
built from one checkout don't overwrite each other's image. An absolute
path is never redirected.

Nothing here is needed for a build to succeed, only to spare it work
already done or to keep a machine's smallest disk from filling up. See
[What a build keeps, and getting the space back](building.md#what-a-build-keeps-and-getting-the-space-back)
for emptying, exporting or importing what accumulates under the cache and
downloads directories.

## Other variables

| Variable | What it does |
|----------|--------------|
| `SEINE_GISTS_DIR` | Where `seine gist` and the AI chat's `gist-*` tools keep reusable spec fragments, overriding the `XDG_DATA_HOME`-based default -- not under `SEINE_BUILD_DIR`, since a gist is meant to outlive any one project. See [Gists](building.md#gists) |
| `SEINE_KEEP_DEAD_CONTAINERS` | Keeps a failed step's container instead of removing it, for reading back what podman recorded about the commands run inside. See [Keeping a failed build's containers](building.md#keeping-a-failed-builds-containers) |
| `SEINE_SIGN_KEY` | Same as `--sign-key`: sign the rebuilt packages and their repository with this gpg key |
| `NO_COLOR` | Same as `--no-color`: print a `--dry-run` plan without colour |
| `SSH_AUTH_SOCK` | Forwarded into the builder container so a `git+ssh://` package source can be fetched with your own agent |
