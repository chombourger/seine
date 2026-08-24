# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import copy
import difflib
import getopt
import jinja2
import jinja2.meta
import os
import re
import shutil
import subprocess
import sys
import yaml

from seine            import settings
from seine.image      import Image
from seine.cmd        import Cmd
from seine.partition  import PartitionHandler
from seine.tasks      import Interrupted
from seine.utils      import ContainerEngine, digest, locked
from seine.utils      import redact, redactions

# Specifications are rendered before they are parsed, so that one file can
# say what is true of several architectures or releases instead of being
# copied once per each.
#
# The delimiters are not jinja's own on purpose. A specification carries
# ansible tasks, and ansible templates them itself, on the target, at the
# time the playbook runs: '{{ ansible_facts.hostname }}' is a value seine
# has no business resolving and could not resolve if it wanted to. Sharing
# jinja's delimiters would mean eating those at load time, so seine takes a
# pair of its own and leaves '{{ }}' and '{% %}' alone for ansible.
#
# StrictUndefined rather than a name that renders to nothing: a
# specification whose architecture quietly went empty builds an image for
# the wrong machine and says so nowhere.
TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)

# The same, for the pass that only wants to know what a specification sets:
# a name it has not reached yet renders to nothing instead of stopping the
# walk. Its output is thrown away, so nothing is decided on what it makes of
# a half-known file.
PROBE = TEMPLATE.overlay(undefined=jinja2.ChainableUndefined)

# Full-width bars rather than a one-character marker: what a specification
# gained and lost is read off the shape of the block. Green for a gained
# line, red for a lost one.
ADDED   = "\x1b[48;5;22m\x1b[97m"
REMOVED = "\x1b[48;5;52m\x1b[97m"
RESET   = "\x1b[0m"

# Where the last build's specification is kept.
def _baseline(files):
    return os.path.join(ContainerEngine.cache("plans"),
                        "%s.yml" % digest(files))

# What these files last built, or None. Unreadable counts as never built: a
# plan is still worth printing without a baseline.
def recall(files):
    try:
        with open(_baseline(files)) as f:
            return f.read()
    except OSError:
        return None

def remember(files, text):
    path = _baseline(files)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    except OSError:
        pass

def _bar(color, text, width):
    return "%s%s%s" % (color, text.ljust(width) if len(text) < width else text,
                       RESET)

# One 'name: value' and everything under it. Rendered setting by setting
# rather than by yaml.dump on the whole document, since every line needs a
# mark of its own.
def _pair(name, value, indent):
    pad = "  " * indent
    if isinstance(value, dict) and len(value) > 0:
        return [pad + "%s:" % name] + _body(value, indent + 1)
    if isinstance(value, list) and len(value) > 0:
        # Items indented under their key, which YAML allows: folded context
        # can then say which list a change is in.
        return [pad + "%s:" % name] + _items(value, indent + 2)
    return [pad + line for line in
            yaml.dump({name: value}, default_flow_style=False).splitlines()]

def _body(mapping, indent):
    lines = []
    for name in sorted(mapping):
        lines += _pair(name, mapping[name], indent)
    return lines

def _items(values, indent):
    lines = []
    for value in values:
        lines += _item(value, indent)
    return lines

def _item(value, indent):
    if isinstance(value, dict) and len(value) > 0:
        return _dashed(_body(value, indent), indent)
    if isinstance(value, list) and len(value) > 0:
        return _dashed(_items(value, indent), indent)
    return ["  " * (indent - 1) + "- " +
            yaml.dump(value, default_flow_style=False).splitlines()[0]]

# The item's '-' on its first line, where a YAML reader looks for it.
def _dashed(lines, indent):
    return ["  " * (indent - 1) + "- " + lines[0].lstrip()] + lines[1:]

def _dashed_marks(marked, indent):
    mark, text = marked[0]
    if mark == " ":
        return [(" ", "  " * (indent - 1) + "- " + text.lstrip())] + marked[1:]
    # The item did not change, its contents did: keep the '-' on an unmarked
    # line of its own, or it reads as the whole item added or removed.
    return [(" ", "  " * (indent - 1) + "-")] + marked

def _marked(mark, lines):
    return [(mark, line) for line in lines]

# What a list item goes by, so a partition whose size changed reads as that
# partition changed rather than one gone and another arrived.
#
# ponytail: hard-coded keys, not something the sections declare. An item
# nothing matches prints as one removed and one added, which is still true
# -- add the section's own key here if that reads badly for it.
NAMES = ("name", "label", "filename", "where", "suite", "package")

def _named(item, others):
    if isinstance(item, dict) == False:
        return None
    for key in NAMES:
        if key in item:
            for other in others:
                if isinstance(other, dict) and other.get(key) == item[key]:
                    return other
    return None

# Compared setting by setting rather than line by line: a line diff of YAML
# calls an indentation that shifted a change.
def _changes(old, new, indent=0):
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in new:
            lines += _marked("-", _pair(name, old[name], indent))
        elif name not in old:
            lines += _marked("+", _pair(name, new[name], indent))
        elif old[name] == new[name]:
            lines += _marked(" ", _pair(name, new[name], indent))
        elif isinstance(old[name], dict) and isinstance(new[name], dict):
            lines.append((" ", "  " * indent + "%s:" % name))
            lines += _changes(old[name], new[name], indent + 1)
        elif isinstance(old[name], list) and isinstance(new[name], list):
            lines.append((" ", "  " * indent + "%s:" % name))
            lines += _listed(old[name], new[name], indent + 2)
        else:
            lines += _marked("-", _pair(name, old[name], indent))
            lines += _marked("+", _pair(name, new[name], indent))
    return lines

# Two lists, in the new one's order. Equal items match first, the rest by
# the name they go by (NAMES) and are then compared as two of the same
# thing.
def _listed(old, new, indent):
    # repr because dicts cannot be hashed. Equal dicts have equal reprs
    # here: they came out of yaml, which orders their keys the same way.
    matcher = difflib.SequenceMatcher(None, [repr(item) for item in old],
                                      [repr(item) for item in new])
    lines = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for item in new[j1:j2]:
                lines += _marked(" ", _item(item, indent))
            continue
        gone, matched = list(old[i1:i2]), {}
        for n, item in enumerate(new[j1:j2]):
            match = _named(item, gone)
            if match is not None:
                gone.remove(match)
                matched[n] = match
        # Removals before additions, so a replacement reads old then new.
        for item in gone:
            lines += _marked("-", _item(item, indent))
        for n, item in enumerate(new[j1:j2]):
            if n not in matched:
                lines += _marked("+", _item(item, indent))
            else:
                lines += _dashed_marks(_changes(matched[n], item, indent),
                                       indent)
    return lines

