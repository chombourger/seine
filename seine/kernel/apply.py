# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# extend(): applies 'extends: kernel:' to a fetched source once it is
# in hand -- the kconfig fragments, flavour restriction/derivation, and
# what the graft needs disabled -- then rebuilds debian/control from it.

import os
import re

from seine.utils import WORKDIR

from .config import _write_configs
from .flavour import (_add_derived_flavours, _disable_signed,
                      _restrict_flavour, _set_abi_suffix)


# Applies the 'extends: kernel:' settings to a fetched kernel source.
#
# Debian assembles each kernel's configuration from a stack of kconfig
# files, the last of which is the architecture's own -- appending to it
# puts the specification's fragments last, where they win. The kernel's
# own 'oldconfig' then turns off whatever the disabled options were
# holding up, so a fragment says what it means rather than having to
# list every symbol underneath it.
#
# None of this needs a patch: the configuration lives in debian/, which
# a "3.0 (quilt)" package lets us edit directly.
#
# Per architecture the package is built for, which for a kernel is
# exactly one: the parser refuses 'scope: both' on a kernel, since a
# flavour is a name within an architecture and one cannot be right for
# two of them. The loop is over what the package says it is built for
# rather than over the image's architecture, so the two cannot drift.
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

        # 'derived-flavours' first: a package carrying both is far more
        # likely to have inherited a plain 'flavour' from an
        # architecture file's 'defaults' than to mean both at once, and
        # naming a base already says which Debian flavour 'flavour'
        # would have.
        if package.kernel_derived_flavours:
            _add_derived_flavours(package, sourcedir, architecture)
        elif package.kernel_flavour is not None:
            _restrict_flavour(package, sourcedir, architecture)
        if package.kernel_upstream is not None:
            _disable_signed(package, sourcedir, architecture)

    # One setting in the top-level defines.toml, unlike the edits above:
    # no per-architecture loop needed.
    if package.kernel_abi_suffix is not None:
        _set_abi_suffix(package, sourcedir)

    # debian/control lists a binary package per flavour and is generated
    # from the files edited above, so it has to be rebuilt before the
    # source package is; sbuild would otherwise build every flavour the
    # unmodified control file still mentions.
    #
    # DEBIAN_KERNEL_DISABLE_SIGNED is what makes the rebuild reachable
    # at all. On the architectures that ship a signed kernel, 'linux'
    # builds linux-image-<abi>-<flavour>-unsigned, and the package the
    # linux-image-<flavour> metapackage actually depends on --
    # linux-image-<abi>-<flavour> -- is built by a *different* source
    # package, linux-signed-<arch>, which takes the unsigned one and
    # signs it with a key we do not have. Rebuilding 'linux' alone
    # therefore produces a kernel nothing installs: apt keeps taking
    # the distribution's signed one, and the image looks fine while
    # containing none of the configuration asked for.
    #
    # Turning signing off makes 'linux' build that name itself, which
    # the pin then prefers. A locally rebuilt kernel could not have
    # carried Debian's signature in any case; Secure Boot with one
    # needs a key of your own.
    #
    # PYTHONDONTWRITEBYTECODE: the generator is written in python and
    # leaves __pycache__ behind in debian/, which dpkg-source then
    # refuses to put in a source package ("unwanted binary file").
    builder.builderImage.exec(
        ["debian/rules", "debian/control"],
        volumes=[(os.path.dirname(sourcedir), WORKDIR)],
        workdir="%s/%s" % (WORKDIR, os.path.basename(sourcedir)),
        environment={"PYTHONDONTWRITEBYTECODE": "1",
                     "DEBIAN_KERNEL_DISABLE_SIGNED": "1"},
        check=False)

    # What this kernel ended up calling itself, kept for the modules
    # built against it. Read here rather than worked out later: it is
    # in the control file that was just regenerated, and the module
    # that needs it is built after this.
    _record_abiname(builder, package, sourcedir)

    if package.kernel_flavour is not None or package.kernel_derived_flavours:
        for architecture in architectures:
            _check_flavour(package, sourcedir, architecture)

# The ABI the packaging gave this kernel, off the control file it just
# generated. Absent rather than fatal when it cannot be read: a kernel
# nothing builds modules against does not need one, and the module
# that does need it says so itself, naming the kernel.
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

# Restricting the build only takes effect once the rules have been
# regenerated from the edited defines, and that regeneration reports
# success by failing -- its own message says so. Rather than read
# anything into its exit status, check what it produced: the generated
# rules carry one setup target per kernel, named by featureset and
# flavour, and exactly the one asked for should be left.
#
# Checks what the built names actually are, not only how many: a count
# that happens to match hides a generator that built the wrong flavour.
# 'derived-flavours' expects one image per name derived, rather than
# one for 'flavour' -- the same check, widened.
def _check_flavour(package, sourcedir, architecture):
    control = os.path.join(sourcedir, "debian", "control")
    if os.path.isfile(control) == False:
        raise ValueError(
            "package '%s': restricting the kernel left no debian/control "
            "behind" % package.source)

    abi, kernels = _kernel_packages(control, architecture)
    if package.kernel_derived_flavours:
        # Not the dictionary flattened again: it may name bases from
        # more than one architecture, and only '_add_derived_flavours'
        # knows which names it actually made for this one.
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

# The kernel image packages debian/control builds for an architecture,
# and the ABI name they share. Read as the deb822 it is, from the binary
# packages -- the generated makefiles write targets on one line, name
# non-kernel families too, and are the wrong thing to parse for this.
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

    # linux-image-<abi>-<flavour>, the versioned image packages, one
    # per kernel -- as opposed to the metapackage linux-image-<flavour>
    # pointing at one of them, and the debug packages beside them.
    abi = _abiname(packages)
    prefix = "linux-image-%s-" % abi
    return abi, sorted(set(
        name for name, architectures in packages
        if architecture in architectures
        and name.startswith(prefix) and name.endswith("-dbg") == False))

# The ABI name the packaging gave this kernel, which is the only thing
# separating a versioned image package from the metapackage pointing
# at it. It is read back rather than predicted: it is built from the
# upstream version, the abiname in debian/config and what the
# changelog's distribution earns it, and the UNRELEASED entry a local
# rebuild has to carry changes its shape. A pattern written for one
# shape finds no kernels at all and calls that a failed restriction.
#
# Debian builds one headers package shared by every flavour of a
# kernel and names it for the ABI alone, which is what is read here.
def _abiname(packages):
    for name, _ in packages:
        abi = re.match(r"^linux-headers-(.+)-common$", name)
        if abi:
            return abi.group(1)
    raise ValueError(
        "debian/control has no 'linux-headers-<abi>-common' package to "
        "read the kernel's ABI name from")
