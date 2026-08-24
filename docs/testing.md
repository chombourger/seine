# Testing

`seine test` runs a specification's own tests -- boot a real target,
drive it over the console or HID, check what came back -- against a
target reachable through [mtda](https://github.com/siemens/mtda), the
same one `/target` (see [docs/tui.md](tui.md)) already drives.

A specification carries its tests the same way it carries its
`packages`/`playbook`/`image` -- `test` is an ordinary section (see
[docs/specification.md#test](specification.md#test)), a list of entries
composed across `requires`: an entry named the same as one already
loaded is merged into it rather than duplicated, the same way `packages`
merge by name. It is not a second,
static language living beside a build spec's own: a build spec
deliberately has no `if`/`for` (see
[Variables](specification.md#variables) for why), but a test's whole job
is reacting to what a real, physical target actually does, so it gets
real control flow -- `if`/`for`/`while`/`try`, reusable named keywords,
variables assigned from what a step returned -- borrowed from
[Robot Framework](https://robotframework.org/), a mature, widely used
test-automation engine, rather than seine inventing a second control-flow
language of its own. seine's own part is the YAML shape below, the
keywords that expose seine/mtda actions to it, and the CLI/TUI/AI
plumbing; IF/FOR/WHILE/TRY, variables, tags, setup/teardown and
structured pass/fail/skip results are Robot's, unmodified.

## Running one

```
seine test SPEC.yml
seine test --tags=smoke SPEC.yml OTHER.yml   # only tests tagged 'smoke'
seine test --dry-run SPEC.yml                # resolve every keyword, touch no hardware
```

`SPEC.yml` is loaded exactly the way `seine build` would -- `requires`,
`[[ ]]` variables, several files on one command line -- and the merged
specification's own `test` section runs. Needs the `test` extra (`pip
install seine[test]`, or the `seine-test` package -- `robotframework`
itself, a pure-Python package, unlike `mtda` which is a system package
seine never pip-installs, see [doctor](#doctor)). Exit status is 0 only
if every test that ran passed, 1 if any failed, 2 if `SPEC.yml` has no
`test` section or could not be loaded -- a CI job reads that alone, no
output parsing needed. `--outdir` (default a fresh directory under the
same logs root a build's own logs land under, see
[Environment variables](environment.md)) is where Robot's own
`output.xml` and any captured screen/image artifacts go; point Robot's
own `rebot`/`libdoc` tooling at `output.xml` for a richer `log.html` than
seine's own plain-text summary. `--dry-run` is Robot's own dry run:
every step's keyword is resolved and its arguments checked, but no
keyword body actually runs -- nothing touches real hardware, the way
`seine validate` checks a build spec without building it.

From `seine tui`, `/test [--tags=TAG,...] [SPEC...]` does the same
thing, on the Test screen, through the exact same engine -- omit
`SPEC` to run the active specification's own tests, the same default
`/build` falls back to; typing `/test` again while one is running just
switches to watching it. `/cancel` doesn't reach a test run yet. The AI
chat's own `run-test`/`test-validate` tools ([docs/ai.md](ai.md) covers
the AI chat generally) report into the same Test screen, so a test
started either way is visible either way.

## Writing tests

```yaml
# examples/rebuild-busybox/busybox.yaml
test:
    - name: busybox rebuild
      tags: [smoke]
      setup:
          connect_target: {}
      teardown:
          disconnect_target: {}
      tests:
          - name: banner shows the rebuild marker
            steps:
                - power_cycle: {settle: 2s}
                - log_in: {}
                - console_send: {data: "busybox | head -1\n"}
                - console_wait: {pattern: "rebuilt-by-seine", timeout: 15s, assign: MATCHED}
                - should_be_true: ["${MATCHED}"]
```

`test` is a list; each entry is one suite fragment: `name`, `tags`
(applied to every test the entry contributes), `variables` (seeded
once, plain scalars), `library` (extra Python keyword library modules,
dotted paths, beyond the seine ones below -- always available, nothing
to import), `keywords` (reusable named actions, see below),
`setup`/`teardown` (one keyword call each, exactly like Robot's own
`[Setup]`/`[Teardown]` -- bundle more than one step under a `keywords:`
entry, don't list several here), and `tests` (each with its own
optional `tags`/`setup`/`teardown` and a `steps:` list). An entry's own
`setup`/`teardown` is the default for the tests *that entry*
contributes, not the whole merged suite -- two entries (a shared
fragment's own suite and a board's own, say) each declaring one is the
ordinary case, not a conflict; a test's own `setup`/`teardown`
overrides its entry's default the same way it always would.

Every entry a specification's own `requires` chain contributes compiles
into **one** suite, sharing keywords and variables -- not one suite per
file. This is what makes `log_in: {}` above work: `Log In` is defined
in `examples/common/conf-accounts.yaml`, beside the very password it
sets, not in `busybox.yaml` at all:

```yaml
# examples/common/conf-accounts.yaml
playbook:
    - name: configure user accounts
      tasks:
          - name: set root password
            user: name=root password=$6$...

test:
    - name: shared login
      keywords:
          - name: Log In
            steps:
                - console_wait: {pattern: "login:", timeout: 120s}
                - console_send: {data: "root\n"}
                - console_wait: {pattern: "Password:", timeout: 15s}
                - console_send: {data: "welcome123\n"}
                - console_wait: {pattern: "[$#] $", timeout: 15s}
```

Any board `requires:`-ing `conf-accounts` reaches `Log In` the same way
it already reaches the password -- change the hash once and every
board's own suite picks it up, rather than a search-and-replace across
each one. Follow the same placement logic a spec's own `packages`/
`playbook` fragments already do (see [docs/specification.md](specification.md)):
a keyword belongs where what it depends on is defined, a suite testing
one concern belongs beside the fragment describing that concern. Two
entries defining the same keyword name is refused (`seine test`/
`test-validate` catch it, see below), since which of the two would
apply is not something to guess at silently.

An entry named the same as one already loaded amends it rather than
duplicating it: a board's own file naming `shared login` again can add
`tags`/`setup` the shared fragment didn't set, or add a case to its
`tests`, without repeating what `conf-accounts.yaml` already says.
Cases inside `tests` are matched and amended the same way, by their own
`name`. A setting both files already set keeps the first-loaded value,
the same "the specification reaching for a fragment overrides it" rule
`packages` follows; `keywords` and a case's own `steps` are the
exception -- two different bodies under the same name is refused rather
than silently picking one, the same as two entries defining the same
keyword name above.

`examples/rebuild-busybox/busybox.yaml` and
`examples/pc-image/test-boot.yaml` are complete, runnable examples;
`seine test examples/pc-image/main.yaml` runs the latter (`test-boot`
is in `main.yaml`'s own `requires:`).

### Steps

A step is either Robot's own plain text (`"Log    hello"`, space-
separated, for a one-off call needing nothing fancier) or one of:

* **A keyword call**, the common case:
  `{snake_name: ARGS}`, e.g. `power_cycle: {settle: 2s}` calls the
  `Power Cycle` keyword (snake_case turns into Title Case) with
  `settle=2s` as a named argument. A scalar (`console_send: "hi\n"`) or a
  list (`should_be_equal: ["${X}", "1"]`) becomes positional arguments
  instead of named ones. `assign` inside the mapping form captures the
  keyword's return value: `{console_run: {command: "uname -s", assign:
  KERNEL_NAME}}` makes `${KERNEL_NAME}` available to every step after
  it. `{call: NAME, args: [...], assign: VAR}` is the explicit escape
  hatch for a keyword name `snake_name` can't spell (one with digits or
  punctuation in it, say).
* **`if`/`elif`/`else`**:
  ```yaml
  - if: "'${ARCH}' == 'amd64'"
    then: [...]
    elif:
      - condition: "'${ARCH}' == 'arm64'"
        then: [...]
    else: [...]
  ```
* **`for_each`** (iterate a list) / **`for_range`** (iterate a range):
  ```yaml
  - for_each: {as: PORT, in: ["1", "2", "3"]}
    do: [...]
  - for_range: {as: I, start: 0, stop: 5, step: 1}
    do: [...]
  ```
* **`while`** (Robot's own polling primitive, `limit` a duration string
  it fails with a clear message past rather than spin forever) and
  **`retry_until`** (sugar over it -- `timeout`/`interval` instead of
  writing the `not (...)`/`Sleep` yourself). The condition is checked
  *before* the first iteration too, same as Robot's own `WHILE` --
  a variable the loop body only assigns (`SCREEN` below) needs a value
  set before the loop, or that first check fails with "Variable
  '$SCREEN' not found" instead of running the loop at all:
  ```yaml
  - set: {name: SCREEN, value: ""}
  - retry_until: "'login:' in $SCREEN"
    timeout: 120s
    interval: 5s
    do:
      - capture_screen: {name: boot, assign: SCREEN}
  ```
* **`try`/`except`/`else`/`finally`**, Robot's own structured error
  handling:
  ```yaml
  - try:
      - console_run: {command: "apt-get install -y might-not-exist"}
    except:
      - pattern: "*"
        do:
          - log: {message: "install failed, continuing anyway"}
  ```
  `pattern` is a glob against the failure's message by default (`type:
  regexp`/`type: literal` for the other two Robot supports); omit it to
  catch anything.
* **`break`** / **`continue`** (`{break: true}` / `{continue: true}`),
  valid inside `for_each`/`for_range`/`while` only -- Robot itself
  refuses one anywhere else, with its own clear error.
* **`set`**: `{set: {name: X, value: "1"}}` assigns `${X}` directly,
  Robot's own `VAR` statement, for a value that isn't a keyword's return.

### Conditions

`if`/`elif`/`while`/`retry_until` conditions are Robot's own expression
syntax -- ordinary Python-like comparisons. Reference a captured value
with `$NAME` (no braces): Robot binds the variable's real value into the
expression directly, rather than splicing its text in, so a captured
screen full of quotes and newlines is still safe to compare with `in`.
`${NAME}` (with braces) still works for building a string, e.g. inside a
keyword's own arguments.

`set:`'s own `value:` is always text (Robot's `VAR`, the same as a
`.robot` file's own variable table) -- `{set: {name: N, value: "0"}}`
makes `${N}` the *string* `"0"`, not the number, which then fails a
`while: "$N < 3"` comparing a string against an int. `${{ ... }}`
(double braces) is Robot's own escape into a real Python expression
instead of text substitution: `{set: {name: N, value: "${{0}}"}}` makes
`${N}` a native int, and `{set: {name: N, value: "${{$N + 1}}"}}`
increments it as one. A `for_each`/`for_range` loop variable is already
native (an int for `for_range`, whatever type the list held for
`for_each`) -- this only matters for `set:`.

### Reusable named actions (`keywords:`)

A `keywords:` entry is a named, parameterised bundle of steps -- Robot's
own user keyword, defined once and called like any other step
(`log_in: {}` above). Give it `args:` (a list of parameter names) and
its own `steps:`, exactly the same grammar as a test's. This is the
only way to bundle more than one step under `setup:`/`teardown:`, since
those (like Robot's own `[Setup]`/`[Teardown]`) take a single keyword
call each. Every `test:` entry a specification's `requires` chain
contributes shares one set of keywords -- see "Writing tests" above for
why that is where `Log In` lives, and why a name collision is refused.

## Keywords

Every seine/mtda action is a keyword, ready with no `library:` needed.

**`seine.testing.library.target.TargetLibrary`** -- thin wrappers over
`seine.tui.target`, the same functions `/target` itself calls:
`Connect Target [agent]`, `Disconnect Target`, `Power On`/`Power Off`/
`Power Toggle`/`Power Cycle [settle]`, `USB Port PORT STATE`,
`Console Send DATA`, `Console Run COMMAND` (waits for mtda's configured
prompt to know a command finished -- its default, `=> `, is a U-Boot
prompt, so it needs a real one set first; `Console Prompt
[NEW_PROMPT]` sets it, `Log In` (examples/common/conf-accounts.yaml)
already does after login. `Console Send` + `Console Wait` don't depend
on it at all. A command that pages its own output -- `dpkg -l` past a
terminal's height, say -- never returns either, for the same reason: it
is genuinely still running, sitting at its own `--More--` prompt, not
the shell's; pipe it through `| cat` the way any non-interactive use of
a pager-backed tool has to), `Console Wait PATTERN [TIMEOUT]`
(truthy once `PATTERN` appears, falsy on timeout -- not the matched
text, that's mtda's own protocol; read the console with `Console Tail`/
`Console Dump`/`Capture Screen` instead), `Console Dump`/`Console Tail`,
`Target Status`, `Storage To Host`/`Storage To Target`,
`Write Image To Target PATH`, `Storage Snapshot`/`Storage Rollback`,
`Keyboard Press KEY [repeat] [ctrl] [shift] [alt] [meta]`,
`Keyboard Write TEXT`, `Mouse Move X Y [buttons]` (one HID report --
`buttons` held down right now, absolute HID coordinates 0-32767, not
pixels), `Mouse Click X Y [button] [hold]` (a real click is two reports,
button down then up a moment apart, exactly like a physical mouse --
this is that pair, `Mouse Move` alone is not a click).

**`seine.testing.library.image.ImageLibrary`** -- the very
specification `test` came from, and the image seine already knows how
to build/read: `Get Spec Value PATH` (dotted, e.g.
`distribution.architecture`), `Build Image [FILES...]` (defaults to the
same files; the same `BuildCmd` `seine build` uses), `Inspect Image
Path [PATH]` (lists a directory or reads a file from the last built
image, via the same read-only `Inspector` `seine inspect` uses).

**`seine.testing.library.observation.ObservationLibrary`** -- two kinds
of screen, both plain values a step can `assign:` and use in any
condition or `should_*` keyword: `Capture Screen [name]` (the console's
decoded text, pyte's own screen -- the same one the Remote Target screen
renders) and `Capture Screen Image [name]` (a real video frame, mtda's
own `VideoSnapshot`, for a target with nothing useful on its serial
console -- a Wayland/Qt app has pixels, not text, to check; saved under
`--outdir` and returns the file path). `Screen Should Contain`/
`Screen Matches` are convenience over the text case, calling straight
into Robot's own `BuiltIn`. `Classify Screen` is a documented,
not-yet-implemented seam for an OpenCV/vision-model classifier over a
captured frame -- see that module's own comment for why capturing is
done and classifying isn't.

**Robot's own `BuiltIn`** needs no import either -- `Should Be Equal`,
`Should Contain`, `Should Be True`, `Should Match Regexp`, `Sleep`, `Log`,
`Fail`, `Skip`, and the rest are already there, reached through the same
`{snake_name: ARGS}` shorthand as a seine keyword (`should_contain:
[...]` calls `Should Contain`). No seine-specific assertion library was
written: Robot's own is mature and already covers this, reinventing it
would only be a second thing to keep correct.

## What Robot Framework brings, and what doesn't apply here

Keywords, variables, return values, `IF`/`ELSE`, `FOR`, `WHILE`, `BREAK`/
`CONTINUE`, `TRY`/`EXCEPT`/`FINALLY`, setup/teardown, tags, suites, and
structured pass/fail/skip results are all Robot's, reached through
`robot.running`'s own programmatic model (`TestSuite`/`.body.create_*()`)
rather than by generating `.robot` text and re-parsing it -- one less
format to keep in sync with Robot's own.

Deliberately not exposed:

* **`.robot`/`.resource` text files.** A test is always written as
  seine's own YAML, inside an ordinary specification; Robot's plain-text
  format is not a second front-end seine offers. `library:` still names
  ordinary Robot/Python keyword libraries by dotted path, so an existing
  Python library (not a `.robot` file) drops in unchanged.
* **Data-driven templates (`[Template]`) and `robot.api.TestSuiteBuilder`
  directory discovery.** `for_each`/`for_range` already cover "run this
  shape with different data" for what a hardware suite needs; a
  directory-of-files test layout wasn't asked for and would be a second
  way to organise suites alongside `requires:`-style composition,
  which a specification already has.
* **Robot's own CLI (`robot`/`rebot`) and `--listener`/`--prerunmodifier`
  plugin surface.** `seine test` is the one entry point; Robot's own is
  not additionally exposed, so there is one command to learn, one set of
  flags, one place results are reported (though `output.xml` is still
  written, for anyone who wants Robot's own `rebot`/`log.html` on top).
* **Remote library / XML-RPC keyword servers.** Every keyword here runs
  in-process, in the same Python process already talking to mtda; a
  keyword server would only matter for a keyword implemented in another
  language, which nothing in seine needs yet.

## Doctor

`seine doctor` reports whether `robotframework` is importable, the same
"optional, a note not an error" shape it already reports `mtda` in
(`Optional test automation (seine.testing)`).

## AI chat

`run-test` (gated, needs confirmation) runs the active specification's
own tests the same way `seine test` does, reporting into the Test
screen as it goes; `test-validate` (ungated -- Robot's own dry run,
nothing real happens) proves a `test:` section just edited via
`spec-update`/`spec-create` is at least well-formed before spending a
real run on it; `test-result` (ungated) reads back what the last run in
this session did. There is no dedicated tool for writing or editing a
`test:` section -- it is spec content, changed with `spec-update`/
`spec-create` the same as any other section. See [docs/ai.md](ai.md)
for the AI chat generally.