def _depth(text):
    return len(text) - len(text.lstrip())

# How much of what did not change is printed around what did.
CONTEXT = 3

# A specification is hundreds of lines and a change to it is a few, so what
# did not change is folded away -- keeping the lines around a change and the
# keys it sits under, so it still reads as a place in the specification.
# With nothing changed there is nothing to fold around, so all of it stays.
def _folded(lines, context=CONTEXT):
    changed = [i for i, (mark, _) in enumerate(lines) if mark != " "]
    if len(changed) == 0:
        return lines
    keep = set()
    for i in changed:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    # An added 'size' is worth little without the partition it is in.
    for i in sorted(keep):
        depth = _depth(lines[i][1])
        for j in range(i - 1, -1, -1):
            if depth == 0:
                break
            if _depth(lines[j][1]) < depth:
                keep.add(j)
                depth = _depth(lines[j][1])
    # Folding one or two lines costs a line to say so, and saves nothing.
    run = []
    for i in range(len(lines)):
        if i not in keep:
            run.append(i)
            continue
        if 0 < len(run) <= 2:
            keep.update(run)
        run = []
    folded, hidden = [], 0
    for i, line in enumerate(lines):
        if i not in keep:
            hidden += 1
            continue
        if hidden > 0:
            folded.append((" ", "%s... (%d line%s unchanged)"
                           % (" " * _depth(line[1]), hidden,
                              "" if hidden == 1 else "s")))
            hidden = 0
        folded.append(line)
    if hidden > 0:
        folded.append((" ", "... (%d line%s unchanged)"
                       % (hidden, "" if hidden == 1 else "s")))
    return folded

# The new specification with what changed since the baseline marked, and
# the rest folded away. Coloured for a terminal, a '+' or a '-' in column
# one for a pipe or a file.
def diff(old, new, color=True, width=None):
    if width is None:
        width = shutil.get_terminal_size((80, 24)).columns
    after = yaml.safe_load(new) or {}
    # No baseline means nothing to say about what changed, not that every
    # line is new.
    try:
        before = (yaml.safe_load(old) or {}) if old else after
    except yaml.YAMLError:
        before = after
    lines = []
    for mark, text in _folded(_changes(before, after)):
        # Marked even when coloured: a bar is lost in a paste, and not
        # everyone can tell the two colours apart.
        text = mark + text
        if color == False:
            lines.append(text.rstrip())
        elif mark == "+":
            lines.append(_bar(ADDED, text, width))
        elif mark == "-":
            lines.append(_bar(REMOVED, text, width))
        else:
            lines.append(text)
    return "\n".join(lines) + "\n"

# Colour for a terminal and nothing else. NO_COLOR is the convention;
# '--no-color' is what someone reaches for without knowing it.
def colorless(options, stream=None):
    if options.get("color") == False:
        return True
    stream = stream or sys.stdout
    return os.environ.get("NO_COLOR") is not None or stream.isatty() == False

