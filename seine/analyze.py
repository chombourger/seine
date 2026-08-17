# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import getopt
import hashlib
import json
import os
import sys
import threading
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

# How often the machine is looked at while a build runs. Short beside the
# steps it samples, and fine enough to see a kernel finish.
SAMPLE = 10

STAT = "/proc/stat"

# What the machine is doing, as the kernel counts it since boot: the ticks
# it spent working, and the ticks it had. Two of these are what a busy
# fraction is made of -- one on its own says only what the machine has
# been up to since it was switched on.
#
# Idle and iowait are both counted as not working, on purpose: a build
# waiting for a disk or a mirror is a build not using the cpu, and that
# is exactly what this is here to be able to say.
def _busy(stat=STAT):
    try:
        with open(stat) as f:
            ticks = [int(field) for field in f.readline().split()[1:]]
    except (OSError, IndexError, ValueError):
        return None
    return sum(ticks) - sum(ticks[3:5]), sum(ticks)

def _cpu(before, after):
    had = after[1] - before[1]
    return round((after[0] - before[0]) / had, 3) if had > 0 else 0.0

# What the machine was doing while the build ran.
#
# 'blame' says where the time went and 'critical-chain' says what the
# build was waiting for. Neither says whether the machine had anything
# left to give, which is the next question every time: a build using a
# third of the cores wants more '--jobs', and one whose load is far above
# what it is burning is waiting for a disk or a mirror, which more of
# them would not change.
#
# The whole machine and not this build: podman, another user and a second
# seine all land in these numbers. Enough to answer 'was it busy', not
# 'who was busy'.
#
# A machine that will not answer -- no /proc/stat, no load average -- is
# not asked again, and the build is recorded without any of this.
class watching:
    def __init__(self, every=SAMPLE, stat=STAT, callback=None):
        self.every = every
        self.stat = stat
        # Pushed a sample live, same shape as recorded, for a caller
        # wanting load while a build runs. None by default.
        self.callback = callback
        self.cpus = os.cpu_count()
        self.samples = []
        self.stop = threading.Event()
        self.watcher = None

    def __enter__(self):
        self.last = _busy(self.stat)
        if self.last is None or _load() is None:
            return self
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        if self.watcher is not None:
            self.watcher.join(timeout=1)
        return False

    def _watch(self):
        while self.stop.wait(self.every) == False:
            busy = _busy(self.stat)
            if busy is None:
                continue
            sample = {"t": time.time(), "load": _load(),
                      "cpu": _cpu(self.last, busy)}
            self.samples.append(sample)
            self.last = busy
            if self.callback is not None:
                self.callback(sample)

def _load():
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        return None

