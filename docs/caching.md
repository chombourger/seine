# Caching

What a build keeps so the next build does not make it again. Nothing
here is needed for a build to succeed — only to spare work already done
— which is what makes removing any of it safe. `seine cache info`
shows it, `seine cache clear` removes it, and `seine cache export` /
`import` carries it to another machine. See
[What a build keeps, and getting the space back](building.md#what-a-build-keeps-and-getting-the-space-back)
for the user-facing surface; this page is the internals behind it.

## What is cached

The registry in `CACHES`:

| name | what it holds | where `ContainerEngine` puts it | portable |
|------|---------------|----------------------------------|----------|
| `downloads` | per-suite `.deb` download cache bind-mounted into `apt` (`ContainerEngine.downloads_root()` / `downloads(suite)`) | `SEINE_DL_DIR` or `SEINE_BUILD_DIR/downloads` | yes |
| `packages` | per-release flat repository of rebuilt `.deb`s, `.dsc`/`.changes`/`.buildinfo`, `.stamps` and `.stamps-spec` | `ContainerEngine.cache("packages", release)` | yes |
| `chroots` | per-`release`/`arch` sbuild chroot tarballs `*.tar.zst` + beside it `<release>-<arch>.inputs` digest | `ContainerEngine.cache("chroots", release, arch)` | yes |
| `bootstraps` | image-build package cache kept under `TMPDIR` via `--mount=type=cache` (`ContainerEngine.cache("bootstraps")`, shunted through `XDG_RUNTIME_DIR` by `_short_tmpdir()`) | `SEINE_CACHE_DIR/bootstraps` | no |
| `vendor` | per-suite flat fetch of `vendor:` artifacts (`.deb`, `.dsc`, `.orig.tar.*`, `.debian.tar.*`) | `ContainerEngine.cache("vendor", suite)` | no |
| `images` | container images in podman's storage (tooling, builder, imager, transport, source, plus pulled base images) | `ContainerEngine.root()` (`SEINE_BUILD_DIR/containers`) | yes (via `podman save` / `load`) |
| `analyze` | per-build cost records | `ContainerEngine.cache("analyze")` | no |
| `scratch` | ephemeral workbenches, unpacked sources, `tmp` | `ContainerEngine.scratch()` / `workbench()` | no |

Only `PORTABLE = ["downloads", "packages", "chroots", IMAGES]` travel
in a `seine cache export` tar. `CARRIED = ["packages", "chroots", IMAGES]`
is what an export carries when nothing is named; `downloads` is left out
unless asked for because a build still needs the archive to serve
`Packages`/`Sources` — carrying the `.deb`s only saves bandwidth.
`scratch` never travels, `vendor` is registered so `seine cache clear
vendor` exists but is never exported, and `bootstraps`/`analyze` are
local.

Images carry by what their `seine.kind` label says
(`tooling`, `builder`, `imager`, `transport`, `source`). `rootfs` does
not — the root filesystem is what `mmdebstrap` made of the archive on
the day, stale as soon as the archive moves, and every image standing on
it remains current anyway because its label carries the base's
`seine.inputs` digest. `--with-image-rootfs` overrides that. Unlabelled
images are rebuilt on sight, base images (`docker.io/...`) travel
unconditionally.

What travels omits the derived
indices (`Packages`, `Packages.gz`, `Sources`, `Sources.gz`, `Release`,
`Release.gpg`, `InRelease`, `.packages.db`), `apt` locks, `partial`,
`sbuild` `.build` logs and their `*.build` symlink, and `*.lock`.

## How cache objects are keyed

The filesystem names an object by path; the **index** names it by
`(kind, key)`. The index is advisory — what
decides a build is still the stamp, the `<release>-<arch>.inputs` file,
or the `seine.inputs` label. A missing or corrupt index costs a report,
never a wrong build.

### Index kinds

| kind | constant | key shape | who writes it |
|------|----------|-----------|---------------|
| `chroot` | `CHROOT` | `<release>-<arch>` e.g. `bookworm-amd64` | `SbuildChroot` — `hit` on reuse, `made` on creation |
| `package` | `PACKAGE` | `<release>/<arch>/<source>` e.g. `bookworm/amd64/busybox` — one entry per architecture's build; `scope: both` is two entries | package builder — `hit` on reuse, `made` on creation |
| `image` | `IMAGE` | podman name e.g. `bootstrap/debian/trixie/all`, `builder/debian/trixie` | bootstrap builder — `hit` on reuse, `made` on creation |
| `downloads` | `DOWNLOADS` | `<release>` e.g. `bookworm` | image assembly (hitting the archive cache) |
| `vendor` | `VENDOR` | `<suite>_<source>_<name>_<arch>_<version>` e.g. `bookworm_libssl3_libssl3_amd64_3.0.11-1` (`source` literal is `"source"` for source artifacts, binary name otherwise; arch is `"-"` for source) | vendor fetch — `made` on fetch, `hit` for already-fetched |

`downloads` is deliberately coarse — which `.deb` `apt` took is decided
inside the container, so the honest unit is the release. `VENDOR` is the
opposite — one entry per artifact so a superseded version ages out on its
own via `seine cache clear --older-than … vendor`.

### Filesystem layout behind a key

* **downloads** `downloads/<suite>/` plus `downloads/<suite>/lists/` (host-persisted `apt` lists reused by the vendor resolver).
* **packages** `packages/<release>/` flat: `*.deb`, `*.dsc`, `*.changes`, `*.buildinfo`, hidden `.stamps/<source>_<arch>_<digest>` (the stamp lists the `.deb`s it produced), beside it `.stamps-spec/<stamp>.spec` digest excerpts, plus `Packages`/`Sources`/`Release` and `.packages.db` built from the directory. One repository per release for every arch — an `all` package belongs to none and is not kept per-arch.
* **chroots** `chroots/<release>/<arch>/bookworm-amd64.tar.zst` and `bookworm-amd64.inputs`.
* **vendor** `vendor/<suite>/` flat fetched files, indexed on demand into `deploy/vendor/<suite>/pool/{main,extra}/` / `dists/<suite>/…`.
* **images** `build/containers/` — podman's graph-root, asked via `podman images` and `podman system df`.

### Scoped exports

`Wanted` derives, without building,
what a build of those specs would reach for: `releases`, per-release file
sets derived from each package's stamp, `chroots`
as `(release, chroot_arch)` pairs, and image names. `Wanted.records(kind,key)`
and `Wanted.holds(path)` then filter the tar and the `index.json` it
carries. Release-wide for `downloads`, stamp-named for `packages`, choice
is exact for `chroots`/`images`; base images again travel regardless.

## The index

`index.db` under `SEINE_CACHE_DIR` (or `SEINE_BUILD_DIR/cache`).
SQLite via stdlib `sqlite3`, WAL, `busy_timeout=5000`, `synchronous=NORMAL`.
Migrated from `index.json` — the JSON file rewrote the whole
store under a file lock per `hit`/`made`, which did not scale past tens of
entries. `index.db` is a different file
from `index.json`, so there is nothing to migrate — a bad file is dropped
and recreated.

Schema:

```sql
CREATE TABLE cache_entries (
  kind TEXT NOT NULL, key TEXT NOT NULL,
  made INTEGER, used INTEGER, uses INTEGER,
  metadata TEXT,
  PRIMARY KEY (kind, key)
) STRICT;
CREATE INDEX idx_used ON cache_entries(used);
CREATE INDEX idx_kind ON cache_entries(kind);
```

Fixed fields:

* `made` — when last made (unix epoch, `int`).
* `used` — when last used (unix epoch).
* `uses` — how many times reused since made; `made` resets to 0.
* `metadata` — `TEXT` holding a JSON object (see below).

`entries(present=None)` returns `(kind, key, entry)` dicts oldest-`used`
first, merging `metadata` JSON keys into the entry dict. `stripped()` is
what an export carries — only `{"made": …}` per key, without `used`/`uses`;
the importer resets `used=now`, `uses=0` preserving `made` (`merge()`).
`forget(kind, key)` deletes by kind or key, used by `clear`/`stale`/eviction.

`hit(kind, key, metadata=None)` vs `made(kind, key, metadata=None)`:
`hit` bumps `uses`, keeps `made`; `made` replaces `made`
with `now` and resets `uses` to 0. Both set `used=now`. Counted alongside
so summary and record cannot disagree; `summary()`
prints `reused: …; made: …` at the end of a build.

`get(kind, key)` returns the same merged dict as `entries()` would for a
single `(kind, key)` or `None` if absent/corrupt — advisory, never raises.

`patch(kind, key, patch)` merges `patch` dict into the existing metadata
and bumps `used` (via `hit`), preserving other metadata keys. Unlike
`hit(..., metadata=...)` which replaces `metadata` wholesale, `patch` is for
incremental updates (e.g. `vendor`’s `has_gocode` flag). If no entry exists
it creates one.

The store is advisory and concurrent-build friendly: `BEGIN IMMEDIATE`,
WAL, and on error / lock contention
a write is silently dropped — a worse report, never a worse build. A corrupt
DB is dropped and recreated.

Compatibility shims re-express the old
`{kind: {key: entry}}` JSON shape atop SQLite so the `aged()` test helper
and any code poking the old internals continue to work;
they are not public API.

## Metadata

Every entry can carry arbitrary extra data beyond `made`/`used`/`uses`
through the `metadata` column.

### Writing

```python
from seine.cache_index import Index, PACKAGE

Index().hit(PACKAGE, "bookworm/amd64/linux", metadata={"revision": "mod1", "pinned": True})
Index().made(PACKAGE, "bookworm/amd64/linux", metadata={"digest": "b41c…", "spec": "…"})
```

* `metadata` should be a JSON-serialisable `dict`. A non-dict is stored
  as `{"metadata": value}` on read.
* Keys `made`, `used`, `uses` are dropped from `metadata` before storing
  — they are real columns and would be unreadable back
  (column wins on read) if stored and silently lost.
* On `INSERT` the dict is serialised into `metadata TEXT`. On
  `UPDATE`, if `metadata is not None` it replaces the previous JSON
  wholesale; if `metadata is None` the previous JSON is kept.
  Use `patch(kind, key, {"k": v})` to merge incrementally instead of
  replacing — it reads the existing JSON, merges `patch`, and `hit`s.
* Serialization errors fall back to keeping the
  old JSON or dropping the new one; storage errors fall back to a
  transient in-memory entry, never a raised build error.

### Reading

`entries()` decodes `metadata` JSON and merges dict keys into
the returned `entry` dict, skipping any key already present as a column.
`get(kind, key)` is the same but for a single `(kind, key)` — `None` if
absent/corrupt:

```python
for kind, key, entry in Index().entries():
    entry["made"]   # int
    entry["used"]   # int
    entry["uses"]   # int
    entry.get("revision")  # from metadata, if stored

entry = Index().get(PACKAGE, "bookworm/amd64/linux")
entry.get("revision")  # same merged view, or None
```

Non-dict metadata appears as `entry["metadata"]`. Bad JSON is ignored.

### What travels

* **Export** carries
  only `made`. An extra `metadata` key does not ride the `index.json`
  member inside the tar — how often someone else reached
  for a chroot says nothing about the receiving machine, and historical
  locality matters more than foreign history. `Wanted` scopes it
  with the same filter as the objects themselves.
* **Import** takes `made` and any extra keys beyond
  `made`/`used`/`uses` in the carried dict as new metadata. Existing
  metadata is preserved and merged — carried extra keys update / add, not
  wipe, the local dict; `used` is reset to `now`, `uses` to the local
  count, and `made` to the carried value (or `now` if missing).

So metadata is **local by default** — written and read on one machine,
not replicated. If future cross-machine metadata is needed, `stripped()`
and `merge()` are the extension points (add the JSON to `stripped()` and
let `merge()` keep it).

### Users of the mechanism

`vendor` writes `has_gocode` per binary artifact via
`Index().made(VENDOR, key, metadata={"has_gocode": bool})` on fetch and
`Index().patch(VENDOR, key, {"has_gocode": bool})` / `Index().get()` on
reuse/index — advisory, with `dpkg-deb -c` fallback if the index is wiped.
Otherwise the column and the `hit`/`made`/`patch`/`get` path exist as
infrastructure and as an extension point. The design lets
future users attach per-entry data without schema changes:

* a package entry could store its stamp excerpt, resolved
  `version`, or signing fingerprint so `seine cache info
  --entries-matching` or `seine plan` can answer without re-reading the
  repository;
* a vendor artifact could store `files` or `component` so eviction and
  reporting avoid re-listing the directory;
* an image could store `inputs` digest in the index as well as on the
  label, or a chroot its `inputs` digest, for faster staleness checks.

Any such user follows the contract above: pass `metadata=dict` to
`hit`/`made`, read it back from `entries()` entry dict, never use
`made`/`used`/`uses` as metadata keys, and expect it not to be exported
unless `stripped()` is extended.

## Reporting, eviction and transport

* **Reporting**: `seine cache info` sizes each cache; `--entries` lists filtered
  by cache name and sorts by `used`; `--entries-matching`
  regex-filters `key` and, for `PACKAGE`, appends the
  on-disk stamp name and its digest excerpt.
* **Build summary**: every `hit`/`made` increments the per-kind counter, `summary()`
  is the `reused: …; made: …` line at the end of a build, `say()` is the
  `cache: … reused, made …` lines under `--verbose`.
* **Stale sweep**: `clear --older-than 30d` asks the
  index for `used < cutoff` and evicts per entry, then
  forgets it. A cache with no record is left alone — a guess about
  `mtime` is not made. Eviction knows how to remove each kind (image removal, directory removal,
  flat file(s) for vendor, stamp + named `.deb`s for package).
* **Whole-cache clear**: `clear [names]` under a non-blocking storage lock — refuses with *"a build is running"* instead of queuing, removes per cache best-effort (a `partial` dir owned by the container's user may be undeletable — the rest still goes), then forgets the kind, exit 1 if anything was left with a hint about `podman unshare rm -rf`.
* **Export / import**: `export [file] [caches]` writes a tar with one
  top-level member per cache plus `index.json` for the
  record and `images/images.tar.gz` via `podman save | gzip -1`.
  Derived indices, locks, and `partial` are skipped.
  Import validates every tar member
  fails on devices/absolute paths/unknown caches, extracts,
  prunes superseded stamps and unreachable
  `.deb`s (`--force` for orphans), removes stale indices, merges the index.
  `Wanted` scopes both sides without fetching or building.