class BuildCmd(Cmd):
    # What this command is called and what it prints for '-h'. A command that
    # is the same build with one option decided for it says so by overriding
    # these rather than by copying main().
    NAME = "build"
    SHORT_OPTIONS = "dDhj:kv"
    LONG_OPTIONS = [
        "debug",
        "dry-run",
        "dump",
        "help",
        "jobs=",
        "keep",
        "no-color",
        "packages-only",
        "parallel=",
        "rebuild",
        "require-hashes",
        "rootfs-only",
        "sbom",
        "sign-key=",
        "spec-only",
        "target=",
        "tasks-only",
        "verbose"
    ]

    def __init__(self):
        self.image = None
        # 'jobs' falls back to the persisted setting (seine/settings.py,
        # '/set jobs N' in the TUI) before the hardcoded '1' -- an
        # explicit '-j'/'--jobs' below still overrides either.
        self.options = { "ansible_library": [], "build": True, "color": None,
                         "debug": False, "dry_run": False,
                         "jobs": settings.load().get("jobs") or 1, "keep": False,
                         "packages_only": False, "parallel": None,
                         "rebuild": False, "require_hashes": False,
                         "rootfs_only": False,
                         "sbom": False, "sign_key": None, "spec": True,
                         "target": None,
                         "tasks": True, "verbose": False }
        self.partitionHandler = PartitionHandler()
        self.spec = None
        # self.spec exactly as merged, before parse() mutates it in place
        # (size: strings -> byte ints) -- what Inspector needs.
        self.raw_spec = None
        self._loading = []
        # Every real file this build has loaded, in order first reached
        # -- unlike _loading above, never popped: the durable record
        # dump_file() checks a path against.
        self.loaded_files = []
        self._probing = False
        self._variables = None
        self._names = []
        self._prober = None
        self._probed = set()

    # A specification handed over as text, rather than as a tree of files to
    # walk: there is nothing to probe, so it renders against the
    # specification merged so far and reads what earlier calls have set.
    def loads(self, yaml_spec):
        return self._load("<string>", yaml_spec)

    # Every file the command names, probed before any of them is loaded:
    # what one asks for may be set by another further along the line, and
    # 'seine build' takes as many of them as a user cares to compose.
    def load_all(self, yaml_files):
        for yaml_file in yaml_files:
            self._probe(yaml_file, check=False)
        self._check_names(self._prober._names if self._prober else [])
        for yaml_file in yaml_files:
            self.load(yaml_file)
        return self.spec

    # What a specification sets, learned before it is loaded for real.
    #
    # Rendering against the specification merged so far is enough for a
    # fragment to read what the file that reached for it said, and not
    # enough for it to read what a fragment listed after it says. That is an
    # ordering nobody should have to keep in their head, so the tree is
    # walked once beforehand with a lenient jinja, purely to collect what it
    # sets, and the result of that walk is thrown away.
    #
    # Only the names it found survive it, as the context the real walk
    # renders against. Nothing the lenient render made of a half-known file
    # reaches the specification that gets built.
    #
    # One walk for every file, merged the way a specification is merged
    # rather than by replacing whole sections: two files both naming
    # 'distribution' -- one the architecture, one the components a board's
    # firmware needs -- each said half of it, and the second took the
    # first's half away.
    def _probe(self, yaml_file, check=True):
        if self._prober is None:
            self._prober = BuildCmd()
            self._prober._probing = True
        self._prober.load(yaml_file)
        self._probed.add(os.path.realpath(yaml_file))
        self._variables = self._prober.spec or {}
        if check:
            self._check_names(self._prober._names)

    # Every name the specification asks for that it never sets, in one
    # message. The walk has been to every file by the time this runs, so
    # reporting the first and stopping would be a choice to make someone
    # find the rest one build at a time.
    #
    # Names, not paths: what a file asks of a name it does read -- the
    # 'architecture' of a 'distribution' that is set -- is a question about
    # a value rather than about the specification's shape, and the load that
    # follows answers it against the real values.
    def _check_names(self, names):
        missing = []
        for filename, asked in names:
            for name in sorted(asked - set(self._variables or {})):
                missing.append("%s: '%s' is not set by this specification"
                               % (filename, name))
        if len(missing) > 0:
            raise ValueError("\n".join(missing))

    # The files being loaded, innermost last. 'requires' pulls in files that
    # pull in files themselves, and nothing stopped two of them from reaching
    # for each other: seine recursed until Python ran out of stack, with a
    # traceback naming neither file. A file already on the chain is one being
    # loaded again before it finished, which is the loop.
    #
    # A file reached twice by two different paths is not, and still loads
    # twice: that is a specification listing a fragment its fragments also
    # list, which is how they are meant to be composed.
    def load(self, yaml_file):
        if self._probing is False and len(self._loading) == 0 \
                and os.path.realpath(yaml_file) not in self._probed:
            self._probe(yaml_file)
        path = os.path.realpath(yaml_file)
        if path in self._loading:
            loop = self._loading[self._loading.index(path):] + [path]
            raise ValueError("'requires' loops: %s!" % " -> ".join(loop))
        self._loading.append(path)
        try:
            with open(yaml_file, "r") as f:
                return self._load(yaml_file, f.read())
        finally:
            self._loading.pop()

    # A specification is rendered against the one built so far. Files are
    # loaded before the 'requires' they list, so a fragment sees what the
    # specification that reached for it had already said -- which is where
    # the architecture and the release a fragment speaks of come from.
    #
    # Blocks are not accepted, only substitutions. Branching in a
    # specification is what 'requires' is: listing the fragments that apply
    # says which ones apply, in a file that can be read without running it.
    BLOCKS = re.compile(re.escape(TEMPLATE.block_start_string))

    # 'requires' is read from the rendered file like everything else, so a
    # require could name a file through a variable -- and then the set of
    # files a specification is made of depends on the render, which is
    # decided by the files. Kept out from the start.
    REQUIRES = re.compile(r"^([ \t]*)requires:.*?(?=^\1\S|\Z)",
                          re.MULTILINE | re.DOTALL)

    def _render(self, yaml_filename, yaml_spec):
        block = BuildCmd.BLOCKS.search(yaml_spec)
        if block is not None:
            raise ValueError("%s: '%s' blocks are not accepted, only '%s %s' "
                "substitutions -- list the fragments that apply under "
                "'requires' instead!"
                % (yaml_filename, TEMPLATE.block_start_string,
                   TEMPLATE.variable_start_string, TEMPLATE.variable_end_string))
        for requires in BuildCmd.REQUIRES.finditer(yaml_spec):
            if TEMPLATE.variable_start_string in requires.group(0):
                raise ValueError("%s: 'requires' cannot be templated!"
                                 % yaml_filename)
        context = self._variables if self._variables is not None else self.spec
        try:
            template = PROBE if self._probing else TEMPLATE
            if self._probing:
                self._names.append((yaml_filename,
                    jinja2.meta.find_undeclared_variables(
                        template.parse(yaml_spec))))
            return template.from_string(yaml_spec).render(context or {})
        except jinja2.TemplateError as e:
            raise ValueError("%s:%s: %s"
                % (yaml_filename, getattr(e, "lineno", "?"), e)) from e

    # The text of one specification file, not a stream: what a file says is
    # read before it is parsed, and both callers hand over the same thing.
    #
    # A file that does not parse is skipped while probing rather than
    # reported. What failed to parse there is a lenient render, where a name
    # the walk had not reached yet left a value empty, so the fault may be
    # the render's rather than the file's. The real walk reads the same file
    # with every name in hand and reports what is wrong with the file
    # itself; what is lost by skipping it here is the names it would have
    # contributed.
    def _load(self, yaml_filename, yaml_spec):
        if yaml_filename != "<string>":
            path = os.path.realpath(yaml_filename)
            if path not in self.loaded_files:
                self.loaded_files.append(path)
        try:
            spec = yaml.safe_load(self._render(yaml_filename, yaml_spec))
        except yaml.YAMLError:
            if self._probing:
                return self.spec
            raise

        # Patches and kconfig fragments are listed relative to the file
        # listing them, which merging would otherwise lose. Resolved here,
        # where the file they came from is still known: a package assembled
        # from several files has no one directory to carry along.
        for package in self._package_entries(spec):
            self._resolve_files(package, os.path.dirname(yaml_filename))
            self._record_origins(package, yaml_filename)

        # A fragment ships its own Ansible modules the way it ships kconfig
        # fragments: 'library/' beside it, found by convention rather than a
        # setting naming it.
        if yaml_filename != "<string>":
            libdir = os.path.join(os.path.dirname(yaml_filename), "library")
            if os.path.isdir(libdir):
                libdir = os.path.realpath(libdir)
                if libdir not in self.options["ansible_library"]:
                    self.options["ansible_library"].append(libdir)

        if self.spec is None:
            self.spec = spec
        else:
            self.merge(spec)

        if "requires" in spec:
            for req in spec["requires"]:
                req_path = os.path.join(os.path.dirname(yaml_filename), req)
                req_yml = os.path.normpath("%s.yml" % req_path)
                req_yaml = os.path.normpath("%s.yaml" % req_path)
                if os.path.isfile(req_yml):
                    req_path = req_yml
                elif os.path.isfile(req_yaml):
                    req_path = req_yaml
                else:
                    raise FileNotFoundError("%s: '%s' could not be found in %s/!"
                        % (yaml_filename, req, os.path.dirname(req_path)))
                self.load(req_path)
        return self.spec

    # Every package entry a file holds, whether it is asking for a build or
    # only describing one: both name files relative to the file they are in.
    def _package_entries(self, spec):
        spec = spec or {}
        entries = list(spec.get("packages") or [])
        entries += list((spec.get("defaults") or {}).get("packages") or [])
        return [e for e in entries if type(e) == type({})]

    # Which file each of a package's settings came from. A package is
    # routinely described by several, so there is no such thing as "the
    # file this package came from" -- only the file that wrote each
    # setting, which is the one a user would open to change it.
    #
    # Nested settings are named by their path, 'extends.kernel.upstream',
    # so one flat map answers for all of them. It rides along under a key
    # starting with an underscore, which the dump already hides.
    ORIGINS = "_origins"

    def _record_origins(self, package, filename, prefix=""):
        origins = package.setdefault(BuildCmd.ORIGINS, {}) if prefix == "" else None
        for setting, value in list(package.items()):
            if setting.startswith("_"):
                continue
            if prefix == "" and setting == "extends" and type(value) == type({}):
                for kind, settings in value.items():
                    if type(settings) != type({}):
                        continue
                    for name in settings:
                        package[BuildCmd.ORIGINS]["extends.%s.%s" % (kind, name)] = filename
                continue
            package[BuildCmd.ORIGINS][setting] = filename
        return package

    # Where a setting was written down, for the messages that ask someone
    # to change it.
    @staticmethod
    def origin_of(package, setting):
        return (package.get(BuildCmd.ORIGINS) or {}).get(setting)

    # The settings of a package that name files, as the path to reach them
    # from the package's own dictionary.
    FILE_LISTS = [["patches"], ["extends", "kernel", "fragments"]]

    def _resolve_files(self, package, dirname):
        for path in BuildCmd.FILE_LISTS:
            holder = package
            for key in path[:-1]:
                holder = holder.get(key) if type(holder) == type({}) else None
            if type(holder) != type({}):
                continue
            names = holder.get(path[-1])
            if type(names) != type([]):
                continue
            holder[path[-1]] = [
                os.path.normpath(os.path.join(dirname, name))
                if type(name) == type("") else name for name in names]

        # 'derived-flavours' nests fragments two levels deeper than
        # FILE_LISTS reaches; resolved here for the same reason: relative
        # to the file that named it, not to the build's own directory.
        extends = package.get("extends")
        kernel = extends.get("kernel") if type(extends) == type({}) else None
        derived = kernel.get("derived-flavours") if type(kernel) == type({}) else None
        if type(derived) == type({}):
            for base, names in derived.items():
                if type(names) != type({}):
                    continue
                for name, fragments in names.items():
                    if type(fragments) != type([]):
                        continue
                    names[name] = [
                        os.path.normpath(os.path.join(dirname, f))
                        if type(f) == type("") else f for f in fragments]

    def _merge_distro(self, spec):
        if "distribution" in spec:
            if "distribution" in self.spec:
                for setting in spec["distribution"]:
                    if setting == "feeds":
                        self._merge_feeds(spec["distribution"]["feeds"])
                        continue
                    self.spec["distribution"][setting] = spec["distribution"][setting]
            elif "distribution" not in self.spec:
                self.spec["distribution"] = spec["distribution"]

    # Feeds are merged by suite rather than replaced wholesale, the way
    # partitions and volumes are merged by label: a file adding one feed
    # would otherwise have to restate the others to keep them, and
    # restating them means writing down URIs a snapshot has changed.
    # Naming a suite already listed still overrides it.
    def _merge_feeds(self, feeds):
        merged = self.spec["distribution"].get("feeds")
        if merged is None:
            self.spec["distribution"]["feeds"] = feeds
            return

        for feed in feeds:
            suite = feed.get("suite") if type(feed) == type({}) else None
            existing = [f for f in merged
                        if type(f) == type({}) and f.get("suite") == suite]
            if suite is not None and len(existing) > 0:
                existing[0].update(feed)
            else:
                merged.append(feed)

    def _merge_imager(self, spec):
        if "imager" in spec:
            if "imager" in self.spec:
                for setting in spec["imager"]:
                    self.spec["imager"][setting] = spec["imager"][setting]
            else:
                self.spec["imager"] = spec["imager"]

    # Merged by name, reusing _merge_named_list()/_merge_settings(): a
    # fragment reached twice via two 'requires:' paths (conf-accounts's
    # own 'configure user accounts', say) used to duplicate its
    # playbook entry once per path. 'tasks:' stays one additive list
    # rather than merged task-by-task -- ansible tasks run in order, so
    # folding two same-named tasks into one is a bigger behavior change
    # than it would be for an independent list entry; only an exact
    # repeat (the reached-twice case) is dropped.
    #
    # direction: asking file wins (docs/merging.md).
    def _merge_playbooks(self, spec):
        if "playbook" not in spec:
            return
        if "playbook" not in self.spec:
            self.spec["playbook"] = spec["playbook"]
            return
        self._merge_named_list(self.spec["playbook"], spec["playbook"],
                               self._name_of, self._merge_playbook_entry)

    # direction: asking file wins; 'tasks' additive, not merged by name
    # (docs/merging.md).
    def _merge_playbook_entry(self, entry, newentry):
        self._merge_settings(entry, newentry, appends=lambda s: s == "tasks")

    # Packages are merged by the source package they name, the way
    # partitions are merged by label: a file may say what to build and
    # another how, without either having to restate the other.
    #
    # What that is for is a kernel: which patches a tree needs, what
    # upstream it is, what flavour to cut the build down to are none of
    # them properties of the release being built for, and writing them
    # once per suite is how the copies drift apart.
    #
    # A setting already there wins, as it does for partitions: 'requires'
    # loads what a file asked for after the file itself, so the
    # specification reaching for a fragment is the one that overrides it.
    #
    # Named-entry merge, generic over what "named" means: an entry from
    # 'new' matching one already in 'existing' (by name_of()) is folded
    # into it with merge_entry() instead of duplicating it; one with no
    # match is appended. What began as 'packages:''s own loop.
    #
    # direction: set by the caller's merge_entry -- this helper has none
    # of its own (see docs/merging.md).
    def _merge_named_list(self, existing, new, name_of, merge_entry):
        for entry in new:
            name = name_of(entry)
            match = [e for e in existing if name is not None and name_of(e) == name]
            if len(match) == 0:
                existing.append(entry)
            else:
                merge_entry(match[0], entry)

    # One entry's settings, first-loaded-wins for anything already
    # present, unless appends(setting) says the two should add together
    # instead (see _added()). 'skip' is whatever the caller already
    # merged its own way (a nested named list, say).
    #
    # direction: asking file wins -- every caller of this (packages,
    # playbook, test) inherits it (see docs/merging.md).
    def _merge_settings(self, entry, newentry, appends=lambda setting: False, skip=()):
        for setting in newentry:
            if setting == BuildCmd.ORIGINS or setting in skip:
                continue
            if appends(setting) and setting in entry:
                entry[setting] = self._added(entry[setting], newentry[setting])
                self._take_origin(entry, newentry, setting)
            elif setting not in entry:
                entry[setting] = newentry[setting]
                self._take_origin(entry, newentry, setting)

    # The name_of() every _merge_named_list() caller that matches by a
    # plain 'name' field can share -- packages matches by source package
    # name instead (its own callback), but playbook/test entries and any
    # future named-list section need nothing more than this.
    def _name_of(self, entry):
        if type(entry) != type({}):
            return None
        name = entry.get("name")
        return name if type(name) == type("") else None

    # direction: asking file wins (docs/merging.md).
    def _merge_packages(self, spec):
        if "packages" not in spec:
            return
        if "packages" not in self.spec:
            self.spec["packages"] = spec["packages"]
            return
        self._merge_named_list(self.spec["packages"], spec["packages"],
                               self._package_name, self._merge_package)

    # A package entry under 'defaults' describes a package without asking
    # for it to be built: it is what an architecture file needs to say
    # which kernel flavour is meant without conjuring a kernel rebuild into
    # every image that includes it.
    #
    # The last file to describe a package wins, which is the opposite of
    # 'packages' and is what makes them useful: files are listed from the
    # general to the particular, so a board file gets the last word over
    # the architecture file it sits on. Anything under 'packages' still
    # beats every default, since that is the file doing the asking.
    def _merge_defaults(self, spec):
        if "defaults" not in spec:
            return
        defaults = spec["defaults"]
        if type(defaults) != type({}):
            raise ValueError("'defaults' shall be a dictionary!")
        for setting in defaults:
            if setting != "packages":
                raise ValueError(
                    "'defaults' holds package entries only, not '%s'" % setting)

        merged = self.spec.setdefault("defaults", {}).setdefault("packages", [])
        for package in defaults.get("packages") or []:
            name = self._package_name(package)
            existing = [p for p in merged if self._package_name(p) == name]
            if name is None or len(existing) == 0:
                merged.append(package)
            else:
                self._override_package(existing[0], package)

    # As _merge_package(), with the two files the other way round: what the
    # later one says replaces what the earlier one did.
    def _override_package(self, package, newpackage):
        for setting in newpackage:
            if setting == BuildCmd.ORIGINS:
                continue
            if setting == "extends" and type(package.get(setting)) == type({}):
                for kind in newpackage[setting]:
                    if type(package[setting].get(kind)) != type({}):
                        package[setting][kind] = newpackage[setting][kind]
                    else:
                        for name, value in (newpackage[setting][kind] or {}).items():
                            if self._appends(kind, name):
                                value = self._added(
                                    package[setting][kind].get(name), value,
                                    kind, name)
                            package[setting][kind][name] = value
                    for name in newpackage[setting][kind] or []:
                        self._take_origin(package, newpackage,
                                          "extends.%s.%s" % (kind, name))
            else:
                package[setting] = newpackage[setting]
                self._take_origin(package, newpackage, setting)

    # Folded into the packages the specification asked for, once every file
    # has been read. A default naming a package nothing builds describes
    # nothing and is dropped -- which is the whole point of it -- but it is
    # parsed first, so a typo in an architecture file is reported by the
    # file that holds it rather than by the one image that happens to build
    # a kernel.
    def _apply_defaults(self):
        from seine.packages import Package

        defaults = (self.spec.pop("defaults", None) or {}).get("packages") or []
        for index, default in enumerate(defaults):
            Package(default, index)
            self._drop_unbuilt_kernels(default)
            name = self._package_name(default)
            for package in self.spec.get("packages") or []:
                if self._package_name(package) == name:
                    self._merge_package(package, default)

    # A description may name a kernel this specification does not build,
    # and that is not a mistake: an architecture file says "if a kernel of
    # our own is built, put modules on it too", and it is included by every
    # image of that architecture, most of which build no kernel. So a
    # kernel named here that nothing builds is dropped, exactly as the
    # description itself is when nothing asks for the package. Under
    # 'packages' it stays an error, as it already is for 'before'.
    #
    # Only bare names are dropped: an 'apt://' kernel is the
    # distribution's and is built by nobody here.
    def _drop_unbuilt_kernels(self, default):
        module = (default.get("extends") or {}).get("module")
        if type(module) != type({}):
            return
        built = {self._package_name(package)
                 for package in self.spec.get("packages") or []}
        for setting, kernels in module.items():
            if self._appends("module", setting) == False:
                continue
            if type(kernels) != type([]):
                continue
            module[setting] = [
                kernel for kernel in kernels
                if type(kernel) != type("") or "://" in kernel
                or kernel in built]

    # direction: asking file wins, 'extends:' recurses the same way
    # (docs/merging.md).
    def _merge_package(self, package, newpackage):
        skip = set()
        if "extends" in newpackage and type(package.get("extends")) == type({}):
            self._merge_extends(package["extends"], newpackage["extends"],
                                package, newpackage)
            skip.add("extends")
        self._merge_settings(package, newpackage, skip=skip)

    # A setting and the file that wrote it move together. Taking a setting
    # whole takes what is under it: copying an 'extends' block no file had
    # yet copies where each of its settings was written, not one answer
    # for the block.
    def _take_origin(self, package, newpackage, setting):
        origins = newpackage.get(BuildCmd.ORIGINS) or {}
        taken = {name: origin for name, origin in origins.items()
                 if name == setting or name.startswith("%s." % setting)}
        if len(taken) > 0:
            package.setdefault(BuildCmd.ORIGINS, {}).update(taken)

    # One level deeper than the rest: 'extends' is a dictionary of kinds,
    # and two files describing the same kernel are describing the same
    # 'kernel' entry rather than replacing each other's.
    def _merge_extends(self, extends, newextends, package, newpackage):
        if type(newextends) != type({}):
            return
        for kind in newextends:
            if type(extends.get(kind)) != type({}) or type(newextends[kind]) != type({}):
                if kind not in extends:
                    extends[kind] = newextends[kind]
                    for setting in newextends[kind] or []:
                        self._take_origin(package, newpackage,
                                          "extends.%s.%s" % (kind, setting))
                continue
            for setting in newextends[kind]:
                if self._appends(kind, setting):
                    extends[kind][setting] = self._added(
                        extends[kind].get(setting), newextends[kind][setting],
                        kind, setting)
                    self._take_origin(package, newpackage,
                                      "extends.%s.%s" % (kind, setting))
                elif setting not in extends[kind]:
                    extends[kind][setting] = newextends[kind][setting]
                    self._take_origin(package, newpackage,
                                      "extends.%s.%s" % (kind, setting))

    # Settings that two files add to rather than settle between them.
    #
    # Which kernels a module is built against is one of them: the file
    # asking for the module names the kernels it knows about, and the file
    # building a kernel of its own adds that one. Neither says the same
    # thing twice, so "what was said first stands" would quietly drop a
    # kernel somebody asked to have modules for.
    #
    # 'derived-flavours' is the other: a board file builds on a base an
    # architecture file already derives, and "what was said first
    # stands" would drop the second file's base entirely.
    #
    # 'configs' is the same idea, one level flatter: a file naming a
    # group another file also named on the same kernel entry is two
    # requests for that kernel, not one description repeated -- the gap
    # a real session hit (build/chats/20260820T080504625424.json), where
    # the second file's whole 'configs:' was dropped rather than merged.
    def _appends(self, kind, setting):
        from seine.module import MODULE_KERNELS
        if kind == "module" and MODULE_KERNELS.match(setting) is not None:
            return True
        return kind == "kernel" and setting in ("derived-flavours", "configs")

    # Two lists, in the order they were written, without repeating what
    # both of them named -- the same kernel added by an architecture file
    # and by the file asking for the module is one kernel; 'configs'
    # merged one group at a time, the same reasoning one level flatter;
    # or 'derived-flavours', merged one base at a time so a second file
    # naming a flavour under a base the first already used adds to it
    # rather than replacing it.
    def _added(self, listed, added, kind=None, setting=None):
        if kind == "kernel" and setting == "configs":
            return self._added_configs(listed or {}, added or {})
        if type(listed) == type({}) or type(added) == type({}):
            if type(listed) != type({}) or type(added) != type({}):
                return added if type(added) == type({}) else listed
            merged = {base: dict(names) for base, names in listed.items()}
            for base, names in added.items():
                merged.setdefault(base, {}).update(names)
            return merged
        if type(listed) != type([]) or type(added) != type([]):
            return added if type(added) == type([]) else listed
        return listed + [entry for entry in added if entry not in listed]

    # 'configs' groups are name to a list of lines, not name to a
    # dictionary like 'derived-flavours' -- a group named by both files
    # merges its own two line lists the way a plain list would, rather
    # than the second replacing the first outright the way a repeated
    # 'derived-flavours' name does.
    def _added_configs(self, listed, added):
        merged = {group: list(lines) for group, lines in listed.items()}
        for group, lines in added.items():
            current = merged.setdefault(group, [])
            merged[group] = current + [line for line in lines if line not in current]
        return merged

    # What decides whether two entries are the same package: the name it
    # was given, or failing that the source package its 'source' URI
    # names. Kept simple on purpose: the URI is parsed properly later, and
    # a name that comes out wrong here merges nothing that was not already
    # separate.
    #
    # 'name' first, because it is the package's name and the URI only says
    # where the source came from: a file adding to a package described
    # elsewhere then names it, rather than repeating a git URI complete
    # with a revision it has no opinion about.
    def _package_name(self, package):
        if type(package) != type({}):
            return None
        if type(package.get("name")) == type(""):
            return package["name"]
        if type(package.get("source")) != type(""):
            return None
        _, _, rest = package["source"].partition("://")
        rest = rest.split(";")[0].partition("=")[0]
        return os.path.basename(rest).removesuffix(".git").split("_")[0]

    def _lookup_named_part_or_vol(self, parts, label, kind):
        for part in parts:
            if part["label"] == label:
                return part
        return None

    def _update_named_part_or_vol(self, parts, newpart, kind):
        index = 0
        for part in parts:
            if part["label"] == newpart["label"]:
                parts[index] = newpart
            index = index + 1
        return parts

    def _merge_part_flags(self, part, newpart):
        for flag in newpart["flags"]:
            if flag.startswith("~"):
                flag = flag[1:]
                if flag in part["flags"]:
                    part["flags"].remove(flag)
            else:
                if not flag in part["flags"]:
                    part["flags"].append(flag)
        return part

    def _merge_part_or_vol(self, part, newpart, kind):
        for setting in newpart:
            if setting == "flags":
                if "flags" in part:
                    part = self._merge_part_flags(part, newpart)
                else:
                    part["flags"] = []
                    for flag in newpart["flags"]:
                        if not flag.startswith("~"):
                            part["flags"].append(flag)
            elif setting not in part:
                part[setting] = newpart[setting]
        return part

    def _merge_parts_or_vols(self, spec, kind):
        parts = self.spec["image"][kind]
        for newpart in spec["image"][kind]:
            part = self._lookup_named_part_or_vol(parts, newpart["label"], kind)
            if part is None:
                parts.append(newpart)
            else:
                part = self._merge_part_or_vol(part, newpart, kind)
                parts = self._update_named_part_or_vol(parts, part, kind)
        self.spec["image"][kind] = parts

    def _merge_image(self, spec):
        if "image" in self.spec:
            for setting in spec["image"]:
                if (setting == "partitions" or setting == "volumes") and (setting in self.spec["image"]):
                    self._merge_parts_or_vols(spec, setting)
                else:
                    self.spec["image"][setting] = spec["image"][setting]
        elif "image" not in self.spec:
            self.spec["image"] = spec["image"]

    # What a fragment asked not to print, gathered from every file rather
    # than taken from the last one to say it: the fragment that holds a
    # secret is the fragment that knows it is one, and it is rarely the
    # file a build is started from.
    def _merge_redact(self, spec):
        for pattern in spec.get("redact") or []:
            patterns = self.spec.setdefault("redact", [])
            if pattern not in patterns:
                patterns.append(pattern)

    def merge(self, spec):
        self._merge_redact(spec)
        self._merge_distro(spec)
        self._merge_imager(spec)
        self._merge_defaults(spec)
        self._merge_packages(spec)
        self._merge_playbooks(spec)
        if "image" in spec:
            self._merge_image(spec)
        return self.spec

    def parse(self):
        if self.image is None:
            self.image = Image(self.partitionHandler, self.options)
        self._apply_defaults()
        self.raw_spec = copy.deepcopy(self.spec)
        self.spec = self.partitionHandler.parse(self.spec)
        self.spec = self.image.parse(self.spec)
        return self.spec

    def build(self, reporter=None):
        if self.spec is None or self.image is None:
            raise RuntimeError("no specification was loaded or parsed!")
        return self.image.build(reporter=reporter)

    # The intermediate images of the multi-stage builds this made, once,
    # when there is nothing left to stand on them -- rather than after
    # every image, which is what it was.
    #
    # 'podman image prune' is machine-wide, so it is taken exclusively and
    # skipped when it is not free: another build holds the storage, has
    # intermediates of its own, and prunes when it finishes.
    def _prune(self):
        try:
            with locked(ContainerEngine.storage_lock(), blocking=False):
                ContainerEngine.run(["image", "prune", "-f"], check=False)
        except BlockingIOError:
            pass

    # The merged specification as YAML, without what only seine needs. On a
    # copy: what is hidden from a reader is still what the build walks.
    def dump(self, spec):
        spec = copy.deepcopy(spec)
        if "image" in spec:
            # hide internal attributes (_foo) but also "priority" settings
            # from "partitions" and "volumes" sections
            for what in [ "partitions", "volumes" ]:
                if what not in spec["image"]:
                    continue
                objects = []
                for o in spec["image"][what]:
                    kvp = {}
                    for k in o:
                        if k.startswith("_") == False and k != "priority":
                            kvp[k] = o[k]
                    objects.append(kvp)
                spec["image"][what] = objects

        if "packages" in spec:
            # hide internal attributes (_foo) and the "priority" settings,
            # as done above for partitions and volumes
            packages = []
            for p in spec["packages"]:
                packages.append({k: v for k, v in p.items()
                                 if k.startswith("_") == False and k != "priority"})
            spec["packages"] = packages

        if "playbook" in spec:
            # hide "hosts" settings from playbooks since they are added by
            # us to make ansible happy
            playbooks = []
            for p in spec["playbook"]:
                p.pop("hosts", None)
                playbooks.append(p)
            spec["playbook"] = playbooks

        # hide the "requires" section since YAML files were supposedly merged
        # together and we now have a consolidated specification
        spec.pop("requires", None)

        # take out what the specification asked not to print, everywhere it
        # appears. The section itself is left as it is: its patterns say
        # what a reader is not being shown, and one of them matching itself
        # would hide that too.
        patterns = redactions(spec)
        for section in spec:
            if section != "redact":
                spec[section] = redact(spec[section], patterns)

        # return the spec in YAML format
        return yaml.dump(spec)

    # One file's own text, not the merged spec dump() returns -- redacted
    # the same patterns, but never the literal bytes on disk. Refused
    # unless in loaded_files or 'extra_allowed' (e.g. spec-files' unloaded
    # siblings). Read as written, not rendered -- no Jinja substitution,
    # so '{{ }}' shows up verbatim.
    def dump_file(self, path, extra_allowed=()):
        real = os.path.realpath(path)
        if real not in self.loaded_files and real not in extra_allowed:
            raise ValueError("%s is not one of this build's own loaded files" % path)
        with open(real, "r") as f:
            try:
                spec = yaml.safe_load(f.read()) or {}
            except yaml.YAMLError as e:
                raise ValueError("%s: %s" % (path, e)) from e
        patterns = redactions(self.spec)
        return yaml.dump(redact(spec, patterns))

    # Every local file this build's 'packages:' entries reference --
    # patches, kernel fragments, derived-flavour fragments -- the same
    # files a real build reads to compile them. Never
    # 'defaults.packages:', a description names nothing built. Read
    # fresh, not cached, so a spec-update mid-conversation shows up now.
    def referenced_files(self):
        from seine import packages
        try:
            parsed = packages.parse(self.spec)
        except ValueError:
            return set()
        return {os.path.realpath(f) for p in parsed for f in p.referenced_files()}

    # Falls back to a referenced file (a patch, a kernel fragment) when
    # dump_file() refuses -- read as written, redacted as flat text via
    # redact() (no YAML round-trip needed). No path-containment check:
    # a real build already reads and ships whatever a 'patches:'/
    # 'fragments:' entry names, a bigger exposure than this preview.
    def read(self, path, extra_allowed=()):
        try:
            return self.dump_file(path, extra_allowed=extra_allowed)
        except ValueError:
            pass
        real = os.path.realpath(path)
        if real not in self.referenced_files():
            raise ValueError(
                "%s is not one of this build's own loaded files, siblings, "
                "or a local file a 'packages:' entry references" % path)
        with open(real, "r") as f:
            text = f.read()
        return redact(text, redactions(self.spec))


    # The same, marked with what changed since these files last built. With
    # no baseline nothing is marked, and stderr says why -- stdout carries
    # the specification, whatever is reading it.
    def changed(self, files, spec):
        baseline = recall(files)
        if baseline is None:
            sys.stderr.write(
                "nothing was built from %s here yet, so there is nothing to "
                "compare this against\n" % ", ".join(files))
        return diff(baseline, self.dump(spec),
                    color=colorless(self.options) == False)

    def usage(self):
        return USAGE

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(self.usage())
            sys.exit(1)
        for o, a in opts:
            if o in ("-d", "--debug"):
                self.options["debug"] = True
                self.options["verbose"] = True
            elif o in ("-h", "--help"):
                print(self.usage())
                sys.exit()
            elif o in ("-j", "--jobs"):
                # How many steps of a build may run at once. One by
                # default: that is the order and the output a build has
                # always had, and the only thing that makes a failure
                # readable without going looking for a file.
                try:
                    self.options["jobs"] = int(a)
                except ValueError:
                    sys.stderr.write("error: --jobs expects a number\n")
                    sys.exit(1)
                if self.options["jobs"] < 1:
                    sys.stderr.write("error: --jobs shall be at least 1\n")
                    sys.exit(1)
            elif o in ("-k", "--keep"):
                self.options["keep"] = True
            elif o in ("--no-color"):
                self.options["color"] = False
            elif o in ("--spec-only"):
                self.options["tasks"] = False
            elif o in ("--tasks-only"):
                self.options["spec"] = False
            elif o in ("--packages-only"):
                self.options["packages_only"] = True
            elif o in ("--rootfs-only"):
                self.options["rootfs_only"] = True
            elif o in ("--target"):
                # Checked against the graph itself once it exists
                # (Image.tasks(), via tasks.closure()) rather than here --
                # a task's name depends on what the specification asks
                # for (which package, which architecture), which is not
                # known yet from the command line alone.
                self.options["target"] = a
            elif o in ("--dry-run"):
                self.options["dry_run"] = True
            elif o in ("-D", "--dump"):
                self.options["build"] = False
            elif o in ("--parallel"):
                # How many cores one package build may use. Left unset it
                # follows --jobs, so raising that divides the machine
                # rather than multiplying it: N builds each helping
                # themselves to every core is how a build machine dies.
                try:
                    self.options["parallel"] = int(a)
                except ValueError:
                    sys.stderr.write("error: --parallel expects a number\n")
                    sys.exit(1)
                if self.options["parallel"] < 1:
                    sys.stderr.write("error: --parallel shall be at least 1\n")
                    sys.exit(1)
            elif o in ("--require-hashes"):
                self.options["require_hashes"] = True
            elif o in ("--rebuild"):
                self.options["rebuild"] = True
            elif o in ("--sign-key"):
                self.options["sign_key"] = a
            elif o in ("--sbom"):
                self.options["sbom"] = True
            elif o in ("-v", "--verbose"):
                self.options["verbose"] = True
            else:
                assert False, "unhandled option"

        if len(args) == 0:
            sys.stderr.write("error: %s command expects a YAML file\n" % self.NAME)
            sys.exit(1)

        try:
            # '--' separates groups of files: several images, one
            # scheduler. A single group -- no '--' anywhere -- takes the
            # path below exactly as it always has; multiconfig.run() has
            # its own copy of what follows it, for more than one.
            from seine import multiconfig
            groups = multiconfig.split(args)
            if len(groups) > 1:
                sys.exit(multiconfig.run(groups, self.options))

            self.options["files"] = args
            self.load_all(args)

            spec = self.parse()
            result = 0
            if self.options["build"] == False:
                print(self.dump(spec))
            elif self.options["dry_run"]:
                # What a build would build, then how. No lock is taken and
                # nothing is pruned: a dry run writes no storage, so it has
                # nothing to wait for.
                if self.options["spec"]:
                    print(self.changed(args, spec))
                if self.options["tasks"]:
                    result = self.build()
            else:
                # Taken before the build, which writes into the
                # specification as it goes -- the ansible runner puts each
                # playbook's environment there. Taken after, every playbook
                # would differ from the one a plan renders.
                recorded = self.dump(spec)
                # Shared: a build started in another terminal runs beside
                # this one, which is what a machine with cores to spare is
                # for. What may not run beside it is something that sweeps
                # the storage -- 'seine cache clear', and the prune below.
                with locked(ContainerEngine.storage_lock(), shared=True):
                    result = self.build()
                self._prune()
                # What the next plan compares against, recorded only for a
                # build that finished: build() returns None on success and
                # the code it failed with otherwise.
                if not result:
                    remember(args, recorded)
            sys.exit(result)

        except OSError as e:
            sys.stderr.write("error: couldn't open build YAML file: {0}\n".format(e))
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: YAML file is invalid: {0}\n".format(e))
            sys.exit(3)
        except subprocess.CalledProcessError as e:
            sys.stderr.write("error: build failed: {0}\n".format(e))
            sys.exit(4)
        # 128 plus SIGINT, as a shell reports it. A second Ctrl-C arrives as
        # a KeyboardInterrupt: the same answer, without the traceback.
        except (Interrupted, KeyboardInterrupt) as e:
            sys.stderr.write("error: build was %s\n" % (str(e) or "interrupted"))
            sys.exit(130)

