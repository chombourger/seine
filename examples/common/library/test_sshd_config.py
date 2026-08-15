#!/usr/bin/python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# run with 'python3 examples/common/library/test_sshd_config.py'

from sshd_config import _edit, _include_ok, _toggle

# appends when the directive is missing
lines, changed = _edit(["# a comment", "Port 22"], "PermitRootLogin", "no", "present")
assert changed
assert lines == ["# a comment", "Port 22", "PermitRootLogin no"]

# replaces the existing active line, in place
lines, changed = _edit(["PermitRootLogin yes", "Port 22"],
                       "PermitRootLogin", "no", "present")
assert changed
assert lines == ["PermitRootLogin no", "Port 22"]

# is idempotent
lines, changed = _edit(["PermitRootLogin no"], "PermitRootLogin", "no", "present")
assert not changed
assert lines == ["PermitRootLogin no"]

# a commented-out line is left alone, not mistaken for an active one
lines, changed = _edit(["#PermitRootLogin yes"], "PermitRootLogin", "no", "present")
assert changed
assert lines == ["#PermitRootLogin yes", "PermitRootLogin no"]

# a duplicate active line collapses to the one sshd would actually use
lines, changed = _edit(["PermitRootLogin yes", "PermitRootLogin no"],
                       "PermitRootLogin", "no", "present")
assert changed
assert lines == ["PermitRootLogin no"]

# state: absent removes the active line and nothing else
lines, changed = _edit(["PermitRootLogin no", "Port 22"],
                       "PermitRootLogin", None, "absent")
assert changed
assert lines == ["Port 22"]

# absent on a directive that was never set is a no-op
lines, changed = _edit(["Port 22"], "PermitRootLogin", None, "absent")
assert not changed
assert lines == ["Port 22"]

# bookworm's shipped sshd_config includes the drop-in directory
assert _include_ok(["# comment", "Include /etc/ssh/sshd_config.d/*.conf"])

# a commented-out Include does not count
assert not _include_ok(["#Include /etc/ssh/sshd_config.d/*.conf"])

# an older sshd_config with no drop-in support at all
assert not _include_ok(["Port 22"])

# adds an algorithm that was not in sshd's resolved default
assert _toggle(["aes256-ctr", "aes128-ctr"], "chacha20-poly1305", "present") == \
    ["aes256-ctr", "aes128-ctr", "chacha20-poly1305"]

# is a no-op if it is already there
assert _toggle(["aes256-ctr", "chacha20-poly1305"], "chacha20-poly1305", "present") == \
    ["aes256-ctr", "chacha20-poly1305"]

# removes one entry, leaves the rest of the default alone
assert _toggle(["aes256-ctr", "3des-cbc", "aes128-ctr"], "3des-cbc", "absent") == \
    ["aes256-ctr", "aes128-ctr"]

# is a no-op if it was never there
assert _toggle(["aes256-ctr"], "3des-cbc", "absent") == ["aes256-ctr"]

print("ok")
