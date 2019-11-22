
import getopt
import subprocess
import sys
import yaml

from seine.image     import Image
from seine.cmd       import Cmd
from seine.partition import PartitionHandler

class BuildCmd(Cmd):
    def __init__(self):
        self.image = None
        self.options = {}
        self.partitionHandler = PartitionHandler()
        self.spec = None

    def load(self, yaml_file):
        with open(yaml_file, "r") as f:
            spec = yaml.load(f)
        if self.spec is None:
            self.spec = spec
        else:
            self.merge(spec)
        return self.spec

    def _merge_distro(self, spec):
        if "distribution" in self.spec and "distribution" in spec:
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
        if "image" in self.spec and "image" in spec:
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
            opts, args = getopt.getopt(argv, "h", ["help"])
        except getopt.GetoptError as err:
            print(err)
            cmd_build_usage()
            sys.exit(1)
        for o, a in opts:
            if o in ("-h", "--help"):
                cmd_build_usage()
                sys.exit()
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


