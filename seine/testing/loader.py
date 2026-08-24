# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Compiles one 'test:' entry -- a spec's own list of them, the same
# section every other part of a specification lives in (a fragment
# 'requires:'s in its own suite beside whatever it tests, merged by
# name across fragments the way 'packages:' is; see
# BuildCmd._merge_tests()) -- onto robot.
# running's programmatic model (TestSuite/UserKeyword/If/For/While/Try,
# all built with .body.create_*() calls) rather than generating '.robot'
# text -- no second parser, and no syntax to keep in sync with Robot's
# own. IF/FOR/WHILE/TRY/keywords/variables/tags/setup/teardown are
# Robot's; what this module adds is only the step shape a test author
# writes and how it maps onto those.
#
# A step is one of:
#   "Some Keyword  arg1  arg2"          -- Robot's own text call, verbatim
#   {call: NAME, args: [...], assign: VAR}
#   {snake_name: ARGS}                  -- shorthand, see _keyword_step()
#   {if: COND, then: [...], elif: [{condition:, then:}...], else: [...]}
#   {for_each: {as: VAR, in: LIST|VAR}, do: [...]}
#   {for_range: {as: VAR, start:, stop:, step:}, do: [...]}
#   {while: COND, limit: DURATION, do: [...]}
#   {retry_until: COND, timeout: DURATION, interval: DURATION, do: [...]}
#   {try: [...], except: [{pattern:, type:, do:}...], else: [...], finally: [...]}
#   {break: true} / {continue: true}
#   {set: {name: VAR, value: EXPR}}
#
# Keyword libraries are always seine's own (target/image/observation --
# see library/), plus whatever 'library:' names; assertions are Robot's
# own BuiltIn (Should Be Equal, Should Contain, ...), called through the
# same {snake_name: ARGS} shorthand as a seine keyword -- one call syntax
# for both, no separate 'assert:' step type to maintain.

DEFAULT_LIBRARIES = [
    "seine.testing.library.target.TargetLibrary",
    "seine.testing.library.image.ImageLibrary",
    "seine.testing.library.observation.ObservationLibrary",
]

# Control-flow keys, checked before the generic {snake_name: ARGS}
# shorthand -- a step naming one of these is never mistaken for a
# keyword call, so a library is free to define a keyword called (say)
# 'While' without colliding with the step type.
_CONTROL_KEYS = ("if", "for_each", "for_range", "while", "retry_until",
                 "try", "break", "continue", "set", "call")

class LoadError(ValueError):
    pass

# snake_case -> Title Case, so 'power_cycle' calls the 'Power Cycle'
# keyword and 'should_contain' calls Robot's own BuiltIn 'Should Contain'
# -- the same name whichever library it turns out to live in.
def _keyword_name(snake):
    return " ".join(word.capitalize() for word in snake.split("_"))

def _text(value):
    return value if isinstance(value, str) else str(value)

def _assign_list(name):
    if name is None:
        return []
    return [name if name.startswith("${") else "${%s}" % name]

# {call: NAME, args:, assign:} or the {snake_name: ARGS} shorthand --
# both end up as one body.create_keyword() call.
def _keyword_step(step):
    if "call" in step:
        name = step["call"]
        args = [_text(a) for a in step.get("args", [])]
        named = {k: _text(v) for k, v in (step.get("named") or {}).items()}
        assign = step.get("assign")
    else:
        if len(step) != 1:
            raise LoadError("a keyword step takes one key, got %r" % (step,))
        (key, value), = step.items()
        name = _keyword_name(key)
        if isinstance(value, dict):
            value = dict(value)
            assign = value.pop("assign", None)
            args, named = [], {k: _text(v) for k, v in value.items()}
        elif isinstance(value, list):
            args, named, assign = [_text(v) for v in value], {}, None
        elif value in (None, {}):
            args, named, assign = [], {}, None
        else:
            args, named, assign = [_text(value)], {}, None
    return name, args, named, assign

