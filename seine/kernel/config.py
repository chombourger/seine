# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Reading 'extends: kernel:' onto a package, and writing what it asks
# for into the fetched source's own kconfig fragments.

import os
import re

from . import (CONFIG_LINE, CONFIG_LINE_DISABLED, DEFAULT_FEATURESET,
              TARBALL_SUFFIXES, UPSTREAM_SCHEMES)


# The tree named by 'extends: kernel: upstream:'. It carries the same
# fields a Package does -- scheme, name, parameters -- so the code that
# fetches one fetches the other.
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
            # Same reason a git 'source' is pinned: a branch name moves,
            # and a kernel rebuild is not a thing to repeat by accident,
            # or to skip when it should not have been.
            if len(self.parameters.get("rev", "")) == 0:
                raise error(
                    "'extends: kernel: upstream' git trees shall be pinned "
                    "with ';rev=<commit>'")
            self.name = os.path.basename(location).removesuffix(".git")

    def __str__(self):
        return self.uri

# 'extends: kernel: configs:' entries -- named groups of literal
# 'CONFIG_OPTION=value' lines, for the common case of wanting a handful of
# symbols set without writing a fragment file for them. Kept as the
# assignments they were written as rather than translated here: that is a
# serialization detail of the fragment they end up in ('_config_line'),
# not something the specification's own words should already have lost.
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

# The fragment line one 'CONFIG_OPTION=value' entry becomes. 'n' is the one
# value kconfig itself does not accept as an assignment -- a disabled
# symbol is said by commenting it out, not by '=n' -- so that value alone
# is rewritten; anything else is passed through as written.
def _config_line(assignment):
    if CONFIG_LINE_DISABLED.match(assignment) is not None:
        return assignment
    symbol, value = assignment.split("=", 1)
    if value == "n":
        return "# %s is not set" % symbol
    return assignment

# Appends every 'configs:' group to an architecture's own config file, in
# the order the specification wrote them -- two groups may touch the same
# symbol, and the later one is meant to win, exactly as it would for two
# fragment files. A function of its own, the way '_add_derived_flavours'
# is, so it can be pointed at a fixture directly rather than only through
# the whole of 'extend'.
def _write_configs(package, config):
    if len(package.kernel_configs) == 0:
        return
    with open(config, "a") as f:
        for name, lines in package.kernel_configs.items():
            f.write("\n# %s, added by seine\n" % name)
            for line in lines:
                f.write("%s\n" % _config_line(line))

# Reads 'extends: kernel:' onto the package it was written on. The package
# is handed over whole rather than a dictionary handed back: what a
# setting is called in the yaml and what it is called on the package are
# one subject, and the error messages come from the package that is being
# parsed.
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
    # What a graft's ABI carries instead of Debian's own '+unreleased'.
    # None leaves it alone.
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
    # One or several flavours of seine's own, derived from an existing
    # Debian one, keyed by the flavour derived from rather than the
    # architecture ('armhf' builds 'armmp'). Takes precedence over a bare
    # 'flavour' rather than erroring when both are set: 'defaults'
    # commonly gives every kernel one, for a module built against it.
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
    # Filled in by '_add_derived_flavours' with what it actually built
    # for this architecture, which '_check_flavour' reads back rather
    # than working it out itself. None until then.
    package.kernel_derived_flavours_built = None
    # None, not a list of globs: with nothing said, which patches are
    # the packaging is decided by what they touch rather than by their
    # names. An explicit empty list is a different answer -- "keep none
    # of them" -- so presence decides, not emptiness.
    package.kernel_keep_patches = None
    if "keep-patches" in settings:
        package.kernel_keep_patches = package._parse_list(settings,
                                                          "keep-patches")
    # Subtracted from what 'keep-patches' selected, since that only
    # adds: taking one patch out of 'debian/*' would otherwise mean
    # writing out the thirty-odd being kept.
    package.kernel_drop_patches = package._parse_list(settings, "drop-patches")
    # Added to the patterns seine ships, for a packaging that builds
    # through files Debian's does not touch. Checked here rather than
    # where the series is read: a bad expression should be reported by
    # the specification that wrote it, not by the patch it first fails
    # to match.
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
