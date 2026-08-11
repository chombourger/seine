# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import hashlib
import json
import os
import sys
import time

from seine.cmd      import Cmd
from seine.progress import elapsed
from seine.utils    import ContainerEngine

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

# What was recorded, newest run first. Without a digest, every plan's runs
# are read: someone asking about the build they have just watched should
# not have to name it again.
#
# A record that cannot be read is one run fewer rather than an error. These
# are written by a build that may have been killed halfway through writing
# one, and a half-written file is no reason to refuse the report.
def runs(spec=None):
    root = ContainerEngine.cache(RECORDS)
    plans = [spec] if spec is not None else \
            sorted(os.listdir(root)) if os.path.isdir(root) else []
    found = []
    for plan in plans:
        where = os.path.join(root, plan)
        if os.path.isdir(where) == False:
            continue
        for name in sorted(os.listdir(where)):
            if name.endswith(".json") == False:
                continue
            try:
                with open(os.path.join(where, name)) as f:
                    found.append(json.load(f))
            except (OSError, ValueError):
                continue
    return sorted(found, key=lambda run: run.get("started") or 0, reverse=True)

# How long the build itself took, which is not the sum of its steps: with
# more than one running at a time it is less, and the difference between
# the two is what parallelism bought.
def spent(run):
    return max([task["end"] for task in run["tasks"]] or [0])

# The runs that make up one build, newest first.
#
# A build that failed is fixed and run again, and the second run does what
# the first did not: the packages the first one built are built no more, so
# it is a shorter run of the same plan and half of the story. Reading only
# the newest run describes that half as if it were the build.
#
# So: the newest run, and then every run before it that failed. A run that
# succeeded ends the chain -- what happened before it was a build of its
# own, finished, and nothing to do with this one.
def chain(recorded):
    taken = recorded[:1]
    for run in recorded[1:]:
        if run.get("ok"):
            break
        taken.append(run)
    return taken

# Those runs read as one build.
#
# A step keeps the last time it ran: a bootstrap that ran again on the
# resume cost what it cost the second time, and adding the two would
# invent time nobody waited.
#
# The runs are laid end to end, oldest first, so what ran beside what
# inside a run is kept while the wait between a failure and the day
# someone came back to it is in no number here.
def merged(taken):
    if len(taken) == 0:
        return None
    tasks, offset = {}, 0.0
    for run in sorted(taken, key=lambda run: run["started"]):
        for task in run["tasks"]:
            tasks[task["name"]] = dict(task,
                                       start=task["start"] + offset,
                                       end=task["end"] + offset)
        offset += spent(run)

    newest = taken[0]
    return {"spec": newest["spec"],
            "started": newest["started"],
            "jobs": newest.get("jobs", 1),
            "ok": newest.get("ok", True),
            "runs": len(taken),
            "graphs": len({run.get("graph") for run in taken}),
            "tasks": sorted(tasks.values(), key=lambda task: task["start"])}

def _header(run):
    said = "plan %s, built %s" % (
        run["spec"], time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(run["started"])))
    if run.get("jobs", 1) > 1:
        said += ", %d steps at a time" % run["jobs"]
    if run.get("runs", 1) > 1:
        said += ", %d runs joined" % run["runs"]
    if run.get("ok") == False:
        said += ", failed"
    # Which is expected of a resume and worth saying anyway: the steps of
    # these runs were not the same set, so what is reported is what each
    # step cost the last time it ran rather than one walk of one graph.
    if run.get("graphs", 1) > 1:
        said += "\nthe runs ran different steps: a resumed build no longer " \
                "builds what the failed one built"
    return said

# Where the time went, longest step first. The two totals at the end are
# the pair worth reading together: step time is the work the build did,
# and build time is how long it took to do it.
def blame(run):
    print(_header(run))
    print()
    total = 0
    for task in sorted(run["tasks"], key=lambda t: t["end"] - t["start"],
                       reverse=True):
        took = task["end"] - task["start"]
        total += took
        print("  %8s  %s%s" % (elapsed(took), task["name"],
                               "  (failed)" if task["failed"] else ""))
    print()
    print("  %s of step time in %s of build" % (elapsed(total),
                                                elapsed(spent(run))))
    return 0

REPORTS = {
    "blame": blame,
}

class AnalyzeCmd(Cmd):
    NAME = "analyze"

    def main(self, argv):
        if len(argv) == 0 or argv[0] in ["-h", "--help"]:
            print(USAGE)
            sys.exit(0 if len(argv) > 0 else 1)

        report = REPORTS.get(argv[0])
        if report is None:
            sys.stderr.write("error: '%s' is not something seine analyzes%s"
                             % (argv[0], USAGE))
            sys.exit(1)

        try:
            opts, args = getopt.getopt(argv[1:], "h", ["help"])
        except getopt.GetoptError as err:
            sys.stderr.write("error: %s%s" % (err, USAGE))
            sys.exit(1)
        for o, _ in opts:
            if o in ("-h", "--help"):
                print(USAGE)
                sys.exit()

        sys.exit(report(self.latest(args)))

    # The build to report on: the most recent one of the plan these
    # specifications describe, or simply the most recent one when none are
    # named -- and the runs it was resumed from, read as the one build they
    # are. Nothing is fetched or built to work that out: the specifications
    # are loaded and parsed, which is what 'seine plan' already does
    # without touching anything.
    def latest(self, specifications):
        spec = self._digest(specifications) if len(specifications) > 0 else None
        recorded = runs(spec)
        if len(recorded) == 0:
            sys.stderr.write(
                "error: nothing recorded%s yet; a build writes a record as "
                "it runs\n" % ("" if spec is None else " for plan %s" % spec))
            sys.exit(1)
        # Whatever was built last, and then that plan alone: runs of two
        # plans are two builds however close together they ran, and a
        # chain across them would join a kernel's build to somebody
        # else's.
        newest = recorded[0]["spec"]
        return merged(chain([run for run in recorded
                             if run["spec"] == newest]))

    def _digest(self, specifications):
        # Here rather than at the top of the file: what loads a
        # specification is the build command, which is reached through the
        # image, which is what writes the records this reads.
        from seine.build import BuildCmd

        build = BuildCmd()
        try:
            for name in specifications:
                build.load(name)
            return spec_digest(build.parse())
        except OSError as e:
            sys.stderr.write("error: couldn't open build YAML file: %s\n" % e)
            sys.exit(2)
        except ValueError as e:
            sys.stderr.write("error: YAML file is invalid: %s\n" % e)
            sys.exit(3)

USAGE = """
Say where the time went in a build that ran

Description:
  Every build records what each of its steps cost. These read that record
  back -- nothing is fetched, built or written.

  Named specifications are loaded the way 'seine build' loads them, and the
  report is of the last build of exactly those. Named none, it reports on
  the last build of anything.

Usage:
  seine analyze blame [SPEC...]

Reports:
  blame                 the steps of the build, longest first, and what the
                        build cost in total

Examples:
  seine analyze blame
  seine analyze blame demo-image.yml

Flags:
  -h, --help            print this message

"""
