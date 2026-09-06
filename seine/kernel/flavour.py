# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Restricting or deriving a kernel's own flavour(s), and the toml/ini
# editing (debian/config/*/config.toml, defines.toml) that takes.

import os
import re
import tomllib


# Says in the source that this kernel is not a signed one, which a
# grafted kernel cannot be. Debian's Secure Boot support is not in its
# packaging: CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT comes from the
# features/all/lockdown patches, which change C source and so are not
# among the patches kept, and the build stops in a check that is right
# to stop it. DEBIAN_KERNEL_DISABLE_SIGNED does not reach that check
# -- it is read when debian/control is generated, here, and the check
# runs later inside the chroot -- so it is said in debian/config,
# where everything downstream of it agrees.
#
# Nothing is lost that was there to lose: the kernel Debian signs is
# built from a source package of its own, from a key we do not have.
# An image wanting the lockdown behaviour needs those patches kept
# and rebased.
def _disable_signed(package, sourcedir, architecture):
    defines = os.path.join(sourcedir, "debian", "config", architecture,
                           "defines.toml")
    if os.path.isfile(defines):
        _toml_set(defines, "build", "enable_signed", "false")

# gencontrol.py picks the '[[debianrelease]]' whose 'name_regex' matches
# the changelog distribution (which stays UNRELEASED) and takes its
# 'abi_suffix' as the kernel's ABI. Rewriting that value, not the match,
# is enough: debian/control, the maintainer scripts and the ABINAME the
# build compiles modules under all read it back from the same place.
def _set_abi_suffix(package, sourcedir):
    path = os.path.join(sourcedir, "debian", "config", "defines.toml")
    with open(path, "r") as f:
        lines = f.readlines()

    blocks = _toml_blocks(lines)
    for position in range(len(blocks) - 1):
        kind, start = blocks[position]
        if kind != "debianrelease":
            continue
        end = blocks[position + 1][1]
        if _toml_value(lines, start, end, "name_regex") == "UNRELEASED":
            break
    else:
        raise ValueError(
            "package '%s': debian/config/defines.toml has no "
            "[[debianrelease]] for 'UNRELEASED' to give 'abi-suffix' to"
            % package.source)

    value = "'%s'" % package.kernel_abi_suffix
    existing = _toml_line(lines, start, end, "abi_suffix")
    if existing is not None:
        lines[existing] = "abi_suffix = %s\n" % value
    else:
        lines.insert(start + 1, "abi_suffix = %s\n" % value)

    with open(path, "w") as f:
        f.writelines(lines)

# Cuts the build down to the one kernel asked for. Debian builds every
# featureset and flavour an architecture has, and for the kernel each
# of those is a full build -- on amd64, a cloud flavour and a realtime
# kernel besides the one an appliance wants.
#
# Debian described its kernels in an ini-like debian/config/*/defines
# and moved to a defines.toml. Both are still in the archive at once,
# so which one the source carries decides, not the release built for.
def _restrict_flavour(package, sourcedir, architecture):
    config = os.path.join(sourcedir, "debian", "config")
    defines = os.path.join(config, architecture, "defines.toml")
    if os.path.isfile(defines) == False:
        return _restrict_flavour_ini(package, sourcedir, architecture)

    _restrict_flavour_toml(package, defines, architecture,
                           ["flavour", "featureset"])
    # The featuresets again, where they are declared for every
    # architecture at once. Disabling one for amd64 alone leaves the
    # packages that do not depend on an architecture -- the headers
    # every flavour of a kernel shares -- still being built for it,
    # and a featureset whose patches the graft dropped cannot be.
    _restrict_flavour_toml(package, os.path.join(config, "defines.toml"),
                           architecture, ["featureset"])

