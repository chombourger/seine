# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import os

# A source package to rebuild, as described by one entry of the spec's
# 'packages' section. Where the source comes from is given as a URI:
#
#   apt://busybox[=1:1.37.0-6]     the distribution's own source package
#   https://.../busybox_1.dsc      a source package published elsewhere
#   git://host/busybox.git;rev=..  a tree carrying its own debian/ directory
#
# Only .dsc files are accepted over https: a plain upstream tarball has no
# debian/ directory and so cannot be built, and pairing one with packaging
# taken from somewhere else is a second source this does not model yet.
SCHEMES = ["apt", "git", "https"]

class Package:
    def __init__(self, spec, index):
        self.index = index
        if type(spec) != type({}):
            raise ValueError("package #%d is not a dictionary!" % index)
        if "source" not in spec:
            raise ValueError("package #%d has no 'source' specified!" % index)

        self.spec = spec
        self.dirname = spec.get("_dirname", "")
        self.priority = spec.get("priority", 500)

        self._parse_source(spec["source"])
        self.cross = self._parse_bool(spec, "cross")
        self.options = self._parse_list(spec, "options")
        self.patches = self._parse_list(spec, "patches")
        self.profiles = self._parse_list(spec, "profiles")
        self.source_date_epoch = self._parse_epoch(spec)

    def _error(self, message):
        return ValueError("package #%d ('%s'): %s" % (self.index, self.source, message))

    def _parse_source(self, source):
        self.source = source
        if type(source) != type(""):
            raise ValueError("package #%d has a non-string 'source'!" % self.index)
        if "://" not in source:
            raise ValueError(
                "package #%d ('%s') has no URI scheme: expected one of %s"
                % (self.index, source, ", ".join("%s://" % s for s in SCHEMES)))

        self.scheme, rest = source.split("://", 1)
        if self.scheme not in SCHEMES:
            raise self._error("unsupported URI scheme '%s://', expected one of %s"
                % (self.scheme, ", ".join("%s://" % s for s in SCHEMES)))
        if len(rest) == 0:
            raise self._error("URI has nothing after its scheme")

        # Defaults for the fields only some of the schemes carry, so callers
        # may read them without caring which scheme they got.
        self.name = None
        self.version = None
        self.parameters = {}

        if self.scheme == "apt":
            self.name, _, self.version = rest.partition("=")
            if len(self.name) == 0:
                raise self._error("no source package name given")
            if self.version == "":
                self.version = None
        elif self.scheme == "https":
            if not rest.endswith(".dsc"):
                raise self._error(
                    "https sources shall point at a .dsc file: an upstream "
                    "tarball carries no debian/ directory to build from")
            self.name = os.path.basename(rest).split("_")[0]
        elif self.scheme == "git":
            # bitbake's notation: location followed by ;key=value pairs.
            location, *parameters = rest.split(";")
            for parameter in parameters:
                key, _, value = parameter.partition("=")
                self.parameters[key] = value
            # A branch name moves, and a build that cannot be repeated is
            # not worth caching, let alone calling reproducible.
            if len(self.parameters.get("rev", "")) == 0:
                raise self._error(
                    "git sources shall be pinned with ';rev=<commit>' so the "
                    "same specification always rebuilds the same source")
            self.name = os.path.basename(location).removesuffix(".git")

    def _parse_bool(self, spec, key):
        value = spec.get(key)
        if value is not None and type(value) != type(True):
            raise self._error("'%s' shall be either true or false" % key)
        return value

    def _parse_list(self, spec, key):
        values = spec.get(key, [])
        if type(values) != type([]):
            raise self._error("'%s' shall be a list" % key)
        for value in values:
            if type(value) != type(""):
                raise self._error("'%s' shall be a list of strings" % key)
        return values

    def _parse_epoch(self, spec):
        value = spec.get("source_date_epoch")
        if value is None:
            return None
        if type(value) != type(0):
            raise self._error("'source_date_epoch' shall be a number of seconds")
        return value

    # Patches are given relative to the YAML file that listed them, which is
    # not necessarily the one being built: specifications are assembled from
    # several files through 'requires'.
    def patch_files(self):
        return [os.path.normpath(os.path.join(self.dirname, p)) for p in self.patches]

# Where the source of a package is fetched and, later, built. Everything
# happens inside the builder container: the tools involved (apt-get source,
# dget, git) are installed there rather than on the machine seine runs on,
# and the working directory is a host-side directory bind-mounted into it so
# the fetched source outlives the container that fetched it.
WORKDIR = "/src"

class Builder:
    def __init__(self, distro, options, builderImage):
        self.builderImage = builderImage
        self.distro = distro
        self.options = options

    def fetch(self, package, workdir):
        volumes = [(workdir, WORKDIR)]
        self.builderImage.exec(
            self._fetch_args(package), volumes=volumes, workdir=WORKDIR)
        return self._source_dir(package, workdir)

    def _fetch_args(self, package):
        if package.scheme == "apt":
            source = package.name
            if package.version is not None:
                source = "%s=%s" % (package.name, package.version)
            return ["apt-get", "source", source]

        if package.scheme == "https":
            # -u: the .dsc of a rebuilt or third-party package is not
            # necessarily signed by a key we have, and refusing to fetch it
            # on those grounds would make the scheme useless. What it is
            # allowed to do to the image is the specification's call.
            return ["dget", "-u", package.source]

        # git:// says nothing about how to reach the remote; bitbake spells
        # that ';protocol=', and https is the sane default.
        protocol = package.parameters.get("protocol", "https")
        location = package.source.split("://", 1)[1].split(";")[0]
        url = "%s://%s" % (protocol, location)

        args = ["git", "clone"]
        if "branch" in package.parameters:
            args += ["--branch", package.parameters["branch"]]
        args += [url, package.name]
        # The revision is what the specification pinned; the branch only
        # says where to look for it.
        return ["sh", "-c", "%s && cd %s && git checkout --detach %s" % (
            " ".join(args), package.name, package.parameters["rev"])]

    # Every scheme leaves exactly one unpacked source tree behind, next to
    # the .dsc/tarballs it came from, but only apt-get source and dget name
    # it after the upstream version rather than the package.
    def _source_dir(self, package, workdir):
        directories = [d for d in sorted(os.listdir(workdir))
                       if os.path.isdir(os.path.join(workdir, d))]
        if len(directories) != 1:
            raise ValueError(
                "fetching '%s' produced %d source directories, expected one: %s"
                % (package.source, len(directories), ", ".join(directories)))
        return os.path.join(workdir, directories[0])

# Validates the 'packages' section and returns it as Package objects,
# ordered the way they will be built. Packages that build-depend on one
# another are ordered by 'priority', as playbooks are.
def parse(spec):
    packages = spec.get("packages", [])
    if type(packages) != type([]):
        raise ValueError("'packages' shall be a list of source packages!")

    parsed = [Package(p, i + 1) for i, p in enumerate(packages)]
    return sorted(parsed, key=lambda p: p.priority)