# Everything 'build' does up to the point of doing it. The same command with
# one option decided for it, rather than a second implementation of it: what
# a plan is worth depends on it being the graph a build would walk, and two
# code paths would be two answers.
class PlanCmd(BuildCmd):
    NAME = "plan"

    # What is left says how the plan is printed, not what is in it. For the
    # plan of a build with particular options, 'seine build --dry-run'
    # still takes them all.
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "no-color", "spec-only", "tasks-only"]

    def __init__(self):
        super().__init__()
        self.options["dry_run"] = True

    def usage(self):
        return PLAN_USAGE

USAGE = """
Build an image using instructions from specifications files

Description:
  Builds an Embedded Linux image using instructions from one or more specification
  files defining the base distribution and the Ansible playbooks to execute to
  customize the image.

Usage:
  seine build [options] SPEC... [-- SPEC...]...

  '--' separates groups of specification files, each the same thing a
  single 'seine build' already takes -- one image per group, several
  built together under one scheduler, sharing what their specifications
  agree on: see 'Building several images together' in docs/building.md.

Examples:
  seine build demo-image.yml
  seine build -v demo-image.yml
  seine build pc-image.yml -- rpi4-image.yml

Flags:
  -d, --debug           print debug messages
  --dry-run             do not build anything, print the steps the build would
                        run and the packages it would leave alone
  -D, --dump            do not build the image, just dump the consolidated specification
  -h, --help            print this message
  -j, --jobs N          run up to N steps of the build at once (1 by default).
                        Steps that depend on each other still wait; what a
                        step's containers print goes to a file of its own
                        while more than one is running
  -k, --keep            keep temporary files
      --no-color        print the specification of a '--dry-run' without
                        colour. NO_COLOR says the same thing, and a plan
                        going anywhere but a terminal is plain anyway
      --packages-only   build the packages of the 'packages' section and stop,
                        without assembling a root file-system or writing an
                        image. What a machine filling a cache for others to
                        import runs, since the packages are the half worth
                        carrying
      --parallel N      cores one package build may use. Unset, it is derived
                        from --jobs so that the builds running together do not
                        ask for more of the machine than it has
  --sign-key KEY        sign the rebuilt packages and the repository holding
                        them with this gpg key, named however gpg will take it
                        -- a key id, a fingerprint, an email address. gpg runs
                        on this machine and talks to your agent, so seine
                        never sees the key itself. SEINE_SIGN_KEY says the
                        same thing
  --rebuild             rebuild the packages of the 'packages' section even if
                        they were built before
  --require-hashes      refuse to build when a source is fetched over http with
                        no sha256 to check it against. Reported when the
                        specification is parsed, before anything is downloaded
  --rootfs-only         build the root file-system as a tarball and stop,
                        without writing a disk image. What looking inside a
                        build rather than booting it wants
  --sbom                produce a Software Bill of Materials (SBOM) using
                        debsbom
  --spec-only           with '--dry-run', print the specification and not the
                        steps
  --target TASK         build just this one task and whatever it needs (as
                        'plan' names them, e.g. 'package:linux') and stop --
                        note this is what it needs, not what needs it, so a
                        package alone does not reach the repository, which
                        is 'deploy:<name>' 's job
  --tasks-only          with '--dry-run', print the steps and not the
                        specification
  -v, --verbose         produce verbose output while building the image

"""