# The toml describes flavours and featuresets as arrays of tables, each
# taking an 'enable' that defaults to true, and a kernel is built only
# when every level of the hierarchy says so. So the entries not wanted
# are said false rather than removed: deleting table blocks means
# getting the boundaries of a nested one right or building something
# else silently.
def _restrict_flavour_toml(package, path, architecture, kinds):
    with open(path, "rb") as f:
        defines = tomllib.load(f)

    wanted = {kind: name for kind, name in
              [("flavour", package.kernel_flavour),
               ("featureset", package.kernel_featureset)] if kind in kinds}
    for kind, name in wanted.items():
        names = [entry["name"] for entry in defines.get(kind, [])]
        if name not in names:
            raise ValueError(
                "package '%s': architecture '%s' has no '%s' kernel %s, "
                "expected one of %s"
                % (package.source, architecture, name, kind,
                   ", ".join(sorted(names))))

    with open(path, "r") as f:
        lines = f.readlines()
    blocks = _toml_blocks(lines)

    # Back to front, so the edits do not move the blocks still to come.
    for position in reversed(range(len(blocks) - 1)):
        kind, start = blocks[position]
        end = blocks[position + 1][1]
        if kind not in wanted:
            continue
        name = _toml_value(lines, start, end, "name")
        if name is None or name == wanted[kind]:
            continue
        enabled = _toml_line(lines, start, end, "enable")
        if enabled is not None:
            lines[enabled] = "enable = false\n"
        else:
            lines.insert(start + 1, "enable = false\n")

    with open(path, "w") as f:
        f.writelines(lines)

# For every base named in 'derived-flavours' that is this architecture's,
# copies its flavour block once per name derived, each pointed at a
# kconfig fragment of its own; every original block is then disabled,
# base or not. What belongs to this architecture is worked out from its
# own defines.toml, since one dictionary may name bases from several.
def _add_derived_flavours(package, sourcedir, architecture):
    path = os.path.join(sourcedir, "debian", "config", architecture,
                        "defines.toml")
    if os.path.isfile(path) == False:
        # The ini format's flavour list is just names, with nowhere to
        # hang a fragment of its own the way '[flavour.build]' does.
        raise ValueError(
            "package '%s': 'derived-flavours' needs the toml defines "
            "format, which architecture '%s' does not have"
            % (package.source, architecture))
    with open(path, "r") as f:
        lines = f.readlines()
    blocks = _toml_blocks(lines)

    # Every '[[flavour]]' block's position and content, keyed by name.
    positions = []
    originals = {}
    for position in range(len(blocks) - 1):
        kind, start = blocks[position]
        if kind != "flavour":
            continue
        end = blocks[position + 1][1]
        positions.append((start, end))
        originals[_toml_value(lines, start, end, "name")] = list(lines[start:end])

    # 'derived-flavours' flattened to one name-to-(base, fragments) map.
    # A base is not always an original Debian flavour: it may be another
    # name in this map, one flavour derived from one already derived.
    flat = {}
    for base, derived in package.kernel_derived_flavours.items():
        for name, fragments in derived.items():
            flat[name] = (base, fragments)

    # A name is for this architecture only if its base -- directly, or
    # through a chain of other derived names -- eventually resolves to
    # one of this architecture's own originals. 'seen' guards a cycle:
    # neither end of one resolves to anything, so a cycle is simply not
    # for this architecture rather than an infinite recursion.
    def resolves_here(base, seen=()):
        if base in originals:
            return True
        if base not in flat or base in seen:
            return False
        return resolves_here(flat[base][0], seen + (base,))
    scope = set(name for name in flat if resolves_here(flat[name][0]))

    # Nothing here is this architecture's: leave every original as
    # Debian shipped it. '_check_flavour' catches the mismatch -- a
    # cycle, a missing base, and this all look the same from here.
    if len(scope) == 0:
        package.kernel_derived_flavours_built = set()
        return

    # Depth-first over the names in scope, so a base is always
    # materialized before what derives from it copies it.
    order = []
    def visit(name):
        if name in order or name not in scope:
            return
        visit(flat[name][0])
        order.append(name)
    for name in scope:
        visit(name)

    config_dir = os.path.dirname(path)
    made = dict(originals)
    for name in order:
        base, fragments = flat[name]
        fragment_name = "%s.config" % name
        with open(os.path.join(config_dir, fragment_name), "w") as f:
            for fragment in fragments:
                f.write("# %s, added by seine\n" % os.path.basename(fragment))
                with open(fragment, "r") as contents:
                    f.write(contents.read())
                f.write("\n")
        made[name] = _derive_flavour_block(
            made[base], 0, len(made[base]), name,
            "%s/%s" % (architecture, fragment_name))

    # Every original is disabled, base or not: it is replaced, not kept
    # beside its derived flavours. Back to front, so editing one block
    # never moves the line numbers of one still to come.
    new_lines = list(lines)
    shift = 0
    for start, end in reversed(positions):
        block = new_lines[start:end]
        enabled = _toml_line(block, 0, len(block), "enable")
        if enabled is None:
            block.insert(1, "enable = false\n")
            shift += 1
        else:
            block[enabled] = "enable = false\n"
        new_lines[start:end] = block

    insert_at = positions[-1][1] + shift if positions else len(new_lines)
    derived_lines = []
    for name in order:
        derived_lines += made[name]
    new_lines[insert_at:insert_at] = derived_lines

    with open(path, "w") as f:
        f.writelines(new_lines)

    # What was actually derived for this architecture, for
    # '_check_flavour' to compare debian/control against -- it cannot
    # work this out itself: by the time it runs, the edits above are
    # already made, and the dictionary alone does not say which
    # architecture a name was for.
    package.kernel_derived_flavours_built = set(order)