# One run, written once it is over -- whether it ended or failed. Steps
# that never ran are not in it: what did not run has nothing to say about
# where the time went, and it is named by the failure instead.
def record(steps, spec, jobs=1, ok=True, machine=None, rootfs_size=None):
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
    # The rootfs tarball's own size, not the disk image's -- a disk is
    # commonly padded well past its content. None for a build that never
    # reached the tarball step (--packages-only stops before it).
    if rootfs_size is not None:
        run["rootfs_size"] = rootfs_size
    if machine is not None and len(machine.samples) > 0:
        run["cpus"] = machine.cpus
        # On the same clock as the steps, so that a load worth explaining
        # is read against what was running at the time.
        run["samples"] = [dict(sample, t=round(sample["t"] - started, 1))
                          for sample in machine.samples]

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
    tasks, samples, boundaries, offset = {}, [], [], 0.0
    for run in sorted(taken, key=lambda run: run["started"]):
        for task in run["tasks"]:
            tasks[task["name"]] = dict(task,
                                       start=task["start"] + offset,
                                       end=task["end"] + offset)
        # Every run's, unlike the steps: what the machine was doing during
        # a run that failed is still what it was doing.
        samples += [dict(sample, t=sample["t"] + offset)
                    for sample in run.get("samples") or []]
        offset += spent(run)
        boundaries.append(offset)

    newest = taken[0]
    return {"spec": newest["spec"],
            "started": newest["started"],
            "jobs": newest.get("jobs", 1),
            "ok": newest.get("ok", True),
            "runs": len(taken),
            "graphs": len({run.get("graph") for run in taken}),
            "cpus": newest.get("cpus"),
            # Where one run ended and the next began, for a chart to say
            # so. The last one is where the build ends, which is a line
            # nobody needs.
            "boundaries": boundaries[:-1],
            "samples": samples,
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
    machine = _machine(run)
    if machine is not None:
        print("  %s" % machine)
    return 0

# How hard the machine was working, beside how long the build took. The
# pair is the answer to 'more --jobs?': a build burning a third of the
# cores has room, and one whose load sits well above what it is burning
# is waiting for a disk or a mirror, which more of them would not change.
def _machine(run):
    samples = run.get("samples") or []
    if len(samples) == 0:
        return None
    busy = sum(sample["cpu"] for sample in samples) / len(samples)
    load = sum(sample["load"] for sample in samples) / len(samples)
    return "the machine was %d%% busy, load %.1f of %d cpus" % (
        busy * 100, load, run.get("cpus") or 0)

# The longest way through the build, as (what it cost, the steps of it).
#
# Node weights rather than edge weights: what a step waits for is the step
# before it, and what it costs is its own. The path with the largest sum is
# what the build could not have gone faster than however many steps ran at
# once -- so it is the list of steps worth making quicker, and every step
# not on it is one whose time somebody else was spending anyway.
def critical(build):
    by_name = {task["name"]: task for task in build["tasks"]}
    longest = {}

    def upto(name):
        if name not in longest:
            task = by_name[name]
            before = max((upto(need) for need in task["needs"]
                          if need in by_name),
                         key=lambda path: path[0], default=(0.0, []))
            longest[name] = (before[0] + task["end"] - task["start"],
                             before[1] + [name])
        return longest[name]

    return max((upto(name) for name in by_name),
               key=lambda path: path[0], default=(0.0, []))

def critical_chain(build):
    print(_header(build))
    print()
    if build.get("jobs", 1) <= 1:
        # Every step waited for the one before it, so the chain is the
        # build and drawing it says nothing. What makes the question worth
        # asking is steps running beside each other.
        print("  one step at a time, so the whole build is the chain:")
        print("  '--jobs' is what makes this worth asking")
        return 0

    took, path = critical(build)
    print("  %s of the %s the build took is on this path" % (
        elapsed(took), elapsed(spent(build))))
    print()
    tasks = {task["name"]: task for task in build["tasks"]}
    # Printed end first, the way a build is read when it is late: what was
    # waited for last, and then what that waited for.
    for depth, name in enumerate(reversed(path)):
        task = tasks[name]
        print("%s%s%s +%s" % ("  " * depth, "" if depth == 0 else "└─",
                              name, elapsed(task["end"] - task["start"])))
    return 0

# The build as a chart: a bar per step on one clock, and what the machine
# was doing underneath them.
#
# Written by hand because that is all it takes -- a rectangle per step and
# a line per series -- and because a chart nobody has to install anything
# to read is a chart that gets read. It goes to stdout, for a browser or
# for an issue.
WIDTH  = 1000    # of the whole chart
GUTTER = 220     # left of the bars, where the names are
ROW    = 18      # per step
TOP    = 44      # above the bars, for the title
BAND   = 96      # under them, where the machine is drawn

def plot(build):
    if sys.stdout.isatty():
        sys.stderr.write("error: the chart is SVG, and this is a terminal; "
                         "redirect it: seine analyze plot > build.svg\n")
        return 1

    tasks = sorted(build["tasks"], key=lambda task: task["start"])
    samples = build.get("samples") or []
    took = spent(build) or 1
    bars = WIDTH - GUTTER - 20
    band = TOP + ROW * len(tasks) + 24
    height = band + (BAND if len(samples) > 0 else 0)

    def x(second):
        return GUTTER + second / took * bars

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'font-family="sans-serif" font-size="11">' % (WIDTH, height),
           '<rect width="100%" height="100%" fill="white"/>',
           # The first line of it: what a chart adds to the header is the
           # picture, and the notes under it are for the reports that
           # have no picture.
           '<text x="10" y="20" font-size="13">%s</text>'
           % _text(_header(build).split("\n")[0])]

    # The clock the whole chart is read against.
    for tick in _ticks(took):
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" '
                   'stroke="#dddddd"/>' % (x(tick), TOP - 12, x(tick), height))
        out.append('<text x="%.1f" y="%d" fill="#666666">%s</text>'
                   % (x(tick) + 2, TOP - 16, elapsed(tick)))

    # Where one run of a resumed build ends and the next begins. The gap
    # between them is not drawn: nobody was building during it.
    for boundary in build.get("boundaries") or []:
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#999999" '
                   'stroke-dasharray="4 3"/>' % (x(boundary), TOP - 12,
                                                 x(boundary), height))

    for row, task in enumerate(tasks):
        y = TOP + row * ROW
        out.append('<text x="%d" y="%d" text-anchor="end" fill="#333333">%s</text>'
                   % (GUTTER - 8, y + 10, _text(task["name"][:34])))
        out.append('<rect class="step" x="%.1f" y="%d" width="%.1f" height="%d" '
                   'fill="%s"><title>%s: %s</title></rect>'
                   % (x(task["start"]), y + 2,
                      max(1.0, x(task["end"]) - x(task["start"])), ROW - 5,
                      "#e45756" if task["failed"] else "#4c78a8",
                      _text(task["name"]),
                      elapsed(task["end"] - task["start"])))

    out += _plot_machine(build, samples, band, x)
    out.append('</svg>')
    print("\n".join(out))
    return 0