PLAN_USAGE = """
Say what a build would do, without doing any of it

Description:
  Prints the specification these files merge into, and then the steps a build
  of it would run, in the order it would run them and with what each waits
  for, and the packages it would leave alone with the stamp that says why.

  The specification is printed as a diff against the one these same files
  last built, so that what changed since is what stands out: added lines on
  green, removed lines on red, and what did not change folded away around
  them. Only a build records one, so files that have not built here before
  have nothing to compare against: their specification is printed as it is,
  and stderr says why nothing in it is marked.

  The plan is not a description of the build: it is the same graph a build
  walks, printed instead of walked. So a package already built from exactly
  these inputs has no steps in it at all -- which is the useful half of the
  answer.

  Nothing is fetched, built or written. 'seine build --dry-run' is the same
  thing.

Usage:
  seine plan [options] SPEC... [-- SPEC...]...

  '--' groups specification files the same way 'seine build' takes them;
  see there for what running several together means.

Examples:
  seine plan demo-image.yml
  seine plan --spec-only demo-image.yml
  seine plan pc-image.yml -- rpi4-image.yml

Flags:
  -h, --help            print this message
      --no-color        print the specification without colour. NO_COLOR says
                        the same thing, and a plan going anywhere but a
                        terminal is plain anyway
      --spec-only       print the specification and not the steps
      --tasks-only      print the steps and not the specification

  And nothing else: these say how the plan is printed, not what is in it. A
  plan is the same whoever asks for it. For the plan of a build with
  particular options, 'seine build --dry-run' takes all of them.

"""
