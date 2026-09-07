# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# extend(): applies 'extends: kernel:' to a fetched source -- kconfig
# fragments, flavour restriction/derivation, disabling signed kernels --
# then rebuilds debian/control from it.

import os
import re

from seine.utils import WORKDIR

from .config import _write_configs
from .flavour import (_add_derived_flavours, _disable_signed,
                      _restrict_flavour, _set_abi_suffix)


# A kernel is built for exactly one architecture at a time (the parser
# refuses 'scope: both' on a kernel package), so this loops over what the
# package declares rather than the image's architecture.
def extend(builder, package, sourcedir, architectures):
    if package.kernel == False:
        return

    fragments = package.kernel_fragment_files()
    for fragment in fragments:
        if os.path.isfile(fragment) == False:
            raise ValueError("package '%s': no such kernel configuration "
                             "fragment: %s" % (package.source, fragment))

    for architecture in architectures:
        config = os.path.join(sourcedir, "debian", "config", architecture,
                              "config")
        if os.path.isfile(config) == False:
            raise ValueError(
                "package '%s' is built as a kernel, but its source has no "
                "debian/config/%s/config to configure"
                % (package.source, architecture))

        if len(fragments) > 0:
            with open(config, "a") as f:
                for fragment in fragments:
                    f.write("\n# %s, added by seine\n"
                            % os.path.basename(fragment))
                    with open(fragment, "r") as contents:
                        f.write(contents.read())

        _write_configs(package, config)

        # 'derived-flavours' checked first: a package carrying both is
        # more likely to have inherited a plain 'flavour' from an
        # architecture default than to mean both.
        if package.kernel_derived_flavours:
            _add_derived_flavours(package, sourcedir, architecture)
        elif package.kernel_flavour is not None:
            _restrict_flavour(package, sourcedir, architecture)
        if package.kernel_upstream is not None:
            _disable_signed(package, sourcedir, architecture)

    if package.kernel_abi_suffix is not None:
        _set_abi_suffix(package, sourcedir)

    # debian/control lists a binary package per flavour and is generated
    # from the files just edited, so it must be rebuilt before sbuild
    # runs -- otherwise it still builds every flavour the old control
    # file mentions.
    #
    # DEBIAN_KERNEL_DISABLE_SIGNED makes the rebuild matter at all: on
    # architectures that ship a signed kernel, 'linux' normally builds
    # only the -unsigned package, while linux-image-<flavour> depends
    # on the signed one built by a separate linux-signed-<arch> source
    # (needing a key we don't have). Without this, apt keeps installing
    # the distribution's signed kernel and our config change never
    # takes effect. PYTHONDONTWRITEBYTECODE avoids __pycache__ under
    # debian/, which dpkg-source refuses to package.
    builder.builderImage.exec(
        ["debian/rules", "debian/control"],
        volumes=[(os.path.dirname(sourcedir), WORKDIR)],
        workdir="%s/%s" % (WORKDIR, os.path.basename(sourcedir)),
        environment={"PYTHONDONTWRITEBYTECODE": "1",
                     "DEBIAN_KERNEL_DISABLE_SIGNED": "1"},
        check=False)

    _record_abiname(builder, package, sourcedir)

    if package.kernel_flavour is not None or package.kernel_derived_flavours:
        for architecture in architectures:
            _check_flavour(package, sourcedir, architecture)

# The ABI this kernel ended up with, read off the just-regenerated control
# file. Silently skipped if unreadable: a kernel with no modules built
# against it doesn't need one recorded.
def _record_abiname(builder, package, sourcedir):
    control = os.path.join(sourcedir, "debian", "control")
    if os.path.isfile(control) == False:
        return
    with open(control, "r") as f:
        stanzas = f.read().split("\n\n")
    packages = []
    for stanza in stanzas:
        name = re.search(r"^Package:\s*(\S+)$", stanza, re.MULTILINE)
        if name:
            packages.append((name.group(1), []))
    try:
        builder.abinames[package.name] = _abiname(packages)
    except ValueError:
        pass

# debian/rules regenerates debian/control by running a generator that
# reports success by failing (its own message says so), so this checks
# what got built instead of the exit status.
def _check_flavour(package, sourcedir, architecture):
    control = os.path.join(sourcedir, "debian", "control")
    if os.path.isfile(control) == False:
        raise ValueError(
            "package '%s': restricting the kernel left no debian/control "
            "behind" % package.source)

    abi, kernels = _kernel_packages(control, architecture)
    if package.kernel_derived_flavours:
        # Not the spec dictionary flattened again: it may name bases from
        # more than one architecture; only '_add_derived_flavours' knows
        # what it actually built for this one.
        names = sorted(package.kernel_derived_flavours_built)
    else:
        names = [package.kernel_flavour]
    expected = set("linux-image-%s-%s" % (abi, name) for name in names)
    if set(kernels) != expected:
        raise ValueError(
            "package '%s': restricting the kernel for '%s'/'%s' did not "
            "take effect for architecture '%s' -- expected %s, "
            "debian/control built %s instead. The generator that "
            "rewrites it is allowed to fail, so look above for what it "
            "said."
            % (package.source, package.kernel_featureset,
               ", ".join(names) or "none", architecture,
               ", ".join(sorted(expected)) or "none",
               ", ".join(sorted(kernels)) or "none"))

# The kernel image packages built for an architecture, and the ABI name
# they share. Parsed as deb822 rather than the generated makefiles, which
# name non-kernel packages too.
def _kernel_packages(control, architecture):
    packages = []
    with open(control, "r") as f:
        stanzas = f.read().split("\n\n")

    for stanza in stanzas:
        fields = {}
        for line in stanza.split("\n"):
            field = re.match(r"^([A-Za-z-]+):\s*(.*)$", line)
            if field:
                fields[field.group(1)] = field.group(2)
        packages.append((fields.get("Package", ""),
                         fields.get("Architecture", "").split()))

    # linux-image-<abi>-<flavour>: the versioned image packages, one per
    # kernel -- not the metapackage or the debug packages beside them.
    abi = _abiname(packages)
    prefix = "linux-image-%s-" % abi
    return abi, sorted(set(
        name for name, architectures in packages
        if architecture in architectures
        and name.startswith(prefix) and name.endswith("-dbg") == False))

# Debian names a shared linux-headers-<abi>-common package once per kernel
# ABI; read back from there since abiname is derived, not fixed.
def _abiname(packages):
    for name, _ in packages:
        abi = re.match(r"^linux-headers-(.+)-common$", name)
        if abi:
            return abi.group(1)
    raise ValueError(
        "debian/control has no 'linux-headers-<abi>-common' package to "
        "read the kernel's ABI name from")