# The load average and the busy cpus, both in cores so that one axis says
# both: a load well above the cores being burnt is a build waiting for
# something that is not a processor.
def _plot_machine(build, samples, band, x):
    if len(samples) == 0:
        return []
    cpus = build.get("cpus") or 1
    top = max([cpus] + [sample["load"] for sample in samples])

    def y(cores):
        return band + BAND - 20 - (cores / top) * (BAND - 34)

    drawn = ['<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#cccccc" '
             'stroke-dasharray="2 4"/>' % (GUTTER, y(cpus), WIDTH - 20, y(cpus)),
             '<text x="%d" y="%.1f" fill="#999999">%d cpus</text>'
             % (WIDTH - 60, y(cpus) - 4, cpus)]
    for name, colour, cores in [
            ("cores busy", "#54a24b",
             [(s["t"], s["cpu"] * cpus) for s in samples]),
            ("load", "#e49444", [(s["t"], s["load"]) for s in samples])]:
        drawn.append('<polyline fill="none" stroke="%s" points="%s"/>'
                     % (colour, " ".join("%.1f,%.1f" % (x(at), y(value))
                                         for at, value in cores)))
        drawn.append('<text x="%d" y="%d" fill="%s">%s</text>'
                     % (10 if name == "cores busy" else 90, band + BAND - 4,
                        colour, name))
    return drawn

# Something to read the clock against, without so many that the chart is
# ticks. Whichever of these gives ten or fewer.
def _ticks(took):
    for every in [30, 60, 300, 600, 1800, 3600, 7200]:
        if took / every <= 10:
            return [tick * every for tick in range(int(took // every) + 1)]
    return []

def _text(said):
    return said.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

REPORTS = {
    "blame": blame,
    "critical-chain": critical_chain,
    "plot": plot,
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
  seine analyze REPORT [SPEC...]

Reports:
  blame                 the steps of the build, longest first, and what the
                        build cost in total
  critical-chain        the longest way through the build: the steps it
                        could not have gone faster than, whatever else ran
                        beside them
  plot                  the build as a chart, as SVG on stdout: a bar per
                        step on one clock, and what the machine was doing
                        underneath them

Examples:
  seine analyze blame
  seine analyze critical-chain demo-image.yml
  seine analyze plot demo-image.yml > build.svg

Flags:
  -h, --help            print this message

"""
