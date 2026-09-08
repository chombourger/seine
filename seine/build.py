# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import copy
import getopt
import jinja2
import jinja2.meta
import os
import re
import subprocess
import sys
import yaml

from seine            import settings
from seine.image      import Image
from seine            import module
from seine.cmd        import Cmd
from seine.partition  import PartitionHandler
from seine.tasks      import Interrupted
from seine.container import ContainerEngine
from seine.utils import distribution, locked
from seine.utils      import lock_sibling, redact, redactions
from seine.diffing    import colorless, diff, recall, remember

# Specs render before parsing (one file covers several
# archs/releases). Custom delimiters keep ansible's own '{{ }}'
# untouched. StrictUndefined: a missing value fails loudly, not
# silently building for the wrong machine.
TEMPLATE = jinja2.Environment(
    variable_start_string="[[", variable_end_string="]]",
    block_start_string="[%", block_end_string="%]",
    comment_start_string="[#", comment_end_string="#]",
    trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined)

# Same, but for the probe pass: unresolved names render empty instead
# of raising, since this output is only used to collect what names
# get set, then thrown away.
PROBE = TEMPLATE.overlay(undefined=jinja2.ChainableUndefined)

# Parses "CLASS=N[,CLASS=N...]" into a {class: capacity} dict, merged
# onto 'previous'. Shared by the CLI and the TUI settings.
def parse_resources(text, previous=None):
    resources = dict(previous or {})
    for entry in text.split(","):
        entry = entry.strip()
        if not entry:
            continue
        cls, _, value = entry.partition("=")
        try:
            capacity = int(value)
        except ValueError:
            raise ValueError("expects CLASS=N, got '%s'" % entry)
        if capacity < 1:
            raise ValueError("capacity shall be at least 1 ('%s')" % entry)
        resources[cls] = capacity
    return resources

def format_resources(resources):
    return ",".join("%s=%d" % (cls, cap)
                    for cls, cap in sorted((resources or {}).items()))

