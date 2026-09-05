## Specification files

A system specification may be written in one or several YAML files comprised
of the following sections:

 * distribution
 * packages
 * playbook
 * image
 * test

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
file requiring them. See [docs/merging.md](merging.md) for how two files
touching the same section combine.

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
 * architecture: one of `amd64`, `arm64` or `armhf` -- the one this
   particular `seine build`/`seine vendor` run targets
 * architectures: every architecture a `vendor:` section may vendor for,
   beside `architecture` itself (see [vendor](#vendor)); optional, and
   nothing else about a run -- `architecture` alone still decides what a
   real build produces. Unlike every other setting here, additive across
   composed files rather than last-loaded-wins (docs/merging.md): a
   per-architecture fragment (`examples/common/amd64.yaml`,
   `.../arm64.yaml`) names its own one, and a specification composing
   both ends up with both
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
downloaded is in the cache under `./build/downloads` (see
[Environment variables](environment.md)), and a snapshot serves the same
bytes for a timestamp for ever, so nothing there can go stale.

When multiple YAML files are parsed, the last parsed value will be used.

#### apt-pull-mode: offline

```
apt-pull-mode: offline
```

Switches every feed from its network `uri` to the local repository
[`seine vendor`](#vendor) built for its suite, so that reaching for a
package touches no network at all: the target's own `apt` tasks in its
running container, `apt://` fetching a package's own source, a buildd
chroot resolving what it build-depends on, and the small apt-get every
build's own host bootstrap -- and, for a `baseline:` playbook, its
transport bootstrap -- runs to install its own tooling (`arch-test`,
`mmdebstrap`, `qemu-user-static`, `python3-apt`...) are covered alike.
`packages: false` is not a thing to say here: `apt-pull-mode` is a
distribution-wide toggle, the same scope `distribution`'s other
settings have, not a per-feed one.

The host and transport bootstraps read the repository at `podman build`
time rather than mounting it into an already-running container the way
everything else above does, so unlike those, an image built from it is
cached by its own Dockerfile text alone -- reading whatever the
repository held the moment it was built, not whatever it holds now. The
repository's own resolved manifest digest is folded into that text for
exactly this reason, so a vendor refresh that changes what apt would
install rebuilds the cached image rather than leaving it stale. Neither
image ever ships or is thrown away the way a buildd chroot is: once
built this way it stays offline-configured on the machine that built
it, which is fine -- it is a build tool, never the device's own root
file-system, and is rebuilt in place the next time its own digest moves.
Asking for this before any `seine vendor` has ever built a repository
for the release fails outright, in plain language, rather than partway
through an `apt-get update` with nothing to read.

It does not reach the target bootstrap's own minimal package set: what
mirrors that is a full-suite snapshot (see
[Building from a snapshot](#building-from-a-snapshot) above), not
`vendor:`, which deliberately excludes anything a buildd chroot already
provides by itself -- exactly what `vendor:`'s own build-dependency
closure covers. Nor does it reach `seine vendor`'s own resolve and fetch
containers: those are what fills this repository in the first place, and
going offline there would have them read one with nothing in it yet --
they bootstrap from their own, permanently-online image instead (kept
distinct from the host bootstrap above precisely so that populating a
repository for the first time never has to wait on one already
existing).

Offline for the length of that alone: what ships in the image is
rewritten back to the real feeds before it is exported, the same
`sources.list.d` entry an online build would have left, so the device it
ends up on can still reach the network on its own -- a path that only
ever existed on the machine that built it would otherwise break the
first `apt-get update` the device itself runs. The sbuild builder image
and buildd chroot get no such rewrite: they are thrown away at the end of
every build, never exported.

A build resolves this itself when a `vendor:` section feeds a suite
covered here -- see [`vendor`](#vendor) above -- so a plain `seine build`
is enough. It is only a bare, `vendor:`-less feed reaching for this
mode, or `seine vendor`'s own resolve and fetch containers (never
themselves offline, whatever the specification says -- see above), that
still depend on a repository having been built some other way first.

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

`defaults` holds package entries only. Playbooks and tests already merge
by name (see [`playbook`](#playbook)/[`test`](#test)), and the other
sections are merged by key.

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
| cross             | no       | Cross-compile (see [Cross-compiling](building.md#cross-compiling)) |
| extends           | no       | Settings for a kind of package (see [Bring your own modules](kernels.md#bring-your-own-modules)) |
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
by setting, as partitions with the same label are. A `requires` is
loaded after the file that asked for it, and a setting that file already
gave wins, so a fragment can carry the parts that do not change while
the specification including it settles the rest. A file given directly
on the command line, or side-loaded in the TUI, is not reached for by
another and so is not a fragment: the next one amends the settings
already loaded, field by field, instead of losing to them (see
[docs/merging.md](merging.md)).

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
local apt repository under `./build/cache/packages/` (see [Environment variables](environment.md)): the playbooks
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
`busybox 1:1.37.0-6` is installed as `1:1.37.0-6+mod1`. A kernel using
[`derived-flavours`](kernels.md#rebuilding-the-kernel) is the one exception: its
default is the name(s) actually derived instead, since two files naming
`derived-flavours` are usually two architectures rebuilding the same
`linux` under names of their own.

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

### vendor

`packages:` rebuilds what a specification changes; `vendor:` is for
everything else it depends on -- directly or by way of a build
dependency -- that seine never touches but a rebuild years from now
still needs, once the feed it came from may no longer be there.
`seine vendor` resolves each entry's full build-dependency closure and
fetches it, binaries and sources both, into a signed apt repository of
its own:

```
vendor:
    - name: openssl
    - name: busybox
      suite: [bookworm, trixie]
    - name: libfoo
      suite: bookworm
      arch: [armhf]
      version: ">=1.2"
```

A flat list, like `packages:`, mergeable across the files a
specification is composed of and merged by name the same way: a file
naming `libfoo` again adds to what an earlier one already said about it
rather than describing a second `libfoo`.

Entries are keyed by the **source** package name; `seine vendor` works
out which binaries that source produces (and what those in turn
build-depend on) itself -- keying this by a binary package name instead
would leave that resolution to be spelled out by hand for every source.

`suite:`/`arch:` each take one name or a list of them; missing, an entry
applies to every suite the specification's `vendor:` section asks for (or
its own release, when nothing asks for another) and every architecture
asked of it -- the specification's own `distribution: architecture:`,
plus `distribution: architectures:` (see [distribution](#distribution)),
plus whatever any other entry's own `arch:` names. A named suite has to
be one of `distribution: feeds:`'s own -- `seine vendor` reads a suite's
packages from the same feed a build would.

`version:` pins what is vendored, either exactly (`"1.2-3"`) or with a
comparison (`">=1.2"`, `"<=1.2"`, `"=1.2"`, `">>1.2"`, `"<<1.2"`), the
same operators `apt_preferences(5)` uses. Unset, whatever the suite's own
feed currently resolves to is taken.

A build dependency already provided by the buildd chroot a real rebuild
would use is left out on its own -- vendoring it would only repeat what
that chroot's own reproducibility already covers. `vendor-exclude:`, a
flat list of source package names beside `vendor:`, is the escape hatch
for what the automatic dedup cannot know about: a build-profile-only
dependency (documentation, tests) nobody wants vendored.

```
vendor-exclude:
    - texlive-latex-base
```

`distribution: architectures:` vendors for these architectures too, on
top of `distribution: architecture:` -- what an unqualified entry (no
`arch:` of its own) then covers. The only other way to reach a second
architecture would be tagging some unrelated entry's own `arch:` with
it, a side effect of a per-package field standing in for what this says
directly:

```
distribution:
    architecture: amd64
    architectures:
        - amd64
        - arm64
```

`seine vendor --architecture NAME` narrows a run to fetching one (or a
few) of the architectures a `vendor:` section -- `arch:`, entry by
entry, `distribution: architectures:`, or `distribution: architecture:`
itself -- together ask for; unlike `--suite`, this only narrows what
gets *fetched*, not resolved, so a suite's frozen manifest stays
complete for every one of them regardless of which `--architecture` any
one run named.

Resolving is frozen: once `seine vendor` has decided a source's version,
an ordinary run keeps it rather than asking the suite's feed again --
what apt would resolve today is not necessarily what it resolved when
the version was first pinned, and a specification's vendor should not
drift underneath it between two runs that changed nothing. `seine vendor
--refresh` asks for a new resolve; `--refresh=NAME` scopes that to one
source package, keeping every other one exactly as it was.

A specification file, `foo.yaml`, pairs with a committed lock file,
`foo.lock.yaml`, if one sits beside it -- loaded automatically, no field
asks for it. A suite the lock names is trusted outright and never
resolved at all, unless the specification's own `vendor:` section has
changed since the lock was last written, in which case `seine vendor`
refuses rather than silently drifting back to whatever the suite's feed
resolves to today -- the entire point of a committed lock is that two
checkouts of the same specification vendor the exact same thing,
regardless of what has changed in the archive between them. A lock
file's own `vendor:` is a resolved manifest -- suite by suite, source by
source, with the sha256 of every file fetched for it -- rather than a
list of entries, and is never written by hand. It carries only what
must be true of the outside world (versions, binaries, files and their
hashes), never seine's own bookkeeping about what it decided to do with
them (which source counts as directly asked for, which build
dependency pulled another source in) -- that stays in the machine-local
cache manifest instead, which `seine vendor` keeps in full:

```
seine vendor --refresh foo.yaml
```

(re)writes `foo.lock.yaml`, whether or not one existed before --
bootstrapping a new lock is just the first `--refresh`, same command as
updating an existing one; `git add foo.lock.yaml` if it should be
tracked. Both `--refresh` and `--check` below need exactly one
specification file, to know which lock they are about.

`seine vendor --check foo.yaml` resolves fresh and reports whether it
still matches `foo.lock.yaml`'s own `vendor:` entries, without writing
anything either way -- non-zero on drift. This is what a scheduled CI
job would run to catch the archive moving under an unchanged version,
which the lock's own digest (everything `manifest_digest()` covers:
entries, excludes, feeds, build-profiles/options) cannot see by itself.

Debian's own archive keeps only the current version of a package, not
its history, so a lock pinning an older one can outlive the live feed's
own copy of it. `--refresh` checks every file it fetches against
[snapshot.debian.org](https://snapshot.debian.org/)'s own machine
API, and, on a match, records its own sha1 for the file (`vendor:`'s
own `snapshot`/`binary_snapshot`, beside `file_hashes`/`binary_hashes`
-- the sha1 alone, not the download URL built from it, since the
filename is already known and the constant
`https://snapshot.debian.org/file/` prefix repeated per file would
otherwise be a fifth of a real generated lock's own size) -- nothing
is recorded, and `--refresh` still succeeds, when snapshot.debian.org
has never indexed that exact name/version/checksum at all. An
ordinary `seine vendor` (no `--refresh`) never queries
snapshot.debian.org itself: it only reads the sha1 already on the
lock, and, when one is there, rebuilds the download URL and fetches
straight from it -- no apt, no container, since it is a plain,
permanent HTTPS download -- verifying the bytes against the lock's own
hash and refusing them outright on any mismatch, never falling back to
the live feed. Nothing changes for a source still served by the live
suite: the recorded sha1 only matters once apt itself can no longer
produce the pinned version.

Signed the same way `packages:`'s own repository is (see
[Signing](#signing) above), with its own, independent key --
`--vendor-sign-key`/`SEINE_VENDOR_SIGN_KEY` -- since a package `vendor:`
pulled in was never built or audited by seine, unlike a `packages:`
rebuild.

Kept under its own `vendor` cache, one repository per suite; `seine
cache clear vendor`/`seine cache clear --older-than ... vendor` apply to
it exactly as they do to any other cache. See `seine vendor --help` for
the rest of its options.

Building a repository is one thing; reading from it instead of the
network is another -- see [`apt-pull-mode: offline`](#apt-pull-mode-offline)
below. `seine build` resolves/fetches/indexes a `vendor:` section itself,
ahead of `packages:`, exactly when that section feeds a suite
`apt-pull-mode: offline` needs -- a `vendor:` that feeds none is still
left for a plain `seine vendor` to build whenever someone wants it,
since nothing in the build would read it either way. Already-frozen
entries cost nothing extra: the same resolve-is-frozen rule above
applies, so a rerun that changed nothing just re-signs what an earlier
resolve found.

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

Composed across `requires` the same way `test` entries are: an entry
named the same as one already loaded is merged into it -- `tasks` add
up (an exact repeat, from a fragment reached twice via two `requires`
paths, is dropped; new tasks append in order) -- rather than a second
entry with the same name running alongside the first.

Frequently used tasks include:
 * `apt`
 * `debconf`

#### Ansible Galaxy collections

`ansible-core` alone covers `apt`, `debconf`, `user` and the rest of the
tasks above. Two Galaxy collections are worth installing alongside
`containers.podman` (the one seine itself needs, to connect into the
container -- see [Installation](getting-started.md#installation)) because
they cover ground `ansible-core` does not and fit how an image is
assembled:

 * [`ansible.posix`](https://galaxy.ansible.com/ui/repo/published/ansible/posix/)
   -- `sysctl` for a kernel parameter an appliance wants tuned,
   `seboolean` alongside the SELinux policy example above, `mount` for
   an `fstab` entry a playbook needs to add.
 * [`community.general`](https://galaxy.ansible.com/ui/repo/published/community/general/)
   -- the long tail of system-configuration modules `ansible-core` no
   longer carries, `timezone` among them.

```
ansible-galaxy collection install ansible.posix community.general
```

`sysctl` writing into the image is not the same as it taking effect on
the machine assembling the image -- the build container has no write
access to the host's `/proc/sys`, so `sysctl_set: false` and
`reload: false` keep the task to writing the config file the booted
target will apply:

```
playbook:
    - name: tune kernel parameters
      tasks:
        - name: lower swappiness for an appliance workload
          ansible.posix.sysctl:
              name: vm.swappiness
              value: "10"
              sysctl_set: false
              reload: false
```

Both collections are exercised for real, not just documented: see
`examples/common/conf-sysctl.yaml` and `examples/common/conf-timezone.yaml`,
built by every image in
[the full plan](getting-started.md#the-full-plan).

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

#### Writing your own module

A `library/` directory beside a specification file is found automatically
and handed to `ansible-playbook`, the same way a kconfig fragment is found
relative to the file that lists it -- no setting names it. A task that
needs no third-party collection, no explicit format knowledge, just a
clean interface, can be a module of its own instead of a handful of
`lineinfile` tasks a reader has to reverse-engineer the intent of:

```
playbook:
    - name: harden ssh
      tasks:
          - name: disable root login over ssh
            sshd_config:
                name: PermitRootLogin
                value: "no"
```

`examples/common/library/sshd_config.py` is a complete, working example: a
module that sets, updates or removes a single `sshd_config` directive,
first-active-line-wins the same way `sshd` itself resolves a duplicate. It
writes to a drop-in under `sshd_config.d` rather than the file Debian's own
package shipped, refusing to run if the shipped file was ever rewritten to
drop the `Include` that reads drop-ins in the first place -- silently doing
nothing is worse than failing loudly for a setting like this one.

A comma-list directive -- `Ciphers`, `MACs`, `KexAlgorithms` and the rest
sshd itself negotiates a default for -- takes `algorithm` instead of
`value`, to turn one entry on or off without a task having to spell out
everything else sshd would otherwise have picked:

```
playbook:
    - name: only turn off one cipher
      tasks:
          - name: drop a weak cipher
            sshd_config:
                name: Ciphers
                algorithm: 3des-cbc
                state: absent
```

Several such tasks, from several playbook fragments, can each turn one
algorithm on or off without overwriting each other's change -- each starts
from `sshd -T`'s own resolved default rather than from the previous task's
raw `value`.

Ansible ships a custom module's code to wherever it runs, same as any
other module -- nothing beyond the module file itself is needed. It is
exercised the same way as the collections above: see
`examples/common/sshd.yaml`, and the module's own
`examples/common/library/test_sshd_config.py` self-check.

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
 * `secure-boot`
 * `size`
 * `table`
 * `volumes`

An `image` shall have at least one partition defined and an output `filename`
specified. The `size` of the disk `image` may be omitted and it will then be
estimated (as the sum of the various partition sizes plus some overhead). The
partition `table` may either be `gpt` or `msdos`.

#### secure-boot

A disk's signing identity, used to sign a UKI the imager finds and
anchors for dm-verity (see [Protecting a read-only partition with
dm-verity](#protecting-a-read-only-partition-with-dm-verity) below) --
a property of the image as a whole, not of any one partition, since one
key signs whichever UKI(s) are on the disk regardless of which mount(s)
they anchor.

| Attribute    | Required | Description                              |
| ------------ |:--------:| ----------------------------------------- |
| private-key  | yes      | Key to sign the anchored UKI with        |
| public-cert  | yes      | Certificate to sign the anchored UKI with |

Both are paths, resolved relative to the current working directory a
build is run from (like `multiconfig: <group>: files:`, not like
`patches:`). Omit `secure-boot` entirely to leave an anchored UKI
unsigned -- correct for a board that has Secure Boot turned off, which
any board using this today has to (nothing here enrolls a certificate
into real firmware).

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

| Attribute   | Required | Description                              |
| ----------- |:--------:| ----------------------------------------- |
| label       | yes      | Name of the partition                    |
| flags       | no       | Partition flags (see below)              |
| group       | no       | Name of the LVM group to join            |
| size        | no       | Size of the partition                    |
| type        | no       | File-system type (e.g. `ext4`)           |
| compression | no       | Compression, for a read-only `type`      |
| identify    | no       | How a read-only `type` is found at boot  |
| verity      | no       | Protect a read-only `type` with dm-verity |
| verity-for  | no       | Label of the partition a `verity-hash` protects |
| where       | yes*     | Where to mount the partition file-system |

(*) Required unless the partition is a LVM physical volume or has
    `type: verity-hash`

`type` may also be `squashfs` or `erofs`, in which case the partition is built
read-only and needs a `gpt` partition `table` (see [image](#image) above): the
imager identifies it in `/etc/fstab` by `PARTLABEL=<label>`, which does not
exist on a `msdos` table. `identify: partuuid` uses `PARTUUID=` instead.
`compression` is passed straight through to the underlying tool (`-comp` for
`mksquashfs`, `-z` for `mkfs.erofs`) with no validation or default of its own
-- when unset, each tool's own default applies. The imager builds these with
its own copy of `mksquashfs`/`mkfs.erofs`, transiently -- the specification
does not need `squashfs-tools`/`erofs-utils` installed, and nothing from
building them is left in the produced image.

##### Protecting a read-only partition with dm-verity

`verity: true` on a `/` or `/usr` partition (the only mountpoints the
[Discoverable Partitions Specification](https://uapi-group.org/specifications/specs/discoverable_partitions_specification/)
defines an auto-discovered Verity partition type for) needs a second
partition of `type: verity-hash`, naming the protected partition back with
`verity-for: <label>`. A `verity-hash` partition is never mounted (no
`where`) and has no filesystem of its own -- it holds a raw dm-verity hash
tree, built by the imager with its own transient copy of `veritysetup`
after the protected partition's read-only image is built. Its `size` must
be given explicitly (there's no way to estimate a hash tree's size before
building it); the build fails if it turns out too small.

The imager sets both partitions' own GPT GUIDs to the built root hash's two
halves (the same self-describing scheme `systemd-repart` uses, see
`repart.d(5)`), so `systemd-gpt-auto-generator`/`systemd-veritysetup-generator`
find and activate the pair with no kernel command line change. A protected
partition gets no `/etc/fstab` entry: the generators mount it, not fstab
(an fstab entry would mount the raw partition straight past dm-verity).

Pairing the two partitions by GUID is not enough on its own: without an
externally-supplied root hash, `systemd-gpt-auto-generator` refuses to
activate a Verity pair at all -- it does not fall back to plain-mounting
the data partition, so a `where: /usr` partition with `verity: true` and
no anchor **does not boot**. The imager closes this by finding any UKI
already installed under `EFI/Linux/` on the same disk (built by an
`extends: uki:` package elsewhere in the specification -- see
[Bring your own modules](kernels.md#bring-your-own-modules)) and
rebuilding it with `usrhash=`/`roothash=` appended to its cmdline, once
the real root hash is known. This happens unconditionally for any
`verity: true` mount -- `usrhash=` is what makes `/usr` mountable at
all, signed or not.

An `image: secure-boot:` (see [secure-boot](#secure-boot) above)
additionally signs the rebuilt UKI (`sbsign`), so Secure Boot's own
signature verification transitively authenticates the anchored cmdline:
a forged `/usr` (with its own re-derived GUIDs) no longer matches the
pinned hash, and the generator refuses it instead of silently
activating it. Currently only `where: /usr` is supported (`/` verity
roots have no anchor mechanism yet), and only a UKI built with `extends:
uki: tool: ukify` (not `efibootguard`, which has no equivalent
PE-section layout to rebuild from).

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

| Attribute   | Required | Description                              |
| ----------- |:--------:| ----------------------------------------- |
| label       | yes      | Name of the volume                       |
| group       | yes      | Name of the LVM group to join            |
| size        | no       | Size of the partition                    |
| type        | no       | File-system type (e.g. `ext4`)           |
| compression | no       | Compression, for a read-only `type`      |
| where       | yes      | Where to mount the volume file-system    |

`type: squashfs`/`erofs` volumes are not restricted to a `gpt` table: the
imager identifies an LVM logical volume by its `/dev/<group>/<label>` device
path regardless of file-system type, so `identify`/`PARTLABEL`/`PARTUUID`
don't apply here. Unlike a plain partition, an LVM volume's `size` is not
grown to fit a built `squashfs`/`erofs` image -- it must already be large
enough, or the build fails.

#### Example: a read-only `/usr`

```yaml
image:
  filename: example.img
  table: gpt
  partitions:
    - label: boot
      where: /boot
      type: ext4
      size: 256MiB
    - label: root
      where: /
      type: ext4
    - label: usr
      where: /usr
      type: squashfs        # or: erofs
      compression: zstd     # optional, passed straight through
    - label: usrlocal
      where: /usr/local      # nested under /usr, its own writable partition
      type: ext4
      size: 128MiB
```

#### Example: a dm-verity-protected `/usr`

```yaml
image:
  filename: example.img
  table: gpt
  secure-boot:            # optional -- signs the anchored UKI; omit to stay unsigned
    private-key: keys/db.key
    public-cert: keys/db.crt
  partitions:
    - label: root
      where: /
      type: ext4
    - label: usr
      where: /usr
      type: erofs
      verity: true
    - label: usr-verity
      type: verity-hash
      verity-for: usr
      size: 64MiB          # must be given -- unknown until the hash tree is built
```

### test

A specification carries its own tests the same way it carries its
`packages`/`playbook`/`image`: `test` is a list of entries, composed
across `requires` -- two files each naming their own suite is the
ordinary case, not a conflict. An entry named the same as one already
loaded is merged into it instead of duplicated, the same way `packages`
are merged by name (see [`packages`](#packages)): cases inside its own
`tests` are merged the same way, by their own `name`. Each entry runs
against a real target over [mtda](https://github.com/siemens/mtda),
with `if`/`for`/`while`/`try`, reusable named keywords and assertions
built on [Robot Framework](https://robotframework.org/). See
[docs/testing.md](testing.md) for the full step grammar, the keywords
available, and `seine test`/`seine tui`'s own `/test`.

