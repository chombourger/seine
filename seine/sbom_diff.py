# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Diffs two SBOMs (seine build --sbom) by package name and version. Not
# image-to-image -- seine has no retention convention for built images,
# so this only compares two SPDX files named explicitly.

import json

def _packages(spdx):
    return {p["name"]: p.get("versionInfo", "") for p in spdx.get("packages", [])
           if "name" in p}

# Added/removed/changed packages between two SPDX documents -- +/-/~,
# the same marks 'seine plan's spec diff uses, plus ~ for a version change.
def diff(old_spdx, new_spdx):
    old = _packages(old_spdx)
    new = _packages(new_spdx)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])

    if len(added) == 0 and len(removed) == 0 and len(changed) == 0:
        return "no package differences"

    lines = []
    for name in added:
        lines.append("+ %-32s %s" % (name, new[name]))
    for name in removed:
        lines.append("- %-32s %s" % (name, old[name]))
    for name in changed:
        lines.append("~ %-32s %s -> %s" % (name, old[name], new[name]))
    return "\n".join(lines)

def diff_files(old_path, new_path):
    with open(old_path) as f:
        old_spdx = json.load(f)
    with open(new_path) as f:
        new_spdx = json.load(f)
    return diff(old_spdx, new_spdx)