# One copy of a '[[flavour]]' block, renamed and pointed at a config
# fragment of its own through '[flavour.build] config'. Appended to an
# existing 'config' list rather than opening a second '[flavour.build]'
# when the base already has one -- a base flavour that is itself derived
# from another, like 'cloud-amd64', already carries one.
def _derive_flavour_block(lines, start, end, name, config_path):
    block = list(lines[start:end])
    idx = _toml_line(block, 0, len(block), "name")
    block[idx] = "name = '%s'\n" % name

    build = _toml_subtable(block, 0, len(block), "flavour.build")
    if build is None:
        insert_at = len(block)
        for i, line in enumerate(block):
            if line.strip().startswith("[flavour."):
                insert_at = i
                break
        block[insert_at:insert_at] = [
            "[flavour.build]\n", "config = ['%s']\n" % config_path]
        return block

    build_start, build_end = build
    existing = _toml_line(block, build_start, build_end, "config")
    if existing is None:
        block.insert(build_start + 1, "config = ['%s']\n" % config_path)
    else:
        block[existing] = re.sub(
            r"\]\s*\n?$", ", '%s']\n" % config_path, block[existing])
    return block

# A '[flavour.<name>]' sub-table within a '[[flavour]]' block's own line
# range -- '_toml_blocks' does not return these, a dotted header being a
# table nested within the block rather than one of its own.
def _toml_subtable(lines, start, end, name):
    header = "[%s]" % name
    for i in range(start, end):
        if lines[i].strip() == header:
            for j in range(i + 1, end):
                if lines[j].strip().startswith("["):
                    return i, j
            return i, end
    return None

# Every table header, with the block it opens. A dotted name --
# [flavour.defs] under [[flavour]] -- is a table within the block
# rather than one of its own; an undotted one always starts a block,
# including the [[flavour]] that follows another [[flavour]]. The last
# entry has no name and marks where the file ends.
def _toml_blocks(lines):
    blocks = []
    for index, line in enumerate(lines):
        header = re.match(r"^\s*\[\[?([A-Za-z0-9_.-]+)\]?\]\s*$", line)
        if header is None or "." in header.group(1):
            continue
        blocks.append((header.group(1), index))
    blocks.append((None, len(lines)))
    return blocks

