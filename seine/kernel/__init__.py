# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# What 'extends: kernel:' means: rebuilding the distribution's kernel with
# a configuration of one's own, and grafting its packaging onto a tree it
# was not written for.
#
# The functions here take the Builder as their first argument where they
# need one at all, rather than living on it: what a kernel is is one
# subject, and seine/packages.py is about source packages in general.
#
# Split by concern: this file holds the shared rules/constants every
# other one needs (kernel_rules(), build_files(), the architecture
# tables); config.py reads 'extends: kernel:' onto a package; upstream.py
# fetches and grafts an 'upstream:' tree; apply.py is extend(), the
# build-time hook; flavour.py restricts/derives flavours and edits the
# toml/ini files that takes. Every name below is re-exported so
# 'from seine import kernel; kernel.<name>' keeps working unchanged.

import collections
import functools
import os
import re
import yaml


# What 'extends: kernel:' takes. A kernel is configured rather than
# patched: Debian builds its kernels from a stack of kconfig files under
# debian/, so a fragment appended to the right one is both easier to write
# and less likely to conflict with the next point release than a patch
# would be.
SETTINGS = ["abi-suffix", "build-files", "configs", "derived-flavours",
            "drop-patches", "featureset", "flavour", "fragments",
            "keep-patches", "upstream", "upstream-sha256"]

# A literal 'extends: kernel: configs:' entry, checked against here rather
# than left for oldconfig to catch: a typo in a group meant for hardware
# nobody has tested yet would otherwise only surface as a symbol that
# silently never got set. Two forms are accepted: an assignment, and
# kconfig's own way of writing a disabled one -- the second so that a
# fragment excerpt (like the one 'config:' itself is documented with) can
# be pasted into a group unchanged rather than rewritten to '=n' first.
# 'CONFIG_X=n' is accepted too, translated to the comment form when the
# line is written into the fragment (config.py's own '_config_line')
# -- kconfig itself does not understand '=n' as an assignment at all.
CONFIG_LINE = re.compile(r"^CONFIG_[A-Za-z0-9_]+=.+$")
CONFIG_LINE_DISABLED = re.compile(r"^# CONFIG_[A-Za-z0-9_]+ is not set$")

# What the graft makes of a tree, beyond the packaging it copies across.
# The rules seine writes are hashed by content; what seine *does* to a tree
# is code, and code is in no digest -- so bump this when that changes, or a
# kernel grafted before it stays as it was.
GRAFT_VERSION = 1

# Debian generates module.lds during the kernel build, installs it under
# arch/<arch>/, and carries a kbuild patch to look for it there. A tree
# that has moved that rule leaves the patch inapplicable, so the graft
# drops it -- and then nothing links a module at all: the '%.ko' rule wants
# a file no package installs, and make says it has no rule to make it.
#
# The patch is written again for the tree in hand rather than shipped as
# one to rebase. It touches scripts/ alone, which is what seine already
# counts as packaging.
MODFINAL = "scripts/Makefile.modfinal"
MODULE_LDS = re.compile(r"\$\(objtree\)/scripts/module\.lds")
MODULE_LDS_PATCH = "debian/module-lds-under-arch-directory.patch"
MODULE_LDS_FALLBACK = (
    "ARCH_MODULE_LDS := $(word 1,$(wildcard $(objtree)/scripts/module.lds "
    "$(objtree)/arch/$(SRCARCH)/module.lds))")

# What a Debian architecture is called by the kernel, and what uname
# would have called it. Two answers to one question, and a tree wants
# whichever its own build system asks for -- the kernel's for ARCH, and
# uname's for anything that would otherwise have run uname.
#
# Kept here rather than in the packaging that uses them, so that adding
# an architecture is one edit rather than three: the make tables in both
# rules files are rendered from these.
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

# The kernel's name for an architecture, or nothing if seine has never
# been told. Answered rather than guessed: an empty ARCH handed to a tree
# that falls back to uname is the builder's architecture, and a module
# built for the wrong one is a module that builds.
def kernel_architecture(architecture):
    kernel = KERNEL_ARCHITECTURES.get(architecture)
    if kernel is None:
        raise ValueError(
            "seine has no kernel architecture for '%s'. Add it to "
            "KERNEL_ARCHITECTURES and KERNEL_MACHINES in seine/kernel/."
            % architecture)
    return kernel

# Where a kernel tree comes from when it is not the one the distribution
# packages: 'extends: kernel: upstream:'. The distribution's debian/ is
# kept and grafted onto that tree, so what comes out carries Debian's
# package names, maintainer scripts and headers layout -- a replacement
# for the distribution's kernel rather than a parallel one beside it.
#
# The same notation a package's 'source' uses, minus apt://, which names
# a source package rather than a tree:
#
#   https://cdn.kernel.org/.../linux-<version>.tar.xz  a release tarball
#   git://host/bsp.git;rev=<commit>                    a tree, BSP or not
UPSTREAM_SCHEMES = ["git", "https"]
TARBALL_SUFFIXES = [".tar.xz", ".tar.gz", ".tar.bz2"]

# What seine knows about Debian's kernel patches -- which of them are
# packaging, and which are never kept -- lives beside the code in a data
# file, so that moving with the distribution is an edit rather than a
# patch. What each setting means is written down there.
#
# Everything outside debian/ is dropped rather than fought with, which
# needs no data to say: bugfix/* against a newer tree is a backport it
# already has, and features/* is keyed to config symbols that oldconfig
# drops along with the patch.
KERNEL_RULES = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "kernel.yml")

KernelRules = collections.namedtuple("KernelRules",
                                     ["build_files", "drop_patches", "content"])

# Read once, and kept with the bytes it was read from: those bytes are
# part of what decides whether a grafted kernel needs rebuilding, so a
# change to the rules is a change to the kernel they produce.
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

# The rules' patterns and whatever the specification added to them, as one
# expression. Added rather than replacing: what makes a kernel build is
# the same wherever the tree came from, and a packaging reaching
# somewhere else reaches there as well as here, not instead of it.
#
# 'extra' is a tuple so that this can be cached: the expression is the
# same for every patch in the series.
@functools.lru_cache(maxsize=None)
def build_files(extra=()):
    return re.compile("|".join(list(kernel_rules().build_files) + list(extra)))

# Debian identifies a kernel by architecture, featureset and flavour, and
# a flavour name only means something within its featureset -- amd64's
# realtime kernel and its ordinary one are both the 'amd64' flavour, of
# the 'rt' and 'none' featuresets. So both have to be named to pick one,
# and 'none' is the one nearly everything wants.
DEFAULT_FEATURESET = "none"

# Debian's build profiles for a kernel's tools. The first drops
# linux-kbuild with them, which a module built against that kernel needs;
# the second drops the same tools and keeps it.
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