def _add_keyword(body, step):
    name, args, named, assign = _keyword_step(step)
    body.create_keyword(name=name, args=args, named_args=named or None,
                        assign=_assign_list(assign))

def _add_if(body, step):
    node = body.create_if()
    branch = node.body.create_branch(type=node.IF, condition=step["if"])
    _compile_steps(branch.body, step.get("then", []))
    for clause in step.get("elif", []):
        branch = node.body.create_branch(type=node.ELSE_IF, condition=clause["condition"])
        _compile_steps(branch.body, clause.get("then", []))
    if "else" in step:
        branch = node.body.create_branch(type=node.ELSE)
        _compile_steps(branch.body, step["else"])

def _add_for_each(body, step):
    spec = step["for_each"]
    values = spec["in"]
    values = values if isinstance(values, list) else [_text(values)]
    node = body.create_for(assign=_assign_list(spec["as"]), flavor="IN",
                           values=[_text(v) for v in values])
    _compile_steps(node.body, step.get("do", []))

def _add_for_range(body, step):
    spec = step["for_range"]
    values = [_text(spec["start"]), _text(spec["stop"])]
    if "step" in spec:
        values.append(_text(spec["step"]))
    node = body.create_for(assign=_assign_list(spec["as"]), flavor="IN RANGE", values=values)
    _compile_steps(node.body, step.get("do", []))

def _add_while(body, step):
    node = body.create_while(condition=step["while"], limit=step.get("limit"))
    _compile_steps(node.body, step.get("do", []))

# Sugar for the "wait for a condition, polling" shape the prompt asks
# for: a WHILE NOT(condition), 'timeout' as Robot's own loop 'limit' (so
# a stuck poll fails with Robot's own clear "did not finish within the
# limit" rather than spinning silently), 'interval' as a Sleep between
# tries -- BuiltIn's own keyword, not a bespoke poll primitive.
def _add_retry_until(body, step):
    condition = step["retry_until"]
    node = body.create_while(condition="not (%s)" % condition,
                             limit=step.get("timeout"))
    _compile_steps(node.body, step.get("do", []))
    if "interval" in step:
        node.body.create_keyword(name="Sleep", args=[_text(step["interval"])])

def _add_try(body, step):
    node = body.create_try()
    branch = node.body.create_branch(type=node.TRY)
    _compile_steps(branch.body, step["try"])
    for clause in step.get("except", []):
        pattern = clause.get("pattern")
        kwargs = {"type": node.EXCEPT}
        if pattern:
            kwargs["patterns"] = [pattern]
            kwargs["pattern_type"] = clause.get("type", "GLOB").upper()
        branch = node.body.create_branch(**kwargs)
        _compile_steps(branch.body, clause.get("do", []))
    if "else" in step:
        branch = node.body.create_branch(type=node.ELSE)
        _compile_steps(branch.body, step["else"])
    if "finally" in step:
        branch = node.body.create_branch(type=node.FINALLY)
        _compile_steps(branch.body, step["finally"])

def _add_set(body, step):
    spec = step["set"]
    body.create_var(name="${%s}" % spec["name"].lstrip("$").strip("{}"),
                    value=[_text(spec["value"])])

_HANDLERS = {
    "if": _add_if, "for_each": _add_for_each, "for_range": _add_for_range,
    "while": _add_while, "retry_until": _add_retry_until, "try": _add_try,
    "set": _add_set,
}

def _compile_steps(body, steps):
    for step in steps:
        if isinstance(step, str):
            name, *args = step.split()
            body.create_keyword(name=name, args=args)
            continue
        if not isinstance(step, dict) or len(step) == 0:
            raise LoadError("a step must be a string or a one-key mapping: %r" % (step,))
        if "break" in step:
            body.create_break()
        elif "continue" in step:
            body.create_continue()
        else:
            key = next((k for k in _HANDLERS if k in step), None)
            if key:
                _HANDLERS[key](body, step)
            else:
                _add_keyword(body, step)

