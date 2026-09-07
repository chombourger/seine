# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Filesystem completion for '@fragment' in the prompt: plain os.listdir,
# kept apart from the widget that shows it so it's testable without a
# running App.

import os

MAX_MATCHES = 100

# 'fragment' is everything typed after '@'. Each match fully replaces
# it, not just the missing tail; a directory gets a trailing '/'.
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
