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
    SHORT_OPTIONS = "dDhkv"
    LONG_OPTIONS = [
        "debug",
        "dump",
        "help",
        "keep",
        "rebuild",
        "sbom",
        "verbose"
    ]

    def __init__(self):
        self.image = None
        self.options = { "build": True, "debug": False, "keep": False, "rebuild": False, "sbom": False, "verbose": False }
        self.partitionHandler = PartitionHandler()
        self.spec = None

    def loads(self, yaml_spec):
        return self._load("<string>", yaml_spec)

    def load(self, yaml_file):
        with open(yaml_file, "r") as f:
            return self._load(yaml_file, f)

    def _load(self, yaml_filename, yaml_spec):
        spec = yaml.safe_load(yaml_spec)

        # Patches and kconfig fragments are listed relative to the file
        # listing them, which merging would otherwise lose. Resolved here,
        # where the file they came from is still known: a package assembled
        # from several files has no one directory to carry along.
        for package in self._package_entries(spec):
            self._resolve_files(package, os.path.dirname(yaml_filename))

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
            if setting == "extends" and type(package.get(setting)) == type({}):
                for kind in newpackage[setting]:
                    if type(package[setting].get(kind)) != type({}):
                        package[setting][kind] = newpackage[setting][kind]
                    else:
                        package[setting][kind].update(newpackage[setting][kind])
            else:
                package[setting] = newpackage[setting]

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
            if setting == "extends" and type(package.get(setting)) == type({}):
                self._merge_extends(package[setting], newpackage[setting])
            elif setting not in package:
                package[setting] = newpackage[setting]

    # One level deeper than the rest: 'extends' is a dictionary of kinds,
    # and two files describing the same kernel are describing the same
    # 'kernel' entry rather than replacing each other's.
    def _merge_extends(self, extends, newextends):
        if type(newextends) != type({}):
            return
        for kind in newextends:
            if type(extends.get(kind)) != type({}) or type(newextends[kind]) != type({}):
                extends.setdefault(kind, newextends[kind])
                continue
            for setting in newextends[kind]:
                extends[kind].setdefault(setting, newextends[kind][setting])

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

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, BuildCmd.SHORT_OPTIONS, BuildCmd.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write(str(err))
            sys.stderr.write(USAGE)
            sys.exit(1)
        for o, a in opts:
            if o in ("-d", "--debug"):
                self.options["debug"] = True
                self.options["verbose"] = True
            elif o in ("-h", "--help"):
                print(USAGE)
                sys.exit()
            elif o in ("-k", "--keep"):
                self.options["keep"] = True
            elif o in ("-D", "--dump"):
                self.options["build"] = False
            elif o in ("--rebuild"):
                self.options["rebuild"] = True
            elif o in ("--sbom"):
                self.options["sbom"] = True
            elif o in ("-v", "--verbose"):
                self.options["verbose"] = True
            else:
                assert False, "unhandled option"

        if len(args) == 0:
            sys.stderr.write("error: build command expects a YAML file\n")
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
  -D, --dump            do not build the image, just dump the consolidated specification
  -h, --help            print this message
  -k, --keep            keep temporary files
  --rebuild             rebuild the packages of the 'packages' section even if
                        they were built before
  --sbom                produce a Software Bill of Materials (SBOM) using
                        debsbom
  -v, --verbose         produce verbose output while building the image

"""
