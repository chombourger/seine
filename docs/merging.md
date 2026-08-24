# Merging specification fragments

A specification is usually more than one file: `requires` pulls in
fragments so an architecture, a release, a board and a concern (test
accounts, sshd hardening, a kernel) are each written once and shared
rather than copied into every image that needs them (see
[docs/specification.md](specification.md)). Loading more than one file
that touches the same section -- two files both naming `packages`, both
naming a `test:` entry called `shared login` -- is the ordinary case,
not a conflict, and each section decides for itself what "combine"
means for the data it holds. This is a map of those decisions: read it
before extending a section's own merge rule, or before debugging why a
value came from the file it did.

## Load order

`requires` is read depth-first: a file's own settings are folded into
the specification *before* its `requires` are walked, and each
required fragment is loaded (and merged) in turn, innermost -- the
fragment at the end of the deepest chain -- last. A fragment reached by
two different `requires` paths is not a loop and loads twice, which is
how a shared fragment (`conf-accounts`, required directly by a board
and again by that board's own test file) is meant to be reused. Loading
it twice does not mean the specification ends up with it twice: each
section's own merge rule is what decides that (see below), and the
`test`/`playbook` fix this doc follows on from is exactly a case where
it didn't, for a while.

## Two directions

Every section merges by *some* unit -- a whole file for the ones with
no shape of their own, a named entry for most of the rest -- but they
disagree, on purpose, about which of two files wins a setting both of
them write. There are two rules in use, never a third:

- **The file reaching for a fragment overrides it.** Since a file's own
  settings are recorded before its `requires` are walked, this means
  *first-loaded wins*: `packages`, `playbook`, `test`, and
  `image`'s own `partitions`/`volumes` (matched by `label`) all work
  this way. A board asking for `apt://linux=6.12.101-1` keeps that
  pin no matter what a kernel fragment it requires says about the
  same package; a board's own `boot` test entry's `setup:` stands even
  if a fragment it requires also names an entry called `boot`. This is
  the rule for **things a file asks for** -- the file doing the asking
  outranks anything a fragment it reaches for has to say about the
  same thing.

- **The most specific file wins.** `distribution`, `imager`, and
  `image`'s own scalar settings (`filename`, `table`, `size` --
  everything except `partitions`/`volumes`) go the other way: the
  *last-loaded* value wins, which in practice is whichever fragment
  sits deepest in the `requires` chain a board pulls in. `defaults` (a
  package *description*, not a request to build one) follows the same
  rule, and says so explicitly: "the opposite of `packages`". This is
  the rule for **things describing what is being built**: a general
  architecture file is loaded, then requires nothing further and lets
  the more specific release/board files loaded around it fill in or
  override what it started -- so the file closest to "what board is
  this, actually" gets the last word.

Getting the direction backwards for a given section is the single
easiest way to misjudge what a new merge rule will do -- verify with a
throwaway two-file `build.loads()` (see `tests/spec/merge.py` for the
pattern) rather than assuming either rule from the other.

| Section | Unit | Direction | Notes |
|---|---|---|---|
| `redact` | exact pattern string | additive, deduplicated | order doesn't matter, only presence |
| `distribution` | scalar setting | last-loaded wins | except `feeds`, merged by `suite` (a feed named by two files still ends up last-loaded-wins per field, via `dict.update`) |
| `imager` | scalar setting | last-loaded wins | |
| `defaults` (`packages` only) | source package | last-loaded wins | deliberately the opposite of `packages` |
| `packages` | name, else parsed from `source:` | first-loaded wins; `extends:` merges kind-by-kind, some settings (`derived-flavours`, `kernel.configs`, a module's own kernel list) are additive instead | |
| `playbook` | `name` | first-loaded wins; `tasks:` is one additive, order-preserving list | tasks never merge task-by-task -- see below |
| `test` | `name` | first-loaded wins; `library`/`tags`/`variables` additive; `tests:` cases merged the same way one level down, by their own `name` | `keywords:` and a case's own `steps:` are equality-or-error, not first-wins -- see below |
| `image` (scalars) | scalar setting | last-loaded wins | |
| `image.partitions` / `image.volumes` | `label` | first-loaded wins; `flags` additive (`~flag` removes one a fragment already set) | |

## Respecting the language a section embeds

`packages`/`image`/`distribution` are seine's own declarative data --
merging a setting one file didn't write in from a file that did is
just filling in a gap, and picking a direction is the only real
decision. `playbook` and `test` are different: a `tasks:` entry is
Ansible, and a `steps:`/`keywords:` entry is Robot Framework by way of
`seine.testing.loader`, and both are *ordered, executable* content, not
data. Merging inside them the way a setting merges would change what
actually runs, silently, which is a much bigger deal than filling in a
setting a file left blank. Two rules follow from that, and are worth
keeping in mind before extending merging into another embedded
language:

- **An ordered list of steps is never merged element-by-element.**
  `tasks:` (Ansible) stays one list, appended to and deduplicated only
  where a fragment is reached twice with the exact same task -- never
  merged task-by-task by name, because an Ansible task's position in
  the sequence is part of what it means, and two fragments' own task
  lists interleaving unpredictably would be far harder to reason about
  than two suites simply running one after the other.

- **Content that is code is merged by identity, not by field.** A
  `keywords:` entry and a `tests:` case's own `steps:` are
  equality-or-error: identical content reached twice (the shared-fragment
  case) is tolerated silently, but two different bodies under the same
  name raise rather than one silently overriding the other or the two
  somehow combining. Silently keeping "whichever loaded first" -- the
  rule every plain setting follows -- would hide a real authoring
  mistake behind behavior that only shows up when the wrong one runs on
  real hardware. The equivalent decision for a plain setting instead
  asks "which of the two would apply is not something to guess at
  silently" -- refuse, don't guess.

Extending merge-by-name to a new section, or to a new field inside
`playbook`/`test`, should ask the same question this doc's own history
did: is what's being merged declarative data (safe to merge
first/last-wins, field by field), or is it a step in a sequence /
a definition of behavior in an embedded language (merge only by exact
identity, or not at all, and raise on a real mismatch rather than
picking a winner)?

## Where this lives in code

`BuildCmd.merge()` (`seine/build.py`) is the dispatcher; each section
has its own `_merge_*` method. `_merge_named_list()` and
`_merge_settings()` are the generic "match by name, merge or append"
and "first-loaded-wins-unless-additive" helpers `packages`, `playbook`,
and `test` (and `test`'s own `tests:`/`keywords:` one level down) all
build on; `_merge_package()`/`_merge_extends()` add `packages`'
`extends:` recursion on top. `distribution`/`imager`/`image`'s own
scalars don't go through either helper -- they're a plain
"later file's value replaces" loop, which is the whole of what
last-loaded-wins needs. `tests/spec/merge.py` is the executable
version of the table above: a merge rule that isn't covered by a test
there is one nobody has actually pinned down yet.
