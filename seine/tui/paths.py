# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Filesystem completion for '@fragment' in the prompt -- plain os.listdir,
# no new dependency, kept apart from the widget that shows it so it is
# testable without a running App (the same split as render.py).

import os

MAX_MATCHES = 100

# 'fragment' is everything typed after '@'. Each match is a full
# replacement for it, not just the missing tail -- a directory gets a
# trailing '/' so completing again needs nothing special from the caller.
def complete(fragment):
    if fragment.startswith("/"):
        slash = fragment.rfind("/")
        directory = fragment[:slash] or "/"
        name = fragment[slash + 1:]
    elif "/" in fragment:
        slash = fragment.rfind("/")
        directory = fragment[:slash]
        name = fragment[slash + 1:]
    else:
        directory = "."
        name = fragment

    try:
        entries = os.listdir(directory)
    except OSError:
        return []

    matches = []
    for entry in sorted(entries):
        if entry.startswith(".") and not name.startswith("."):
            continue
        if not entry.startswith(name):
            continue
        full = entry if directory == "." else os.path.join(directory, entry)
        if os.path.isdir(full):
            full += "/"
        matches.append(full)
    return matches[:MAX_MATCHES]