# One step list becomes one UserKeyword body -- shared by 'keywords:'
# (reusable named actions), suite/test setup and teardown (a single
# keyword call, same restriction Robot's own '[Setup]'/'[Teardown]'
# have -- bundle more than one step under a keyword of its own, exactly
# the way a real .robot file would), and a test's own 'steps:'.
def _compile_keyword(suite, entry):
    uk = suite.resource.keywords.create(
        name=entry["name"], args=[("${%s}" % a) for a in entry.get("args", [])])
    _compile_steps(uk.body, entry.get("steps", []))

def _config_fixture(fixture, step):
    name, args, named, _assign = _keyword_step(step)
    fixture.config(name=name, args=args, named_args=named or None)

# A child TestSuite built from a sibling one does not see its
# resource's own keywords/variables (verified: Robot only wires that up
# for a real directory/'__init__.robot' discovery, not for two
# programmatically-built suites joined with 'suites.append()') -- so
# every 'test:' entry a spec's own fragments contributed compiles into
# ONE flat suite instead, sharing one resource. This is what makes a
# 'Log In' keyword defined where the root account is actually
# configured usable by a test another, unrelated fragment contributes:
# both entries end up in the same suite's own resource, not two
# separate ones that can't see each other.
#
# There is no suite-level setup/teardown: two boards each 'requires:'-
# ing their own board-specific fragment plus a shared one (conf-
# accounts, say) is the ordinary case, and each fragment's own
# 'test:' entry reasonably wants its own tests connected the same way
# -- a single whole-spec setup would mean only one entry's fragment
# could ever declare one, an arbitrary restriction two real,
# unrelated 'connect_target: {}' entries have no reason to trip over.
# Instead, an entry's own 'setup:'/'teardown:' is the default for every
# test *that entry* contributes -- a test's own 'setup:'/'teardown:'
# still overrides it, same as always.
def compile(entries, context, name="seine test"):
    import robot.running as running
    if len(entries) == 1:
        name = entries[0].get("name") or name
    suite = running.TestSuite(name)

    seen_libraries = set()
    seen_keywords = {}
    for entry in entries:
        for library in entry.get("library", []):
            if library not in seen_libraries:
                seen_libraries.add(library)
                suite.resource.imports.library(library, args=(context,))

        for varname, value in (entry.get("variables") or {}).items():
            suite.resource.variables.create(name="${%s}" % varname, value=[_text(value)])

        for keyword in entry.get("keywords", []):
            name = keyword["name"]
            if name in seen_keywords:
                # Identical definition: a fragment reached twice via two
                # 'requires:' paths, tolerated the same as everywhere
                # else that happens. Only a real mismatch is an error.
                if seen_keywords[name] != keyword:
                    raise LoadError(
                        "keyword '%s' is defined differently by two "
                        "'test:' entries -- give one of them a "
                        "different name if they are meant to be two "
                        "keywords" % name)
                continue
            seen_keywords[name] = keyword
            _compile_keyword(suite, keyword)

    # Every default library first (once), then every entry's own extra
    # ones -- imported after the loop above so DEFAULT_LIBRARIES' own
    # names can never collide with an entry naming one of them again.
    for library in DEFAULT_LIBRARIES:
        if library not in seen_libraries:
            suite.resource.imports.library(library, args=(context,))

    for entry in entries:
        entry_tags = entry.get("tags", [])
        for case in entry.get("tests", []):
            test = suite.tests.create(
                name=case["name"], tags=entry_tags + case.get("tags", []))
            setup = case.get("setup", entry.get("setup"))
            teardown = case.get("teardown", entry.get("teardown"))
            if setup is not None:
                _config_fixture(test.setup, setup)
            if teardown is not None:
                _config_fixture(test.teardown, teardown)
            _compile_steps(test.body, case.get("steps", []))

    return suite
