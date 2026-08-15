#!/usr/bin/python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# One directive at a time, first active line wins (sshd's own rule).
# Match blocks and other option-argument sections are out of scope.

DOCUMENTATION = r"""
---
module: sshd_config
short_description: set a sshd_config directive, or toggle one algorithm in it
description:
  - Idempotently sets, updates or removes one C(sshd_config) directive, so
    a playbook does not have to know the file's syntax or worry about a
    duplicate override sshd would silently prefer.
  - Given O(algorithm) instead of O(value), turns one entry of a
    comma-list directive (C(Ciphers), C(MACs), C(KexAlgorithms), ...) on
    or off, leaving the rest of sshd's negotiated default alone -- so
    several tasks, from several playbook fragments, can each turn one
    algorithm on or off without overwriting each other's change.
  - Writes to a drop-in under C(sshd_config.d) by default, so the file
    Debian's own package shipped is never touched -- an upgrade to that
    package cannot conflict with, or silently revert, what this set.
options:
  name:
    description: the directive, e.g. C(PermitRootLogin) or C(Ciphers)
    required: true
    type: str
  value:
    description: the value to set. Mutually exclusive with O(algorithm).
    type: str
  algorithm:
    description:
      - one entry of a comma-list directive to turn on or off, e.g.
        C(chacha20-poly1305@openssh.com) for O(name=Ciphers). Resolved
        against C(sshd -T)'s negotiated default, so what is not named
        here is left exactly as sshd would otherwise have picked.
        Mutually exclusive with O(value).
    type: str
  state:
    description:
      - whether the directive (or, with O(algorithm), the one entry)
        should be present or absent
    choices: [present, absent]
    default: present
    type: str
  path:
    description:
      - path to the config file. Defaults to a drop-in of its own; point
        this at C(/etc/ssh/sshd_config) directly only when there is a
        reason a drop-in cannot be used.
    default: /etc/ssh/sshd_config.d/seine.conf
    type: str
author: seine
"""

EXAMPLES = r"""
- name: disable root login over ssh
  sshd_config:
      name: PermitRootLogin
      value: "no"

- name: disable password authentication
  sshd_config:
      name: PasswordAuthentication
      value: "no"

- name: drop a weak cipher
  sshd_config:
      name: Ciphers
      algorithm: 3des-cbc
      state: absent

- name: allow a legacy cipher some old hardware still needs
  sshd_config:
      name: Ciphers
      algorithm: aes128-cbc
      state: present
"""

RETURN = r"""
changed:
    description: whether the file was modified
    type: bool
    returned: always
"""

import os
import re

from ansible.module_utils.basic import AnsibleModule

# Debian's own openssh-server package has shipped this since bookworm;
# a drop-in this module writes is dead weight without it.
MAIN_CONFIG = "/etc/ssh/sshd_config"
INCLUDE = re.compile(r"^[ \t]*Include[ \t]+.*sshd_config\.d", re.IGNORECASE)


# _include_ok() and _edit() sit apart from main() so test_sshd_config.py
# can test them without going through AnsibleModule.
def _include_ok(main_lines):
    return any(INCLUDE.match(line) for line in main_lines)


def _edit(lines, name, value, state):
    directive = re.compile(r"^[ \t]*%s([ \t]+.*)?$" % re.escape(name),
                           re.IGNORECASE)
    active = [i for i, line in enumerate(lines) if directive.match(line)]

    if state == "absent":
        kept = [line for i, line in enumerate(lines) if i not in active]
        return kept, len(kept) != len(lines)

    wanted = "%s %s" % (name, value)
    if not active:
        return lines + [wanted], True
    first = active[0]
    changed = lines[first] != wanted or len(active) > 1
    new_lines = [wanted if i == first else line
                for i, line in enumerate(lines) if i == first or i not in active]
    return new_lines, changed


# Adds or removes one entry of an already-resolved comma-list.
def _toggle(current, algorithm, state):
    if state == "present" and algorithm not in current:
        current = current + [algorithm]
    elif state == "absent" and algorithm in current:
        current = [a for a in current if a != algorithm]
    return current


# The comma-list _toggle() starts from is sshd's own resolved default, not
# just this dropin's current line -- so a directive no task has touched
# yet still starts from what sshd would otherwise have picked, not an
# empty list.
def _resolve(module, name):
    os.makedirs("/run/sshd", exist_ok=True)  # sshd -T refuses without it
    rc, out, err = module.run_command(["sshd", "-T"])
    if rc != 0:
        module.fail_json(msg="'sshd -T' failed: %s" % err)

    key = name.lower()
    for line in out.splitlines():
        directive, _, value = line.partition(" ")
        if directive == key:
            return value.split(",")
    module.fail_json(msg="'sshd -T' does not report '%s'" % name)


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="str", required=True),
            value=dict(type="str"),
            algorithm=dict(type="str"),
            state=dict(type="str", default="present", choices=["present", "absent"]),
            path=dict(type="str", default="/etc/ssh/sshd_config.d/seine.conf"),
        ),
        mutually_exclusive=[["value", "algorithm"]],
        supports_check_mode=True,
    )

    name = module.params["name"]
    value = module.params["value"]
    algorithm = module.params["algorithm"]
    state = module.params["state"]

    if algorithm is None and value is None and state == "present":
        module.fail_json(msg="'value' is required when state=present")

    path = module.params["path"]
    dropin = os.path.dirname(path) == "/etc/ssh/sshd_config.d"

    if dropin:
        try:
            with open(MAIN_CONFIG) as f:
                if not _include_ok(f.read().splitlines()):
                    module.fail_json(msg="%s does not 'Include' %s -- an "
                        "older sshd_config, or one a playbook rewrote?"
                        % (MAIN_CONFIG, os.path.dirname(path)))
        except FileNotFoundError:
            module.fail_json(msg="%s does not exist" % MAIN_CONFIG)

    if algorithm is not None:
        value = ",".join(_toggle(_resolve(module, name), algorithm, state))
        state = "present"  # always writes the fully resolved list

    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []

    new_lines, changed = _edit(lines, name, value, state)

    if changed and not module.check_mode:
        if dropin:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("\n".join(new_lines) + "\n")

    module.exit_json(changed=changed)


if __name__ == "__main__":
    main()
