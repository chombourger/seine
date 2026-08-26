# Contributing to seine

Contributions of every kind are welcome: bug fixes, features, tests,
documentation, and reviews of other people's changes.

## Submitting changes

seine is developed on [GitHub](https://github.com/chombourger/seine).
Fork it, work on a branch of your own, and open a pull request:

```
git checkout -b my-feature-branch
git commit -s
git push origin my-feature-branch
```

Describe what the change is for in the pull request, and say how you
tested it.

## Commit messages

Commit messages follow the [Conventional Commits][] specification:

```text
<type>[(<scope>)][!]: <summary>

[body]

[footer(s)]
Signed-off-by: Your Name <you@example.com>
```

### Types

One of `build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`,
`revert`, `style` or `test`, lowercase.

### Scopes

The scope is a noun naming the part of seine that changed, and is
optional -- omit it rather than invent one for a change that spans the
whole project. The ones in use are:

| Scope          | What it covers                                        |
| -------------- | ----------------------------------------------------- |
| `ai`           | The AI chat: its tools, `system_prompt.txt`, `seine tui`'s `/settings`-configured model |
| `analyze`      | Recorded build timings, and `seine analyze`           |
| `ansible`      | Running playbooks against the target container        |
| `bootstrap`    | The host and target bootstrap images                  |
| `build`        | Driving a build: the steps it is made of, `seine build` |
| `cache`        | What seine keeps between builds, and `seine cache`    |
| `deps`         | seine's own dependencies and packaging                |
| `distribution` | The `distribution` section: releases, feeds, mirrors  |
| `examples`     | The specifications under `examples/`                  |
| `image`        | Assembling the root file-system and the disk image    |
| `imager`       | The libguestfs appliance that writes the disk image   |
| `issues`       | CVE scanning against an SBOM: `seine issues`, `/issues`, `sbom2cve_program` |
| `kernel`       | What `extends: kernel` means: grafts, patches, ABIs   |
| `module`       | What `extends: module` means: out-of-tree modules     |
| `packages`     | The `packages` section and the rebuilds it asks for   |
| `readme`       | The README                                            |
| `sbom`         | Software Bill of Materials generation                 |
| `sbuild`       | The buildd chroot packages are rebuilt in             |
| `spec`         | The specification files, and how they are loaded      |
| `target`       | Driving real hardware through mtda: `/target`, `seine.tui.target` |
| `testing`      | Test suites and their execution: `seine test`, `seine.testing` |
| `tui`          | The terminal UI, `seine tui`                          |
| `utils`        | Shared helpers: the container engine, apt sources     |
| `vendor`       | The `vendor` section and `seine vendor`               |

### Summary

Use the imperative present tense -- "add", not "added" or "adds" -- do
not capitalise the first letter, and do not end with a full stop. Keep
the whole header line under 72 characters.

### Body

The body says *why* the change was made, and what the behaviour was
before it. *How* is what the diff is for. It is optional only when the
summary leaves nothing unexplained. Wrap it at 72 characters.

### Breaking changes

Mark them both ways: a `!` after the type or scope, and a
`BREAKING CHANGE:` footer saying what stops working.

### Sign-off

Every commit carries a `Signed-off-by:` line, which `git commit -s`
adds for you. It certifies that you wrote the change, or otherwise have
the right to submit it under the project's licence -- see the
[Developer Certificate of Origin](https://developercertificate.org/).

## Tests

The tests live under `tests/spec/` and are run with avocado -- see
"Running the tests" in [docs/getting-started.md](docs/getting-started.md) for how to install it.
Run them before opening a pull request:

```
avocado run tests/spec/*.py
```

A change in behaviour comes with a test for it. Tests that need podman
or kvm are tagged `container`, so that the rest stay runnable anywhere;
tag yours the same way if it builds something, and have it cancel
itself where what it needs is missing. `tests/spec/tui.py` (the TUI,
`seine/tui/`) needs `textual` (the `tui` extra) the same way and is
tagged `tui`, cancelling itself where that is missing.

## Coding style

Follow the style of the code around what you are changing. Comments are
for what the code cannot say for itself -- why a thing is done this way,
what breaks otherwise -- rather than a retelling of the lines below
them.

New source files start with the same two-line header the existing ones
have:

```
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0
```

## Licence

seine is licensed under the Apache License 2.0. Contributions are
accepted under the same licence.

[Conventional Commits]: https://www.conventionalcommits.org/
