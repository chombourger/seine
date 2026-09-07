# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# gist-list/show/create/delete: reusable spec fragments kept outside
# any one project.

import os

from . import Plan, Preview, Tool, _no_args, _redacted_diff

# A gist lives outside any project, so this reads no active spec. Each
# line carries the absolute path too, so side-load doesn't need a
# follow-up lookup.
def _tool_gist_list(app, arguments):
    from seine import gists
    found = gists.list_gists()
    if not found:
        return "no gists yet -- %s" % gists.default_dir()
    lines = []
    for name, description in found:
        path = gists.path_for(name)
        lines.append("%s -- %s\n  %s" % (name, description or "(no description)", path))
    return "\n".join(lines)

def _tool_gist_show(app, arguments):
    from seine import gists
    name = arguments.get("name")
    if not name:
        return "gist-show needs 'name'"
    try:
        return gists.read(name)
    except (OSError, ValueError) as e:
        return "could not read: %s" % e

# Same "brand new file, diff against nothing" shape as spec-create's,
# but writing under gists.default_dir() instead of beside a loaded
# file -- a gist lives outside any project, so there's no 'redact:'
# to apply (spec=None).
def _gist_create_plan(app, arguments):
    name = arguments.get("name")
    description = arguments.get("description")
    content = arguments.get("content")
    if not name or not description or content is None:
        return Plan(False, "gist-create needs 'name', 'description', and "
                    "'content' (the fragment, as YAML)")
    from seine import gists
    try:
        path = gists.path_for(name)
    except ValueError as e:
        return Plan(False, str(e))
    if os.path.exists(path):
        return Plan(False, "gist '%s' already exists" % name)

    import yaml
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as e:
        return Plan(False, "'content' is not valid YAML: %s" % e)

    body = content if content.endswith("\n") else content + "\n"
    new_text = "# %s\n%s" % (description, body)
    diff = _redacted_diff(None, "", new_text, "/dev/null", path)
    return Plan(True, diff, path, new_text)

def _tool_gist_create_preview(app, arguments):
    plan = _gist_create_plan(app, arguments)
    return Preview(plan.ok, plan.message)

# Recomputes the plan rather than trusting the preview -- another gist
# of the same name could have appeared since.
def _tool_gist_create(app, arguments):
    plan = _gist_create_plan(app, arguments)
    if not plan.ok:
        return plan.message
    from seine import gists
    try:
        gists.create(arguments["name"], arguments["description"], arguments["content"])
    except ValueError as e:
        return "could not create: %s" % e
    return "created %s" % plan.path

def _tool_gist_delete_preview(app, arguments):
    from seine import gists
    name = arguments.get("name")
    if not name:
        return Preview(False, "gist-delete needs 'name'")
    try:
        content = gists.read(name)
    except (OSError, ValueError) as e:
        return Preview(False, str(e))
    path = gists.path_for(name)
    diff = _redacted_diff(None, content, "", path, "/dev/null")
    return Preview(True, diff)

def _tool_gist_delete(app, arguments):
    from seine import gists
    name = arguments.get("name")
    if not name:
        return "gist-delete needs 'name'"
    try:
        gists.delete(name)
    except (OSError, ValueError) as e:
        return "could not delete: %s" % e
    return "deleted %s" % name

TOOLS = [
    Tool("gist-list", "List reusable spec fragments kept outside any one "
        "project -- 'gist ls' on the command line. Each line is a name, "
        "its description, and the absolute path to hand straight to "
        "side-load. Check this before drafting a fragment from scratch "
        "for something that sounds like a repeat of earlier work.",
        _no_args(), False, _tool_gist_list),
    Tool("gist-show", "Print one gist's raw file content, description "
        "line included -- the same thing 'gist show NAME' prints on the "
        "command line.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a gist name, from gist-list"}},
         "required": ["name"]},
        False, _tool_gist_show),
    Tool("gist-create", "Save a spec fragment as a reusable gist, kept "
        "outside any one project so it can be side-loaded again in a "
        "different one later. Refused if the name is already taken. A "
        "person reviews the whole new file (shown as a diff against "
        "nothing) before it's written, same as spec-create. Offer this "
        "as an alternative to a project's own 'requires:' permanence "
        "once a fragment has proven itself (side-load, and a build if "
        "it rebuilds something) and looks like something worth reusing "
        "elsewhere, not only worth keeping in this one project.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "kebab-case, letters/"
                                                "digits/hyphens only"},
                        "description": {"type": "string",
                                        "description": "one line, shown by "
                                                       "gist-list"},
                        "content": {"type": "string",
                                   "description": "the fragment, as YAML"}},
         "required": ["name", "description", "content"]},
        True, _tool_gist_create, _tool_gist_create_preview),
    Tool("gist-delete", "Permanently remove a gist -- a person reviews "
        "its content (shown as a diff against nothing removed, i.e. "
        "every line dropped) before it's deleted, same as any other "
        "consequential action. Only removes the gist itself, never a "
        "project that side-loaded it before.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "a gist name, from gist-list"}},
         "required": ["name"]},
        True, _tool_gist_delete, _tool_gist_delete_preview),
]