class BuildCmd(Cmd):
    # Command name and its '-h' text. A variant of this command (see
    # PlanCmd) overrides these instead of copying main().
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
        "resource=",
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
        # 'jobs' falls back to the persisted setting (see settings.py / '/set
        # jobs N') before the hardcoded '1'; '-j'/'--jobs' below overrides both.
        self.options = { "ansible_library": [], "build": True, "color": None,
                         "debug": False, "dry_run": False,
                         "jobs": settings.load().get("jobs") or 1, "keep": False,
                         "packages_only": False, "parallel": None,
                         "rebuild": False, "require_hashes": False,
                         "resources": settings.load().get("resources"),
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
        # Every real file loaded, in order first reached. Unlike _loading,
        # never popped -- dump_file() checks paths against this.
        self.loaded_files = []
        self._probing = False
        self._variables = None
        self._names = []
        self._prober = None
        self._probed = set()
        # 'multiconfig:' groups this specification declares, name -> the
        # BuildCmd that parsed it -- see _parse_multiconfig(). Empty for
        # a specification with none.
        self.subbuilds = {}

    # A spec passed in as text rather than a file to walk: nothing to
    # probe, so it renders against the spec merged so far.
    def loads(self, yaml_spec):
        return self._load("<string>", yaml_spec)

    # Probes every named file before loading any -- a file may need a
    # name a later file sets. Each file's lock sibling (foo.yaml ->
    # foo.lock.yaml) is auto-spliced in right after it.
    def load_all(self, yaml_files):
        expanded = []
        for yaml_file in yaml_files:
            expanded.append(yaml_file)
            lock = lock_sibling(yaml_file)
            if lock is not None and os.path.isfile(lock):
                expanded.append(lock)
        for yaml_file in expanded:
            self._probe(yaml_file, check=False)
        self._check_names(self._prober._names if self._prober else [])
        for yaml_file in expanded:
            self.load(yaml_file)
        return self.spec

    # Learns what a spec sets before loading it for real, so ordering
    # between files doesn't matter for name lookups. Uses a lenient jinja
    # pass and throws away its output -- only the discovered names survive.
    def _probe(self, yaml_file, check=True):
        if self._prober is None:
            self._prober = BuildCmd()
            self._prober._probing = True
        self._prober.load(yaml_file)
        self._probed.add(os.path.realpath(yaml_file))
        self._variables = self._prober.spec or {}
        if check:
            self._check_names(self._prober._names)

    # Reports every unset name asked for, all at once rather than one
    # error per run.
    def _check_names(self, names):
        missing = []
        for filename, asked in names:
            for name in sorted(asked - set(self._variables or {})):
                missing.append("%s: '%s' is not set by this specification"
                               % (filename, name))
        if len(missing) > 0:
            raise ValueError("\n".join(missing))

    # Tracks the requires-chain to catch loops (two files requiring each
    # other used to recurse until the stack blew, with no useful
    # traceback). A file reached twice via different paths is not a loop
    # and loads again, on purpose.
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

    # Rendered against the spec built so far, so a fragment can read what
    # reached for it. Only '[[ ]]' substitutions are allowed, not '[% %]'
    # blocks -- 'requires:' is how a spec branches, kept readable without
    # running it.
    BLOCKS = re.compile(re.escape(TEMPLATE.block_start_string))

    # 'requires:' is stripped from the template text before rendering, so a
    # require can't itself be templated (else which files load would
    # depend on the render, which the files decide).
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

    # Takes raw text, not a stream, so loads() and load() can share this.
    # A YAML error while probing is swallowed (the lenient render may have
    # left a name empty) -- the real load below reports it properly.
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

        # Patch/kconfig paths are relative to the file listing them; resolve
        # here while we still know which file that was.
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
            self.merge(spec, peer=len(self._loading) <= 1)

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

    # Which file wrote each of a package's settings (a package is often
    # described by several files). Nested settings use a dotted path
    # ('extends.kernel.upstream'); stored under an '_'-prefixed key so
    # dump() already hides it.
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

    # direction: most-specific file wins (docs/merging.md).
    def _merge_distro(self, spec):
        if "distribution" in spec:
            if "distribution" in self.spec:
                for setting in spec["distribution"]:
                    if setting == "feeds":
                        self._merge_feeds(spec["distribution"]["feeds"])
                        continue
                    if setting == "architectures":
                        self._merge_distro_architectures(
                            spec["distribution"]["architectures"])
                        continue
                    self.spec["distribution"][setting] = spec["distribution"][setting]
            elif "distribution" not in self.spec:
                self.spec["distribution"] = spec["distribution"]

    # Feeds merge by suite (like partitions/volumes merge by label), so
    # adding one feed doesn't require restating the others.
    #
    # direction: most-specific file wins, per setting within a matched
    # suite (docs/merging.md).
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

    # Unlike other 'distribution:' settings, 'architectures' (plural)
    # accumulates across fragments instead of the last one winning.
    # 'architecture' (singular, this run's target) still last-wins.
    #
    # direction: additive, deduplicated (docs/merging.md).
    def _merge_distro_architectures(self, architectures):
        for arch in architectures:
            archs = self.spec["distribution"].setdefault("architectures", [])
            if arch not in archs:
                archs.append(arch)

    # direction: most-specific file wins (docs/merging.md).
    def _merge_imager(self, spec):
        if "imager" in spec:
            if "imager" in self.spec:
                for setting in spec["imager"]:
                    self.spec["imager"][setting] = spec["imager"][setting]
            else:
                self.spec["imager"] = spec["imager"]

    # A group's file list is replaced outright when named again, not
    # extended -- a board file fully overrides what a shared fragment
    # asked a group to load.
    #
    # direction: most-specific file wins (docs/merging.md).
    def _merge_multiconfig(self, spec):
        if "multiconfig" in spec:
            if "multiconfig" in self.spec:
                for name in spec["multiconfig"]:
                    self.spec["multiconfig"][name] = spec["multiconfig"][name]
            else:
                self.spec["multiconfig"] = spec["multiconfig"]

    # Merged by name so a fragment reached twice via two 'requires:'
    # paths doesn't duplicate its playbook entry. 'tasks:' stays additive
    # (order matters for ansible), not merged task-by-task.
    #
    # direction: asking file wins within 'requires:'; a peer file amends
    # by field instead (docs/merging.md).
    def _merge_playbooks(self, spec, peer=False):
        if "playbook" not in spec:
            return
        if "playbook" not in self.spec:
            self.spec["playbook"] = spec["playbook"]
            return
        self._merge_named_list(self.spec["playbook"], spec["playbook"],
                               self._name_of,
                               lambda e, n: self._merge_playbook_entry(e, n, peer=peer))

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'tasks' additive either way (docs/merging.md).
    def _merge_playbook_entry(self, entry, newentry, peer=False):
        self._merge_settings(entry, newentry, appends=lambda s: s == "tasks", peer=peer)

    # Merged by name like 'packages:', so a fragment reached twice via
    # two 'requires:' paths amends its entry instead of duplicating it.
    # See seine.testing for what a 'test:' entry holds and how it runs.
    #
    # direction: asking file wins within 'requires:'; a peer file amends
    # by field instead (docs/merging.md).
    def _merge_tests(self, spec, peer=False):
        if "test" not in spec:
            return
        if "test" not in self.spec:
            self.spec["test"] = spec["test"]
            return
        self._merge_named_list(self.spec["test"], spec["test"],
                               self._name_of,
                               lambda e, n: self._merge_test_entry(e, n, peer=peer))

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'tests'/'keywords' merge by name below, 'keywords' stays
    # identity-or-error either way (docs/merging.md).
    def _merge_test_entry(self, entry, newentry, peer=False):
        skip = set()
        if "tests" in newentry and type(entry.get("tests")) == type([]):
            self._merge_named_list(entry["tests"], newentry["tests"],
                                   self._name_of,
                                   lambda e, n: self._merge_test_case(e, n, peer=peer))
            skip.add("tests")
        if "keywords" in newentry and type(entry.get("keywords")) == type([]):
            self._merge_named_list(entry["keywords"], newentry["keywords"],
                                   self._name_of, self._merge_keyword)
            skip.add("keywords")
        if "variables" in newentry and type(entry.get("variables")) == type({}):
            for name, value in newentry["variables"].items():
                if peer or name not in entry["variables"]:
                    entry["variables"][name] = value
            skip.add("variables")
        self._merge_settings(entry, newentry,
                             appends=lambda s: s in ("library", "tags"), skip=skip,
                             peer=peer)

    # Two 'steps:' for the same case name is likely a mistake, not
    # deliberate composition -- raised rather than silently keeping the
    # first (same identity-or-error rule as _merge_keyword()).
    #
    # direction: asking file wins for settings within 'requires:', peer
    # amends instead; 'steps' is identity-or-error regardless (docs/merging.md).
    def _merge_test_case(self, case, newcase, peer=False):
        if "steps" in newcase and "steps" in case and case["steps"] != newcase["steps"]:
            raise ValueError(
                "test case '%s' is defined differently by two 'test:' "
                "entries -- give one of them a different name if they "
                "are meant to be two cases" % case.get("name"))
        self._merge_settings(case, newcase, appends=lambda s: s == "tags",
                             skip=("steps",) if "steps" in case else (), peer=peer)

    # Two fragments defining the same keyword (reached via two
    # 'requires:' paths) is fine only if identical -- caught here at
    # load time instead of surfacing later when 'seine test' runs.
    #
    # direction: identity-or-error -- never silently overrides
    # (docs/merging.md).
    def _merge_keyword(self, keyword, newkeyword):
        plain = {k: v for k, v in keyword.items() if k != BuildCmd.ORIGINS}
        newplain = {k: v for k, v in newkeyword.items() if k != BuildCmd.ORIGINS}
        if plain != newplain:
            raise ValueError(
                "keyword '%s' is defined differently by two 'test:' "
                "entries -- give one of them a different name if they "
                "are meant to be two keywords" % plain.get("name"))

    # Generic named-entry merge: an entry from 'new' matching one already
    # in 'existing' (by name_of()) is folded into it with merge_entry(),
    # otherwise appended. Shared by packages, playbook, and test merging.
    #
    # direction: set by the caller's merge_entry (docs/merging.md).
    def _merge_named_list(self, existing, new, name_of, merge_entry):
        for entry in new:
            name = name_of(entry)
            match = [e for e in existing if name is not None and name_of(e) == name]
            if len(match) == 0:
                existing.append(entry)
            else:
                merge_entry(match[0], entry)

    # First-loaded-wins per setting, unless appends(setting) says to add
    # together instead (_added()). 'skip' is whatever the caller already
    # merged itself. 'peer' flips the direction for a file reached
    # outside 'requires:', which amends instead of losing.
    #
    # direction: asking file wins (docs/merging.md); peer amends instead.
    def _merge_settings(self, entry, newentry, appends=lambda setting: False,
                        skip=(), peer=False):
        for setting in newentry:
            if setting == BuildCmd.ORIGINS or setting in skip:
                continue
            if appends(setting) and setting in entry:
                entry[setting] = self._added(entry[setting], newentry[setting])
                self._take_origin(entry, newentry, setting)
            elif setting not in entry or peer:
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

    # direction: asking file wins within 'requires:', peer amends
    # instead (docs/merging.md).
    def _merge_packages(self, spec, peer=False):
        if "packages" not in spec:
            return
        if "packages" not in self.spec:
            self.spec["packages"] = spec["packages"]
            return
        self._merge_named_list(self.spec["packages"], spec["packages"],
                               self._package_name,
                               lambda e, n: self._merge_package(e, n, peer=peer))

    # 'vendor:' is a list of asks, merged by name like other named
    # lists. A lock file's 'vendor:' is instead a dict keyed by suite
    # (already-resolved versions) -- told apart by type(), and kept in
    # '_vendor_lock' so existing readers (vendor.py's parse()) still see
    # a plain list.
    #
    # direction: asking file wins within 'requires:', peer amends
    # instead (docs/merging.md).
    def _merge_vendor(self, spec, peer=False):
        if "vendor" not in spec:
            return
        incoming = spec["vendor"]
        if type(incoming) == type({}):
            self.spec.setdefault("_vendor_lock", {}).update(incoming)
            return
        if "vendor" not in self.spec:
            self.spec["vendor"] = incoming
            return
        self._merge_named_list(self.spec["vendor"], incoming,
                               self._vendor_name,
                               lambda e, n: self._merge_settings(e, n, peer=peer))

    def _vendor_name(self, entry):
        if type(entry) != type({}):
            return None
        name = entry.get("name")
        return name if type(name) == type("") else None

    # Additive, deduplicated -- like 'redact': the file that knows a
    # build-dep isn't worth vendoring is rarely the file a vendor run
    # starts from.
    #
    # direction: additive, deduplicated -- order doesn't matter
    # (docs/merging.md).
    def _merge_vendor_exclude(self, spec):
        for name in spec.get("vendor-exclude") or []:
            excluded = self.spec.setdefault("vendor-exclude", [])
            if name not in excluded:
                excluded.append(name)

    # A 'defaults' package entry describes a package without asking to
    # build it (e.g. which kernel flavour is meant, without forcing a
    # rebuild). Last file wins here, unlike 'packages:' -- so a board
    # file overrides the architecture file it sits on.
    #
    # direction: most-specific file wins (docs/merging.md).
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

    # Folds defaults into the packages actually asked for, once every
    # file is read. Parsed first so a typo is reported by the file that
    # has it, not by whichever image happens to build a kernel.
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

    # A default may name a kernel this spec doesn't build -- not a
    # mistake, just "if we build our own kernel, add modules to it too".
    # Drop kernels nothing builds; under 'packages:' that stays an error.
    # Only bare names are dropped, not 'apt://' kernels (the distro's own).
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

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'extends:' recurses the same way (docs/merging.md).
    def _merge_package(self, package, newpackage, peer=False):
        skip = set()
        if "extends" in newpackage and type(package.get("extends")) == type({}):
            self._merge_extends(package["extends"], newpackage["extends"],
                                package, newpackage, peer=peer)
            skip.add("extends")
        self._merge_settings(package, newpackage, skip=skip, peer=peer)

    # A setting and the file that wrote it move together, so copying a
    # whole 'extends' block also copies each nested setting's origin.
    def _take_origin(self, package, newpackage, setting):
        origins = newpackage.get(BuildCmd.ORIGINS) or {}
        taken = {name: origin for name, origin in origins.items()
                 if name == setting or name.startswith("%s." % setting)}
        if len(taken) > 0:
            package.setdefault(BuildCmd.ORIGINS, {}).update(taken)

    # 'extends' is a dict of kinds; two files describing the same kernel
    # describe the same 'kernel' entry rather than replacing each other.
    #
    # direction: asking file wins within 'requires:', peer amends
    # instead, unless _appends() says the setting is additive either way
    # (docs/merging.md).
    def _merge_extends(self, extends, newextends, package, newpackage, peer=False):
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
                elif setting not in extends[kind] or peer:
                    extends[kind][setting] = newextends[kind][setting]
                    self._take_origin(package, newpackage,
                                      "extends.%s.%s" % (kind, setting))

    # Settings two files add to rather than settle between them: which
    # kernels a module targets, and 'kernel: derived-flavours'/'configs'
    # -- "first stands" would silently drop what a second file added.
    def _appends(self, kind, setting):
        from seine.module import MODULE_KERNELS
        if kind == "module" and MODULE_KERNELS.match(setting) is not None:
            return True
        return kind == "kernel" and setting in ("derived-flavours", "configs")

    # Union of two lists (order preserved, no dupes) -- or for dicts,
    # merged key by key: 'configs' one group at a time, 'derived-flavours'
    # one base at a time, rather than the second replacing the first.
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

    # 'configs' maps group name -> list of lines (unlike 'derived-
    # flavours', name -> dict), so a group named by both files unions
    # its two line lists instead of the second replacing the first.
    def _added_configs(self, listed, added):
        merged = {group: list(lines) for group, lines in listed.items()}
        for group, lines in added.items():
            current = merged.setdefault(group, [])
            merged[group] = current + [line for line in lines if line not in current]
        return merged

    # Identifies a package by its 'name', or failing that by parsing its
    # 'source' URI -- kept simple since the URI is parsed properly later,
    # so a wrong guess here just fails to merge, it doesn't merge wrongly.
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

    # direction: additive; a '~flag' removes one a fragment already set
    # (docs/merging.md).
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

    # direction: asking file wins within 'requires:', peer amends
    # instead; 'flags' additive either way (docs/merging.md).
    def _merge_part_or_vol(self, part, newpart, kind, peer=False):
        for setting in newpart:
            if setting == "flags":
                if "flags" in part:
                    part = self._merge_part_flags(part, newpart)
                else:
                    part["flags"] = []
                    for flag in newpart["flags"]:
                        if not flag.startswith("~"):
                            part["flags"].append(flag)
            elif setting not in part or peer:
                part[setting] = newpart[setting]
        return part

    # direction: asking file wins within 'requires:', peer amends
    # instead, matched by 'label' (docs/merging.md).
    def _merge_parts_or_vols(self, spec, kind, peer=False):
        parts = self.spec["image"][kind]
        for newpart in spec["image"][kind]:
            part = self._lookup_named_part_or_vol(parts, newpart["label"], kind)
            if part is None:
                parts.append(newpart)
            else:
                part = self._merge_part_or_vol(part, newpart, kind, peer=peer)
                parts = self._update_named_part_or_vol(parts, part, kind)
        self.spec["image"][kind] = parts

    # direction: most-specific file wins for a plain setting;
    # 'partitions'/'volumes' route to _merge_parts_or_vols instead (asking
    # file wins within 'requires:', matched by 'label') (docs/merging.md).
    def _merge_image(self, spec, peer=False):
        if "image" in self.spec:
            for setting in spec["image"]:
                if (setting == "partitions" or setting == "volumes") and (setting in self.spec["image"]):
                    self._merge_parts_or_vols(spec, setting, peer=peer)
                else:
                    self.spec["image"][setting] = spec["image"][setting]
        elif "image" not in self.spec:
            self.spec["image"] = spec["image"]

    # Same as '_merge_image''s own plain-setting branch: most-specific
    # file wins. 'initrd:' has no 'partitions'/'volumes' equivalent, so
    # nothing routes to _merge_parts_or_vols.
    def _merge_initrd(self, spec):
        if "initrd" in self.spec:
            for setting in spec["initrd"]:
                self.spec["initrd"][setting] = spec["initrd"][setting]
        else:
            self.spec["initrd"] = spec["initrd"]

    # Gathered from every file, not just the last: the fragment holding a
    # secret is the one that knows it's a secret, rarely the top file.
    #
    # direction: additive, deduplicated -- order doesn't matter
    # (docs/merging.md).
    def _merge_redact(self, spec):
        for pattern in spec.get("redact") or []:
            patterns = self.spec.setdefault("redact", [])
            if pattern not in patterns:
                patterns.append(pattern)

    # 'peer' is True for a file with nothing reaching for it -- a
    # top-level CLI file after the first, or a side-loaded fragment --
    # as opposed to one reached via 'requires:' (see _load()).
    def merge(self, spec, peer=False):
        self._merge_redact(spec)
        self._merge_distro(spec)
        self._merge_imager(spec)
        self._merge_multiconfig(spec)
        self._merge_defaults(spec)
        self._merge_packages(spec, peer=peer)
        self._merge_vendor(spec, peer=peer)
        self._merge_vendor_exclude(spec)
        self._merge_playbooks(spec, peer=peer)
        self._merge_tests(spec, peer=peer)
        if "image" in spec:
            self._merge_image(spec, peer=peer)
        if "initrd" in spec:
            self._merge_initrd(spec)
        return self.spec

    def parse(self):
        if self.image is None:
            self.image = Image(self.partitionHandler, self.options)
        self._apply_defaults()
        self.raw_spec = copy.deepcopy(self.spec)
        # A vendor-only spec (no image:/packages:/playbook:) skips both
        # parsers -- nothing to build. 'vendor:' is still validated here so a
        # typo is caught now, not later in 'seine vendor'.
        if "image" in self.spec:
            self.spec = self.partitionHandler.parse(self.spec)
            self.spec = self.image.parse(self.spec)
            module.check_kbuild(self.image.packages)
        elif "initrd" in self.spec or "packages" in self.spec or "playbook" in self.spec:
            # No 'image:' section, but something to build: the root
            # file-system tarball itself becomes this build's real
            # output (Image.parse()/own_tasks()).
            self.spec = self.image.parse(self.spec)
            module.check_kbuild(self.image.packages)
        else:
            from seine import vendor
            distro = distribution(self.spec)
            vendor.suites(vendor.parse(self.spec), distro)
            vendor.exclusions(self.spec)
        self._parse_multiconfig()
        return self.spec

    # Loads each 'multiconfig:' group as its own sub-build (like a CLI
    # '--' group). A sub-group's own 'image:' never reaches this spec --
    # only self.spec's 'image:' owns the disk. 'after'/'before' are
    # resolved into one 'after' set per group before loading, so
    # Image.tasks() has it ready to wire each group's 'needs'.
    def _parse_multiconfig(self):
        from seine import multiconfig
        groups = self.spec.get("multiconfig") or {}
        parsed = {name: multiconfig._parse_group(name, value)
                  for name, value in groups.items()}
        after = multiconfig.resolve_order(parsed)
        self.subbuilds = {
            name: multiconfig._load(files, self.options,
                                    defer_uki_check=len(after[name]) > 0)
            for name, (files, _after, _before) in parsed.items()}
        self.image.subbuilds = self.subbuilds
        self.image.multiconfig_after = after

    def build(self, reporter=None):
        if self.spec is None or self.image is None:
            raise RuntimeError("no specification was loaded or parsed!")
        return self.image.build(reporter=reporter)

    # Prunes intermediate images once, after everything's built, not
    # after each image. 'podman image prune' is machine-wide, so it's
    # skipped (not blocked on) when another build already holds the lock.
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

        # Redacts everywhere the patterns appear, but leaves the 'redact'
        # section itself alone -- its patterns describe what's hidden, and
        # matching itself would hide that.
        patterns = redactions(spec)
        for section in spec:
            if section != "redact":
                spec[section] = redact(spec[section], patterns)

        # return the spec in YAML format
        return yaml.dump(spec)

    # A single file's own text, not the merged spec -- redacted, but never
    # written back to disk. Refused unless in loaded_files or
    # 'extra_allowed'. Read as-is, no Jinja rendering.
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

    # Local files this build's 'packages:' reference (patches, kernel/
    # derived-flavour fragments) -- never 'defaults.packages:', which
    # builds nothing. Read fresh, not cached.
    def referenced_files(self):
        from seine import packages
        try:
            parsed = packages.parse(self.spec)
        except ValueError:
            return set()
        return {os.path.realpath(f) for p in parsed for f in p.referenced_files()}

    # Falls back to a referenced file (patch, kernel fragment) when
    # dump_file() refuses -- redacted as flat text, no YAML round-trip.
    # No path-containment check: the real build already reads whatever
    # a 'patches:'/'fragments:' entry names.
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
                # How many build steps may run at once. Defaults to 1 -- the
                # ordering/output a build has always had, and the easiest to debug.
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
                # Validated later against the task graph (Image.tasks()), not
                # here -- task names depend on the parsed spec, not just the CLI.
                self.options["target"] = a
            elif o in ("--dry-run"):
                self.options["dry_run"] = True
            elif o in ("-D", "--dump"):
                self.options["build"] = False
            elif o in ("--parallel"):
                # Cores one package build may use. Left unset it follows
                # --jobs, so raising --jobs divides the machine instead of
                # multiplying it.
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
            elif o in ("--resource"):
                # 'net=2' -> capacity 2 for tasks costed against "net".
                # A class no --resource names falls back to --jobs.
                try:
                    self.options["resources"] = parse_resources(
                        a, self.options["resources"])
                except ValueError as e:
                    sys.stderr.write("error: --resource %s\n" % e)
                    sys.exit(1)
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
            # '--' separates groups of files: several images, one scheduler. A
            # single group takes the path below as always; multiconfig.run()
            # handles more than one.
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
                # Shared: another build in a different terminal runs alongside
                # this one. What can't run alongside is anything sweeping
                # storage -- 'seine cache clear', and the prune below.
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

# Everything 'build' does, minus doing it. Same command with one
# option decided for it, not a second implementation -- a plan's
# value depends on it being the exact graph a build would walk.
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
  --resource CLASS=N    capacity N for a resource class steps may cost against
                        (e.g. 'net=2', 'io=4'). A class not given this falls
                        back to --jobs; may be given more than once
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
