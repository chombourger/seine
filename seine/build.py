# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import os
import subprocess
import sys
import yaml

from seine.image     import Image
from seine.cmd       import Cmd
from seine.partition import PartitionHandler

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
        "packages-only",
        "parallel=",
        "rebuild",
        "require-hashes",
        "sbom",
        "sign-key=",
        "verbose"
    ]

    def __init__(self):
        self.image = None
        self.options = { "build": True, "debug": False, "dry_run": False, "jobs": 1, "keep": False,
                         "packages_only": False, "parallel": None,
                         "rebuild": False, "require_hashes": False,
                         "sbom": False, "sign_key": None, "verbose": False }
        self.partitionHandler = PartitionHandler()
        self.spec = None
        self._loading = []

    def loads(self, yaml_spec):
        return self._load("<string>", yaml_spec)

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
        path = os.path.realpath(yaml_file)
        if path in self._loading:
            loop = self._loading[self._loading.index(path):] + [path]
            raise ValueError("'requires' loops: %s!" % " -> ".join(loop))
        self._loading.append(path)
        try:
            with open(yaml_file, "r") as f:
                return self._load(yaml_file, f)
        finally:
            self._loading.pop()

    def _load(self, yaml_filename, yaml_spec):
        spec = yaml.safe_load(yaml_spec)

        # Patches and kconfig fragments are listed relative to the file
        # listing them, which merging would otherwise lose. Resolved here,
        # where the file they came from is still known: a package assembled
        # from several files has no one directory to carry along.
        for package in self._package_entries(spec):
            self._resolve_files(package, os.path.dirname(yaml_filename))
            self._record_origins(package, yaml_filename)

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
    FILE_LISTS = [["patches"], ["extends", "kernel", "config"]]

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

    def _append_playbooks(self, spec):
        if "playbook" in spec:
            if "playbook" in self.spec:
                for playbook in spec["playbook"]:
                    self.spec["playbook"].append(playbook)
            elif "playbook" not in self.spec:
                self.spec["playbook"] = spec["playbook"]

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
    def _merge_packages(self, spec):
        if "packages" not in spec:
            return
        if "packages" not in self.spec:
            self.spec["packages"] = spec["packages"]
            return
        for package in spec["packages"]:
            name = self._package_name(package)
            existing = [p for p in self.spec["packages"]
                        if self._package_name(p) == name]
            if name is None or len(existing) == 0:
                self.spec["packages"].append(package)
            else:
                self._merge_package(existing[0], package)

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
                        package[setting][kind].update(newpackage[setting][kind])
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
            name = self._package_name(default)
            for package in self.spec.get("packages") or []:
                if self._package_name(package) == name:
                    self._merge_package(package, default)

    def _merge_package(self, package, newpackage):
        for setting in newpackage:
            if setting == BuildCmd.ORIGINS:
                continue
            if setting == "extends" and type(package.get(setting)) == type({}):
                self._merge_extends(package[setting], newpackage[setting],
                                    package, newpackage)
            elif setting not in package:
                package[setting] = newpackage[setting]
                self._take_origin(package, newpackage, setting)

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
                if setting not in extends[kind]:
                    extends[kind][setting] = newextends[kind][setting]
                    self._take_origin(package, newpackage,
                                      "extends.%s.%s" % (kind, setting))

    # The source package a 'source' URI names, which is what decides
    # whether two entries are the same package. Kept simple on purpose:
    # the URI is parsed properly later, and a name that comes out wrong
    # here merges nothing that was not already separate.
    def _package_name(self, package):
        if type(package) != type({}) or type(package.get("source")) != type(""):
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

    def merge(self, spec):
        self._merge_distro(spec)
        self._merge_imager(spec)
        self._merge_defaults(spec)
        self._merge_packages(spec)
        self._append_playbooks(spec)
        if "image" in spec:
            self._merge_image(spec)
        return self.spec

    def parse(self):
        if self.image is None:
            self.image = Image(self.partitionHandler, self.options)
        self._apply_defaults()
        self.spec = self.partitionHandler.parse(self.spec)
        self.spec = self.image.parse(self.spec)
        return self.spec

    def build(self):
        if self.spec is None or self.image is None:
            raise RuntimeError("no specification was loaded or parsed!")
        return self.image.build()

    def dump(self, spec):
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

        # return the spec in YAML format
        return yaml.dump(spec)

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
            elif o in ("--packages-only"):
                self.options["packages_only"] = True
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
            for spec in args:
                self.load(spec)

            spec = self.parse()
            result = 0
            if self.options["build"]:
                result = self.build()
            else:
                print(self.dump(spec))
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

# Everything 'build' does up to the point of doing it. The same command with
# one option decided for it, rather than a second implementation of it: what
# a plan is worth depends on it being the graph a build would walk, and two
# code paths would be two answers.
class PlanCmd(BuildCmd):
    NAME = "plan"

    # Nothing but '-h'. Of a build's options the only one that reached the
    # plan was how many steps it said it would run at once, which is not
    # what someone asking what a build would do wants to know. 'seine build
    # --dry-run' still takes them all, for the plan of a particular build.
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help"]

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
  seine build [options] SPEC... 

Examples:
  seine build demo-image.yml
  seine build -v demo-image.yml

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
  --sbom                produce a Software Bill of Materials (SBOM) using
                        debsbom
  -v, --verbose         produce verbose output while building the image

"""

PLAN_USAGE = """
Say what a build would do, without doing any of it

Description:
  Prints the steps a build of these specifications would run, in the order it
  would run them and with what each waits for, and the packages it would
  leave alone with the stamp that says why.

  The plan is not a description of the build: it is the same graph a build
  walks, printed instead of walked. So a package already built from exactly
  these inputs has no steps in it at all -- which is the useful half of the
  answer.

  Nothing is fetched, built or written. 'seine build --dry-run' is the same
  thing.

Usage:
  seine plan [options] SPEC...

Examples:
  seine plan demo-image.yml
  seine plan --jobs 4 demo-image.yml

Flags:
  -h, --help            print this message

  And nothing else: a plan is the same whoever asks for it. For the plan of
  a build with particular options, 'seine build --dry-run' takes all of
  them.

"""
