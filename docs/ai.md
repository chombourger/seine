## The AI chat

`seine tui`'s Chat screen lets a real model answer questions about the
active specification and, with explicit approval, act on it. It is
entirely optional -- everything else in the TUI works without it -- and
off by default.

### Enabling it

Set `llm_model` (`/settings`, or `SEINE_LLM_MODEL` as an environment
override) and, usually, `llm_api_base`/`SEINE_LLM_API_BASE` to point at
where the model actually lives. `SEINE_LLM_API_KEY` holds the
credential, if the endpoint wants one -- `settings.json` never does.
With nothing set, a bare prompt (anything not starting with `/`) says
so instead of reaching a model.

There is no separate "provider" setting -- `llm_model` *is* the
provider selector, in the `<provider>/<model>` form the underlying
library (litellm) already uses everywhere. Two shapes cover most
setups:

* **An OpenAI-compatible endpoint** -- a hosted API, or a self-hosted
  server (vLLM, llama.cpp) -- `llm_model` is `openai/<model-name>`,
  `llm_api_base` points at it.
* **Ollama, directly** -- `llm_model` is `ollama_chat/<model-name>`,
  `llm_api_base` the usual `http://localhost:11434` or wherever it's
  reachable.

litellm supports a good many other providers the same way -- its own
model-name prefix is the thing to check for one not listed here.

### The tools

The model never invents what it knows -- every answer is grounded in a
tool call, the same information every screen already renders. Most are
read-only and run without asking:

