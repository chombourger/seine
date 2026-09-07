# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# 'extends: kernel:' rebuilds the distro kernel with a custom config, then
# grafts its packaging onto another tree. Split by concern: this file holds
# shared constants; config.py parses settings; upstream.py fetches/grafts;
# apply.py is the build-time hook; flavour.py handles flavours. Names are
# re-exported here so 'kernel.<name>' keeps working.

import collections
import functools
import os
import re
import yaml


# Keys accepted under 'extends: kernel:'.
SETTINGS = ["abi-suffix", "build-files", "configs", "derived-flavours",
            "drop-patches", "featureset", "flavour", "fragments",
            "keep-patches", "upstream", "upstream-sha256"]

# A literal 'CONFIG_X=value' assignment, or kconfig's own disabled form
# '# CONFIG_X is not set'. Both are accepted in 'configs:' so a fragment
# excerpt can be pasted in unchanged.
CONFIG_LINE = re.compile(r"^CONFIG_[A-Za-z0-9_]+=.+$")
CONFIG_LINE_DISABLED = re.compile(r"^# CONFIG_[A-Za-z0-9_]+ is not set$")

# Bump when what the graft *does* to a tree changes (not just the rules
# file), so a kernel grafted before stays as it was.
GRAFT_VERSION = 1

# Debian installs its generated module.lds under arch/<arch>/, via a kbuild
# patch. A tree that moved this leaves out-of-tree modules unable to link.
# The graft drops that patch; seine rewrites it for the tree in hand.
MODFINAL = "scripts/Makefile.modfinal"
MODULE_LDS = re.compile(r"\$\(objtree\)/scripts/module\.lds")
MODULE_LDS_PATCH = "debian/module-lds-under-arch-directory.patch"
MODULE_LDS_FALLBACK = (
    "ARCH_MODULE_LDS := $(word 1,$(wildcard $(objtree)/scripts/module.lds "
    "$(objtree)/arch/$(SRCARCH)/module.lds))")

# Debian's architecture name vs uname's machine name: the kernel build
# wants the former for ARCH, uname callers want the latter.
KERNEL_ARCHITECTURES = {
    "amd64":   "x86_64",
    "arm64":   "arm64",
    "armel":   "arm",
    "armhf":   "arm",
    "i386":    "i386",
    "ppc64el": "powerpc",
    "riscv64": "riscv",
    "s390x":   "s390",
}

KERNEL_MACHINES = {
    "amd64":   "x86_64",
    "arm64":   "aarch64",
    "armel":   "armv7l",
    "armhf":   "armv7l",
    "i386":    "i686",
    "ppc64el": "ppc64le",
    "riscv64": "riscv64",
    "s390x":   "s390x",
}

def kernel_architecture(architecture):
    kernel = KERNEL_ARCHITECTURES.get(architecture)
    if kernel is None:
        raise ValueError(
            "seine has no kernel architecture for '%s'. Add it to "
            "KERNEL_ARCHITECTURES and KERNEL_MACHINES in seine/kernel/."
            % architecture)
    return kernel

# Scheme/suffix accepted for 'extends: kernel: upstream:', same notation as
# a package 'source' minus apt://. git URIs must be pinned with ';rev='.
UPSTREAM_SCHEMES = ["git", "https"]
TARBALL_SUFFIXES = [".tar.xz", ".tar.gz", ".tar.bz2"]

# What seine knows about Debian's kernel patches (kept vs. always dropped)
# lives in this data file instead of code, so tracking a new release is a
# data edit, not a patch.
KERNEL_RULES = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "kernel.yml")

KernelRules = collections.namedtuple("KernelRules",
                                     ["build_files", "drop_patches", "content"])

# Cached with the raw bytes: those bytes affect whether a grafted kernel
# needs rebuilding.
@functools.lru_cache(maxsize=None)
def kernel_rules():
    with open(KERNEL_RULES, "rb") as f:
        content = f.read()
    rules = yaml.safe_load(content) or {}
    for setting in ["build-files", "drop-patches"]:
        if type(rules.get(setting)) != type([]):
            raise ValueError("%s: '%s' shall be a list"
                             % (KERNEL_RULES, setting))
    return KernelRules(rules["build-files"], rules["drop-patches"], content)

# Rules' build-file patterns plus whatever the spec added, compiled once.
# 'extra' is a tuple so lru_cache can hash it.
@functools.lru_cache(maxsize=None)
def build_files(extra=()):
    return re.compile("|".join(list(kernel_rules().build_files) + list(extra)))

# Debian names a kernel by architecture, featureset and flavour; 'none' is
# the default featureset nearly everything wants.
DEFAULT_FEATURESET = "none"

# Debian build profiles for kernel tools: NO_TOOLS drops linux-kbuild
# entirely, MIN_TOOLS drops the extra tools but keeps it.
NO_TOOLS = "pkg.linux.notools"
MIN_TOOLS = "pkg.linux.mintools"

from .config import Upstream, _config_line, _parse_configs, _write_configs, parse
from .upstream import (SERIES_CHECK, UPSTREAM, _check_series,
                       _filter_series, _graft_release, _kernel_version,
                       _modfinal_is_patched, _packaging_patch, _matches,
                       _source_name, _touches, _upstream_args,
                       _verify_upstream, fetch_upstream, graft,
                       module_lds_patch)
from .apply import (_abiname, _check_flavour, _kernel_packages,
                    _record_abiname, extend)
from .flavour import (_add_derived_flavours, _defines_list,
                      _defines_replace, _defines_set, _derive_flavour_block,
                      _disable_signed, _restrict_flavour,
                      _restrict_flavour_ini, _restrict_flavour_toml,
                      _set_abi_suffix, _toml_blocks, _toml_line,
                      _toml_set, _toml_subtable, _toml_value)
