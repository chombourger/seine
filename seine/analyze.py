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
from seine.container import ContainerEngine

# A build's step timings, kept to read back after the build ends.
# Advisory only: if a record fails to write, the build doesn't fail.
RECORDS = "analyze"

# Runs kept per plan. Records are small; only recent runs matter.
KEEP = 20

# Keyed by the merged spec, not the steps, so a build resumed after
# a failure files under the same key as the failed run.
# default=repr covers values json can't serialize directly.
#
# '_'-prefixed keys are left out: parsers annotate the spec with them
# ('_size', '_prefix', ...) and the build itself writes computed sizes
# back into it (see Image.build()/multiconfig.run()), so a digest taken
# after a run would otherwise never match a fresh reload of the same
# files. What the next plan/overview compares against must be stable
# across that mutation -- same reason BuildCmd.dump() hides them.
def _cleaned(spec):
    if isinstance(spec, dict):
        return {key: _cleaned(value) for key, value in spec.items()
                if not key.startswith("_")}
    if isinstance(spec, list):
        return [_cleaned(value) for value in spec]
    return spec

def spec_digest(spec):
    return _digest(json.dumps(_cleaned(spec), sort_keys=True, default=repr))

# Keyed separately from spec_digest: a resumed build can run a
# different set of steps than the run it resumed.
def graph_digest(steps):
    return _digest("\n".join(sorted(
        "%s<-%s" % (step.name, ",".join(sorted(step.needs)))
        for step in steps)))

def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:12]

# Seconds between machine samples; frequent enough to catch a short step.
SAMPLE = 10

STAT = "/proc/stat"

# CPU ticks since boot: busy vs. total. Idle and iowait count as not
# busy, so a build waiting on disk/network shows as idle, not busy.
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

# Samples the whole machine (not just this build) while it runs, so
# blame/critical-chain can show cpu and load next to step times.
# Gives up quietly if /proc/stat or load average isn't available.
class watching:
    def __init__(self, every=SAMPLE, stat=STAT, callback=None):
        self.every = every
        self.stat = stat
        # Live callback for a caller that wants samples as they're taken.
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

# One run, written once it ends. Steps that never started are skipped.
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
        # Relative to the run's start, not the epoch.
        "tasks": [{"name": step.name,
                   "needs": step.needs,
                   "start": round(step.started - started, 3),
                   "end": round((step.ended or step.started) - started, 3),
                   "failed": step.failed} for step in ran],
    }
    # The rootfs tarball's size, not the padded disk image's. None if
    # the build stopped before that step (e.g. --packages-only).
    if rootfs_size is not None:
        run["rootfs_size"] = rootfs_size
    if machine is not None and len(machine.samples) > 0:
        run["cpus"] = machine.cpus
        # Same clock as the steps, so load lines up with what was running.
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

# Oldest runs beyond KEEP, named so sorting names sorts by time.
def _prune(where, keep=KEEP):
    names = sorted(name for name in os.listdir(where)
                   if name.endswith(".json"))
    for name in names[:max(0, len(names) - keep)]:
        os.unlink(os.path.join(where, name))

# Newest run first. No spec given, reads every plan's runs.
# A record that fails to read (e.g. half-written) is just skipped.
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

# Build time, not sum of step times: parallel steps overlap.
def spent(run):
    return max([task["end"] for task in run["tasks"]] or [0])

# The runs behind one build: the newest run, plus every failed run
# before it. A successful run ends the chain -- what came before it
# was a separate, finished build.
def chain(recorded):
    taken = recorded[:1]
    for run in recorded[1:]:
        if run.get("ok"):
            break
        taken.append(run)
    return taken

# Lays runs end to end, oldest first, to read a resumed build as one.
# A step keeps its last run's timing only, so a retried step's time
# isn't double-counted.
def merged(taken):
    if len(taken) == 0:
        return None
    tasks, samples, boundaries, offset = {}, [], [], 0.0
    for run in sorted(taken, key=lambda run: run["started"]):
        for task in run["tasks"]:
            tasks[task["name"]] = dict(task,
                                       start=task["start"] + offset,
                                       end=task["end"] + offset)
        # Kept for every run, even failed ones: the machine was still busy.
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
            # Where one run ends and the next begins; drop the final one.
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
    # Expected on a resume: the runs didn't build the same steps.
    if run.get("graphs", 1) > 1:
        said += "\nthe runs ran different steps: a resumed build no longer " \
                "builds what the failed one built"
    return said

# Longest step first; totals compare step time vs. build time.
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

# Cpu use next to load: low cpu use with room to add --jobs, or
# load far above cpu use means it's waiting on disk/network.
def _machine(run):
    samples = run.get("samples") or []
    if len(samples) == 0:
        return None
    busy = sum(sample["cpu"] for sample in samples) / len(samples)
    load = sum(sample["load"] for sample in samples) / len(samples)
    return "the machine was %d%% busy, load %.1f of %d cpus" % (
        busy * 100, load, run.get("cpus") or 0)

# Longest path through the build by node weight (step cost), not
# edge weight: a step's cost is its own, and it waits on the step(s)
# before it. This path is the floor on how fast the build could go.
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

# The build as an SVG chart: one bar per step, plus a line for what
# the machine was doing underneath. Written by hand, no library.
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
           # First line of the header only; the rest is text-only detail.
           '<text x="10" y="20" font-size="13">%s</text>'
           % _text(_header(build).split("\n")[0])]

    # The clock the whole chart is read against.
    for tick in _ticks(took):
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" '
                   'stroke="#dddddd"/>' % (x(tick), TOP - 12, x(tick), height))
        out.append('<text x="%.1f" y="%d" fill="#666666">%s</text>'
                   % (x(tick) + 2, TOP - 16, elapsed(tick)))

    # Where a resumed build's runs meet; the gap between them isn't drawn.
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

# Load and busy cpus on one axis, in cores: load far above cpus
# busy means the build is waiting on something other than cpu.
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

# Pick a tick spacing that gives 10 or fewer ticks.
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

    # The most recent build of the named spec (or of anything, if none
    # named), plus the runs it resumed from, read as one build. Nothing
    # is fetched or built -- specs are only loaded and parsed.
    def latest(self, specifications):
        spec = self._digest(specifications) if len(specifications) > 0 else None
        recorded = runs(spec)
        if len(recorded) == 0:
            sys.stderr.write(
                "error: nothing recorded%s yet; a build writes a record as "
                "it runs\n" % ("" if spec is None else " for plan %s" % spec))
            sys.exit(1)
        # Only the newest plan's runs: two plans built close together are
        # still two separate builds, not one chain.
        newest = recorded[0]["spec"]
        return merged(chain([run for run in recorded
                             if run["spec"] == newest]))

    def _digest(self, specifications):
        # Loading a spec needs the build command, deferred to avoid an import cycle.
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
