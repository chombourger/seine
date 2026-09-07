# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Restricting or deriving a kernel's own flavour(s), and the toml/ini
# editing (debian/config/*/config.toml, defines.toml) that takes.

import os
import re
import tomllib


# Marks the source as unsigned, which a grafted kernel cannot be (Secure
# Boot lockdown comes from patches we don't keep). Set in debian/config
# rather than a kconfig symbol, since that's where gencontrol.py reads it
# when generating debian/control.
def _disable_signed(package, sourcedir, architecture):
    defines = os.path.join(sourcedir, "debian", "config", architecture,
                           "defines.toml")
    if os.path.isfile(defines):
        _toml_set(defines, "build", "enable_signed", "false")

# gencontrol.py picks the '[[debianrelease]]' matching the changelog
# distribution (always UNRELEASED for us) and takes its 'abi_suffix' as
# the kernel's ABI; rewriting that value is enough.
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

# Cuts the build down to the one flavour/featureset asked for. Debian
# builds every flavour of an architecture by default; the source may use
# the older ini defines or the newer defines.toml -- whichever it has.
def _restrict_flavour(package, sourcedir, architecture):
    config = os.path.join(sourcedir, "debian", "config")
    defines = os.path.join(config, architecture, "defines.toml")
    if os.path.isfile(defines) == False:
        return _restrict_flavour_ini(package, sourcedir, architecture)

    _restrict_flavour_toml(package, defines, architecture,
                           ["flavour", "featureset"])
    # Featuresets are also declared at the top level, for every
    # architecture at once -- disable there too, or arch-independent
    # packages (e.g. shared headers) still build for a dropped featureset.
    _restrict_flavour_toml(package, os.path.join(config, "defines.toml"),
                           architecture, ["featureset"])

# Flavours/featuresets are arrays of tables each taking an 'enable' that
# defaults true; unwanted entries are set false rather than deleted, since
# deleting a nested table block correctly is fiddlier than flipping a flag.
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

    # Back to front, so edits don't shift the position of blocks not yet
    # processed.
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

# For every base named in 'derived-flavours' that belongs to this
# architecture, copies its flavour block once per derived name, each
# pointed at its own kconfig fragment; every original block is disabled.
def _add_derived_flavours(package, sourcedir, architecture):
    path = os.path.join(sourcedir, "debian", "config", architecture,
                        "defines.toml")
    if os.path.isfile(path) == False:
        raise ValueError(
            "package '%s': 'derived-flavours' needs the toml defines "
            "format, which architecture '%s' does not have"
            % (package.source, architecture))
    with open(path, "r") as f:
        lines = f.readlines()
    blocks = _toml_blocks(lines)

    positions = []
    originals = {}
    for position in range(len(blocks) - 1):
        kind, start = blocks[position]
        if kind != "flavour":
            continue
        end = blocks[position + 1][1]
        positions.append((start, end))
        originals[_toml_value(lines, start, end, "name")] = list(lines[start:end])

    # 'derived-flavours' flattened to name -> (base, fragments). A base
    # may itself be another derived name, not just an original Debian one.
    flat = {}
    for base, derived in package.kernel_derived_flavours.items():
        for name, fragments in derived.items():
            flat[name] = (base, fragments)

    # A name belongs to this architecture only if its base chain
    # eventually resolves to one of this architecture's originals.
    # 'seen' guards against a cycle, which just resolves to False.
    def resolves_here(base, seen=()):
        if base in originals:
            return True
        if base not in flat or base in seen:
            return False
        return resolves_here(flat[base][0], seen + (base,))
    scope = set(name for name in flat if resolves_here(flat[name][0]))

    # Nothing here belongs to this architecture: leave it untouched.
    # '_check_flavour' will report the mismatch (missing base, cycle,
    # wrong architecture all look the same from here).
    if len(scope) == 0:
        package.kernel_derived_flavours_built = set()
        return

    # Depth-first so a base is always materialized before what derives
    # from it copies it.
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

    # Every original is disabled, base or not -- it's replaced, not kept
    # beside its derived flavours. Back to front so line numbers stay valid.
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

    # What was actually derived for this architecture -- '_check_flavour'
    # needs this since it can't work it out from the spec alone.
    package.kernel_derived_flavours_built = set(order)

# One copy of a '[[flavour]]' block, renamed and pointed at a config
# fragment through '[flavour.build] config'. Appends to an existing
# 'config' list rather than opening a second '[flavour.build]', since a
# base flavour that's itself derived (e.g. 'cloud-amd64') may already have one.
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

# A '[flavour.<name>]' sub-table nested within a '[[flavour]]' block's own
# line range. '_toml_blocks' doesn't return these -- a dotted header is
# nested, not a block of its own.
def _toml_subtable(lines, start, end, name):
    header = "[%s]" % name
    for i in range(start, end):
        if lines[i].strip() == header:
            for j in range(i + 1, end):
                if lines[j].strip().startswith("["):
                    return i, j
            return i, end
    return None

# Every top-level table header (undotted, including repeated [[array]]
# headers), with the block it opens. The last entry has no name and marks
# end of file.
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
    # Both may still name a flavour that was just removed.
    _defines_set(defines, "default-flavour", package.kernel_flavour)
    _defines_set(defines, "quick-flavour", package.kernel_flavour)

# debian/config/*/defines are ini-like: list values written one per line,
# indented under their key.
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