| Tool | What it reads |
| --- | --- |
| `overview`, `plan` | Build status, and what a build would do, diffed against the last real one |
| `doctor` | Whether this machine has what a build needs |
| `build-status`, `task-log` | Per-step status and one step's own log tail, of a build this session ran |
| `packages`, `analyze`, `artifacts`, `cache` | The same information `seine --sbom`/`analyze`/`cache` give on the real command line |
| `installed-sizes`, `sbom-diff` | dpkg's real installed-package list; two SBOMs diffed |
| `spec-files` | Every file this build actually loaded, plus unloaded siblings worth pulling in |
| `read` | One file this build trusts -- see [Trust model](#trust-model) below |
| `spec-query`, `spec-dump` | A JSONPath search, or the fully merged spec tree |
| `docs` | Written reference, chunked: the AI's own system prompt, one cluster file per group of related rules (always present), and this project's own written docs (`docs/*.md`, not always present) -- see below |
| `gist-list`, `gist-show` | Reusable spec fragments kept outside any one project -- see [Gists](building.md#gists) |
| `reset-conversation` | Forgets everything discussed so far |

`docs` reaches beyond both the spec and the model's own built-in facts,
for two different things under one name (never ambiguous -- a cluster
file and a `docs/*.md` file never share an extension): detail behind
the system prompt's own rules -- see [The system
prompt](#the-system-prompt) below -- and this project's own written
docs, the schema reference you're reading now, `kernels.md`, and the
rest. The cluster files always ship; `docs/*.md` is the one part whose
presence depends on how seine itself got installed -- a source
checkout or an editable install always has it, a packaged install may
not, and the tool says so plainly rather than pretending to have read
something it hasn't.

Eight more can act, and none of them run unconfirmed -- each shows the
exact effect (a real diff where one applies, added lines green, removed
red) and waits for "Yes"/"No" before doing anything:

* `start-build` / `cancel-build` -- the same as `/build`/`Ctrl+C` on the
  Build screen. Once a build `start-build` itself started actually
  finishes -- the whole build, not a single step -- the chat gets one
  unprompted turn to report on it: what got rebuilt and what the SBOM
  found on success, the failed step's own log on failure. If nothing
  has switched screens since the AI itself opened the Build screen to
  start it, finishing switches back to Chat too, so the answer is shown
  rather than left for someone to notice on their own; having gone
  anywhere else in the meantime is left alone. A build started any
  other way (`/build`, the Build screen) gets no such notice.
* `spec-update` -- changes one node in an already-loaded file, named by
  the same JSONPath `spec-query` already returned for it.
* `spec-create` -- writes a brand new file, only next to one already
  loaded. Creating it is only half the job: it still needs `side-load`
  (to preview the merge) and a `spec-update` on `requires:` to become
  permanent, the same as an existing-but-unloaded sibling `spec-files`
  found.
* `side-load` -- loads one more file on top of the active spec, live,
  the same as typing `/side-load` yourself -- session-only. Its own
  result already includes the merge diff.
* `side-unload` -- the reverse of `side-load`: drops one file back out
  of the active spec, live, the same as typing `/side-unload`. Works on
  any file currently loaded, not only one `side-load` itself added.
* `gist-create` -- saves a fragment as a reusable gist (see
  [Gists](building.md#gists)), for a person to `side-load` in any
  project later, not just this one. Refused if the name is taken.
* `gist-delete` -- permanently removes one. Never touches a project
  that already `side-load`ed it -- only the gist itself.

### Trust model

Every read-only tool above is scoped to files this build *itself*
already vouches for -- never an arbitrary path, and never the whole
filesystem. The same rule answers the same way for the AI chat,
`seine build -D`/`--dump`, and anything else that asks:

* **Loaded files** -- every YAML file this specification actually
  pulled in via `requires:`.
* **Siblings** -- `*.yml`/`*.yaml` sitting in the same directory as a
  loaded file, or (once loaded files span more than one directory) in a
  directory alongside them under their common ancestor. Not part of the
  build, but a candidate for it.
* **Referenced local files** -- a local file one of this build's own
  `packages:` entries names: a patch, a kernel config fragment, a
  derived-flavour fragment -- the exact same files a real build already
  reads to compile them. Only `packages:` counts, never
  `defaults.packages:` -- a [default](specification.md#defaults)
  describes a package, it doesn't build one, so a file it names is not
  trusted just for being mentioned.

`read` is the one tool that spans all three: a loaded or sibling file
comes back as YAML, structurally redacted; a referenced file comes back
as the plain text it is, with the same secrets redacted as a flat
substitution. A path that is none of these is refused, with the reason
named.

**Deliberately no path-containment check** beyond what already exists.
A spec could in principle name a patch that resolves outside its own
directory -- but a real build already reads, compiles, and ships
whatever a `patches:`/`config:` entry names, a strictly bigger exposure
than a read-only preview of the same file through chat. Adding
containment here would invent a restriction the build itself doesn't
have, not close a gap this tool opens.

This is **not** a general filesystem-read tool. A path with no
connection to the active build -- someone else's dotfiles, `/etc/shadow`,
a file mentioned only in an unrelated ansible task's `copy:`/`template:`
argument -- is refused. The model is also told never to guess a path:
every legal one has to have actually turned up in a `spec-files`/
`spec-query`/`spec-dump` result first.

**Gists are the one deliberate exception** to "scoped to files this
build vouches for": `gist-list`/`gist-show` read a fixed directory
outside any build entirely, by design -- that's the point of a gist,
reusable across projects rather than trusted only within one. A gist
is still just a spec fragment, the same shape `read` already shows for
a referenced file, so nothing new is exposed -- only *where* it can
come from is different.

### The system prompt

`seine/data/system_prompt.txt` is read fresh every turn -- editing it
needs no code change or restart. Two things worth knowing before
touching it:

* Everything above the file's own `---` marker is stripped before the
  text reaches the model -- editing guidance for whoever changes the
  file next, not something worth spending tokens on with every turn.
  This is also where the policy for the split described below is
  written down.
* Below the marker, rules carry a bracketed mnemonic (`[SCOPE]`,
  `[BUILD-NOTIFY]`, ...) so one rule can point at another by name
  instead of restating it. These are bookkeeping for the prompt itself
  -- the model is told, in the first line below the marker, never to
  mention one to the person it's talking with. Keep an existing tag's
  spelling and meaning stable when editing; a genuinely new rule gets a
  new tag rather than reusing or repurposing one.

Only what's needed on *every* turn lives in this one file -- tool-call
framing, `[SCOPE]`'s routing, `[GATED]`/`[DENIED]`'s safety rules, and
`[PROMPT-DOCS]`'s own index. Detail specific to one kind of question
-- build status, spec lookup, editing a file, gists, kernel configs,
external references -- lives instead in a cluster file under
`seine/data/prompt/*.txt`, one per group of related tags, fetched with
the same `docs` tool only once that kind of question actually comes
up (it also serves `docs/*.md`, see [The tools](#the-tools) above; a
cluster file and a `docs/*.md` file never share a name since only the
latter ends in `.md`). Unlike `docs/*.md`, these ship with every
install (ordinary `package_data`, the same as `system_prompt.txt`
itself) -- there is no "not present" case to handle. A tag stays
global regardless of which file holds it, so a cluster file can point
at `[GATED]` in the main prompt, or at a tag in a different cluster
file, the same way the main prompt points at them. `[PROMPT-DOCS]`
also tells the model to fetch a cluster fresh each time it's needed,
never to rely on what an earlier turn's fetch (or plain training
knowledge) said -- the same reason `system_prompt.txt` itself is read
fresh every turn rather than cached.
