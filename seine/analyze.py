# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import hashlib
import json
import os

from seine.utils import ContainerEngine

# What a build spent, step by step, kept so that it can be read once the
# build is over.
#
# A build already times its steps -- '--verbose' says what each one cost
# and the progress display counts them as they go -- and then throws the
# numbers away with the process. Which is fine until the question is why
# a build took as long as it did, because that one is asked afterwards.
#
# Advisory, like the cache index beside it: nothing decides a build from
# what is here. A record that could not be written is a report that cannot
# be given, not a build that fails.
RECORDS = "analyze"

# How many runs of one plan are kept. A record is a few kilobytes, and what
# is asked of it is about the builds of this week rather than of the year.
KEEP = 20

# What a run is filed under: the specification the build was asked for,
# once the files were merged and parsed. The specification and not the
# steps, so that a build resumed after a failure is filed together with the
# failure it resumed -- by then the packages the failed run built are no
# longer waiting to be built, and the steps are a different set.
#
# 'default' is there so that a specification carrying something json has no
# opinion about is still filed somewhere rather than raising: what is in a
# parsed specification today is strings, numbers and lists of them.
def spec_digest(spec):
    return _digest(json.dumps(spec, sort_keys=True, default=repr))

# And what the steps of one run were, which is the other half of the
# answer: two runs of one specification whose graphs differ are two runs
# that did different work, and a report that mixes them without saying so
# is a report that misleads.
def graph_digest(steps):
    return _digest("\n".join(sorted(
        "%s<-%s" % (step.name, ",".join(sorted(step.needs)))
        for step in steps)))

def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]

# One run, written once it is over -- whether it ended or failed. Steps
# that never ran are not in it: what did not run has nothing to say about
# where the time went, and it is named by the failure instead.
def record(steps, spec, jobs=1, ok=True):
    ran = [step for step in steps if step.started is not None]
    if len(ran) == 0:
        return None
    started = min(step.started for step in ran)
    run = {
        "spec": spec,
        "graph": graph_digest(steps),
        "started": started,
        "jobs": jobs,
        "ok": ok,
        # Relative to the start of the run rather than to the epoch. What
        # is read out of these is how long a step took and what it ran
        # beside, and both of those live inside one run.
        "tasks": [{"name": step.name,
                   "needs": step.needs,
                   "start": round(step.started - started, 3),
                   "end": round((step.ended or step.started) - started, 3),
                   "failed": step.failed} for step in ran],
    }

    where = ContainerEngine.cache(RECORDS, spec)
    try:
        os.makedirs(where, exist_ok=True)
        path = os.path.join(where, "%d.json" % started)
        with open(path, "w") as f:
            json.dump(run, f)
        _prune(where)
        return path
    except OSError:
        return None

# The oldest runs of one plan, once there are more than KEEP of them. Named
# for the second they started, so sorting the names sorts the runs.
def _prune(where, keep=KEEP):
    names = sorted(name for name in os.listdir(where)
                   if name.endswith(".json"))
    for name in names[:max(0, len(names) - keep)]:
        os.unlink(os.path.join(where, name))