# Sets a key at the top level of a named block, adding the block if the
# file has none.
def _toml_set(path, block, key, value):
    with open(path, "r") as f:
        lines = f.readlines()

    blocks = _toml_blocks(lines)
    for position in range(len(blocks) - 1):
        kind, start = blocks[position]
        if kind != block:
            continue
        end = blocks[position + 1][1]
        existing = _toml_line(lines, start, end, key)
        if existing is not None:
            lines[existing] = "%s = %s\n" % (key, value)
        else:
            lines.insert(start + 1, "%s = %s\n" % (key, value))
        break
    else:
        lines += ["\n[%s]\n" % block, "%s = %s\n" % (key, value)]

    with open(path, "w") as f:
        f.writelines(lines)

# A key at the top level of a toml block, i.e. before its first nested
# table. Only the string and boolean scalars this needs are understood.
def _toml_line(lines, start, end, key):
    for index in range(start + 1, end):
        if re.match(r"^\s*\[", lines[index]):
            break
        if re.match(r"^\s*%s\s*=" % key, lines[index]):
            return index
    return None

def _toml_value(lines, start, end, key):
    index = _toml_line(lines, start, end, key)
    if index is None:
        return None
    value = re.match(r"^\s*%s\s*=\s*['\"](.*)['\"]" % key, lines[index])
    return value.group(1) if value else None

def _restrict_flavour_ini(package, sourcedir, architecture):
    root = os.path.join(sourcedir, "debian", "config", architecture)
    featuresets = _defines_list(os.path.join(root, "defines"),
                                "featuresets")
    if package.kernel_featureset not in featuresets:
        raise ValueError(
            "package '%s': architecture '%s' has no '%s' kernel "
            "featureset, expected one of %s"
            % (package.source, architecture, package.kernel_featureset,
               ", ".join(sorted(featuresets))))

    defines = os.path.join(root, package.kernel_featureset, "defines")
    flavours = _defines_list(defines, "flavours")
    if package.kernel_flavour not in flavours:
        raise ValueError(
            "package '%s': the '%s' featureset of architecture '%s' has "
            "no '%s' kernel flavour, expected one of %s"
            % (package.source, package.kernel_featureset, architecture,
               package.kernel_flavour, ", ".join(sorted(flavours))))

    _defines_replace(os.path.join(root, "defines"), "featuresets",
                     [package.kernel_featureset])
    _defines_replace(defines, "flavours", [package.kernel_flavour])
    # Both may name a flavour that has just been removed.
    _defines_set(defines, "default-flavour", package.kernel_flavour)
    _defines_set(defines, "quick-flavour", package.kernel_flavour)

# debian/config/*/defines are ini-like, with list values written one
# per line and indented under their key.
def _defines_list(path, key):
    values = []
    collecting = False
    with open(path, "r") as f:
        for line in f:
            if line.strip() == "%s:" % key:
                collecting = True
            elif collecting:
                if line.startswith(" ") and len(line.strip()) > 0:
                    values.append(line.strip())
                else:
                    break
    return values

def _defines_replace(path, key, values):
    with open(path, "r") as f:
        lines = f.readlines()

    out = []
    skipping = False
    for line in lines:
        if line.strip() == "%s:" % key:
            out.append(line)
            out += [" %s\n" % v for v in values]
            skipping = True
        elif skipping:
            if line.startswith(" ") and len(line.strip()) > 0:
                continue
            skipping = False
            out.append(line)
        else:
            out.append(line)

    with open(path, "w") as f:
        f.writelines(out)

def _defines_set(path, key, value):
    with open(path, "r") as f:
        lines = f.readlines()
    out = ["%s: %s\n" % (key, value) if line.startswith("%s:" % key) else line
           for line in lines]
    with open(path, "w") as f:
        f.writelines(out)
