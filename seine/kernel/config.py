# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Reading 'extends: kernel:' onto a package, and writing what it asks for
# into the fetched source's own kconfig fragments.

import os
import re

from . import (CONFIG_LINE, CONFIG_LINE_DISABLED, DEFAULT_FEATURESET,
              TARBALL_SUFFIXES, UPSTREAM_SCHEMES)


# The tree named by 'extends: kernel: upstream:'. Carries the same fields
# as a Package (scheme, name, parameters) so the same fetch code works.
class Upstream:
    def __init__(self, uri, error):
        if type(uri) != type(""):
            raise error("'extends: kernel: upstream' shall be a string")
        if "://" not in uri:
            raise error(
                "'extends: kernel: upstream' has no URI scheme: expected one "
                "of %s" % ", ".join("%s://" % s for s in UPSTREAM_SCHEMES))

        self.uri = uri
        self.scheme, rest = uri.split("://", 1)
        self.parameters = {}
        if self.scheme not in UPSTREAM_SCHEMES:
            raise error(
                "'extends: kernel: upstream' has unsupported URI scheme "
                "'%s://', expected one of %s"
                % (self.scheme, ", ".join("%s://" % s for s in UPSTREAM_SCHEMES)))

        if self.scheme == "https":
            if not any(rest.endswith(s) for s in TARBALL_SUFFIXES):
                raise error(
                    "'extends: kernel: upstream' over https shall point at a "
                    "source tarball ending in %s" % ", ".join(TARBALL_SUFFIXES))
            self.name = os.path.basename(rest)
        else:
            location, *parameters = rest.split(";")
            for parameter in parameters:
                key, _, value = parameter.partition("=")
                self.parameters[key] = value
            # A branch name can move; a git ref must be pinned so a
            # rebuild isn't silently repeated (or skipped) by accident.
            if len(self.parameters.get("rev", "")) == 0:
                raise error(
                    "'extends: kernel: upstream' git trees shall be pinned "
                    "with ';rev=<commit>'")
            self.name = os.path.basename(location).removesuffix(".git")

    def __str__(self):
        return self.uri

# 'extends: kernel: configs:' entries: named groups of literal
# 'CONFIG_OPTION=value' lines, for setting a handful of symbols without
# writing a whole fragment file.
def _parse_configs(package, settings):
    if "configs" not in settings:
        return {}
    configs = settings["configs"]
    if type(configs) != type({}):
        raise package._error(
            "'extends: kernel: configs' shall be a dictionary of group "
            "names to a list of 'CONFIG_OPTION=value' lines")
    parsed = {}
    for name, lines in configs.items():
        if type(name) != type("") or len(name) == 0:
            raise package._error(
                "'extends: kernel: configs' has a group name that is not "
                "a non-empty string")
        if (type(lines) != type([]) or len(lines) == 0
                or any(type(line) != type("") for line in lines)):
            raise package._error(
                "'extends: kernel: configs: %s' shall be a non-empty "
                "list of 'CONFIG_OPTION=value' lines" % name)
        for line in lines:
            if (CONFIG_LINE.match(line) is None
                    and CONFIG_LINE_DISABLED.match(line) is None):
                raise package._error(
                    "'extends: kernel: configs: %s' has '%s', which is "
                    "neither 'CONFIG_OPTION=value' nor '# CONFIG_OPTION "
                    "is not set'" % (name, line))
        parsed[name] = lines
    return parsed

# kconfig itself does not accept '=n' as an assignment -- a disabled
# symbol must be a comment -- so only that value is rewritten.
def _config_line(assignment):
    if CONFIG_LINE_DISABLED.match(assignment) is not None:
        return assignment
    symbol, value = assignment.split("=", 1)
    if value == "n":
        return "# %s is not set" % symbol
    return assignment

# Appends every 'configs:' group to an architecture's config file, in
# spec order, so a later group can override an earlier one on the same
# symbol -- same as two fragment files would.
def _write_configs(package, config):
    if len(package.kernel_configs) == 0:
        return
    with open(config, "a") as f:
        for name, lines in package.kernel_configs.items():
            f.write("\n# %s, added by seine\n" % name)
            for line in lines:
                f.write("%s\n" % _config_line(line))

