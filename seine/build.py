
import getopt
import os
import subprocess
import sys
import yaml

from seine.image     import Image
from seine.cmd       import Cmd
from seine.partition import PartitionHandler

class BuildCmd(Cmd):
    def __init__(self):
        self.image = None
        self.options = { "debug": False, "verbose": False }
        self.partitionHandler = PartitionHandler()
        self.spec = None

    def loads(self, yaml_spec):
        return self._load("<string>", yaml_spec)

    def load(self, yaml_file):
        with open(yaml_file, "r") as f:
            return self._load(yaml_file, f)

    def _load(self, yaml_filename, yaml_spec):
        spec = yaml.load(yaml_spec)

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

    def _merge_distro(self, spec):
        if "distribution" in spec:
            if "distribution" in self.spec:
                for setting in spec["distribution"]:
                    self.spec["distribution"][setting] = spec["distribution"][setting]
            elif "distribution" not in self.spec:
                self.spec["distribution"] = spec["distribution"]

    def _append_playbooks(self, spec):
        if "playbook" in self.spec and "playbook" in spec:
            for playbook in spec["playbook"]:
                self.spec["playbook"].append(playbook)
        elif "playbook" not in self.spec:
            self.spec["playbook"] = spec["playbook"]

    def _merge_image(self, spec):
        if "image" in spec:
            if "image" in self.spec:
                for setting in spec["image"]:
                    self.spec["image"][setting] = spec["image"][setting]
            elif "image" not in self.spec:
                self.spec["image"] = spec["image"]

    def merge(self, spec):
        self._merge_distro(spec)
        self._append_playbooks(spec)
        self._merge_image(spec)
        return self.spec

    def parse(self):
        if self.image is None:
            self.image = Image(self.partitionHandler, self.options)
        self.spec = self.partitionHandler.parse(self.spec)
        self.spec = self.image.parse(self.spec)
        return self.spec

    def build(self):
        if self.spec is None or self.image is None:
            raise RuntimeError("no specification was loaded or parsed!")
        return self.image.build()

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, "dhv", ["debug", "help", "verbose"])
        except getopt.GetoptError as err:
            print(err)
            cmd_build_usage()
            sys.exit(1)
        for o, a in opts:
            if o in ("-d", "--debug"):
                self.options["debug"] = True
                self.options["verbose"] = True
            elif o in ("-h", "--help"):
                cmd_build_usage()
                sys.exit()
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
            self.parse()
            sys.exit(self.build())
        except OSError as e:
            sys.stderr.write("error: couldn't open build YAML file: {0}\n".format(e))
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: YAML file is invalid: {0}\n".format(e))
            sys.exit(3)
        except subprocess.CalledProcessError as e:
            sys.stderr.write("error: build failed: {0}\n".format(e))
            sys.exit(4)