def parse(package, extends):
    settings = extends.get("kernel", {})
    package.kernel = "kernel" in extends
    package.kernel_fragments = package._parse_list(settings, "fragments")
    package.kernel_configs = _parse_configs(package, settings)
    package.kernel_flavour = settings.get("flavour")
    package.kernel_featureset = settings.get("featureset", DEFAULT_FEATURESET)
    package.kernel_upstream = None
    if "upstream" in settings:
        if package.scheme != "apt":
            raise package._error(
                "'extends: kernel: upstream' grafts the distribution's "
                "debian/ onto another tree, so the package it is set on "
                "has to be the distribution's own kernel source")
        package.kernel_upstream = Upstream(settings["upstream"], package._error)
    package.kernel_upstream_sha256 = package._parse_digest(settings,
                                                           "upstream-sha256")
    package.kernel_abi_suffix = settings.get("abi-suffix")
    if package.kernel_abi_suffix is not None:
        if type(package.kernel_abi_suffix) != type(""):
            raise package._error("'extends: kernel: abi-suffix' shall be a string")
        if "'" in package.kernel_abi_suffix:
            raise package._error(
                "'extends: kernel: abi-suffix' cannot hold a single quote: "
                "it is written into a TOML string wrapped in one")
        if package.kernel_upstream is None:
            raise package._error(
                "'extends: kernel: abi-suffix' only means something for a "
                "graft: it is what a grafted kernel's ABI carries in "
                "place of '+unreleased', and there is no such ABI to "
                "rename without 'upstream'")
    # Takes precedence over a bare 'flavour' instead of erroring when both
    # are set, since 'defaults' commonly sets 'flavour' for every kernel.
    package.kernel_derived_flavours = settings.get("derived-flavours")
    if package.kernel_derived_flavours is not None:
        if type(package.kernel_derived_flavours) != type({}):
            raise package._error(
                "'extends: kernel: derived-flavours' shall be a "
                "dictionary of Debian flavour names to a dictionary of "
                "flavours derived from each, with a list of config "
                "fragments apiece")
        normalized = {}
        for base, derived in package.kernel_derived_flavours.items():
            if type(base) != type("") or "'" in base:
                raise package._error(
                    "'extends: kernel: derived-flavours' has a Debian "
                    "flavour name that is not a string, or holds a "
                    "single quote -- it is written into a TOML string "
                    "wrapped in one")
            if type(derived) != type({}):
                raise package._error(
                    "'extends: kernel: derived-flavours: %s' shall be a "
                    "dictionary of flavour names to a list of config "
                    "fragments each" % base)
            names = {}
            for name, fragments in derived.items():
                if type(name) != type("") or "'" in name:
                    raise package._error(
                        "'extends: kernel: derived-flavours: %s' has a "
                        "flavour name that is not a string, or holds a "
                        "single quote -- it is written into a TOML "
                        "string wrapped in one" % base)
                if fragments is None:
                    fragments = []
                if (type(fragments) != type([])
                        or any(type(f) != type("") for f in fragments)):
                    raise package._error(
                        "'extends: kernel: derived-flavours: %s: %s' "
                        "shall be a list of config fragments, or left "
                        "empty" % (base, name))
                names[name] = fragments
            if len(names) == 0:
                raise package._error(
                    "'extends: kernel: derived-flavours: %s' names no "
                    "flavour to derive from it" % base)
            normalized[base] = names
        package.kernel_derived_flavours = normalized
    # Filled in later by '_add_derived_flavours' with what it actually
    # built for this architecture.
    package.kernel_derived_flavours_built = None
    # None (not an empty list) means "keep the default set"; an explicit
    # empty list means "keep none of them" -- presence decides.
    package.kernel_keep_patches = None
    if "keep-patches" in settings:
        package.kernel_keep_patches = package._parse_list(settings,
                                                          "keep-patches")
    package.kernel_drop_patches = package._parse_list(settings, "drop-patches")
    package.kernel_build_files = package._parse_list(settings, "build-files")
    for pattern in package.kernel_build_files:
        try:
            re.compile(pattern)
        except re.error as e:
            raise package._error(
                "'extends: kernel: build-files' has '%s', which is not a "
                "regular expression: %s" % (pattern, e))
    for setting, value in [("flavour", package.kernel_flavour),
                           ("featureset", package.kernel_featureset)]:
        if value is not None and type(value) != type(""):
            raise package._error(
                "'extends: kernel: %s' shall be a string" % setting)
