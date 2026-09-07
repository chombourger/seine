# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

# Scans an SBOM for known CVEs, via debsbom's 'sec-scan' subcommand or an
# external program named by settings.sbom2cve_program.

import getopt
import json
import os
import re
import subprocess
import sys
from collections import Counter
from typing import NamedTuple

from seine.cmd import Cmd
from seine import settings
from seine.sbom import DEBSBOM_IMAGE, output_path
from seine.container import ContainerEngine

# One finding: a CVE against a single package. 'urgency' is Debian's
# triage label (high/medium/low/unimportant/end-of-life/not-yet-assigned);
# debsbom's output carries no CVSS or other numeric severity.
class Finding(NamedTuple):
    cve: str
    package: str
    version: str
    urgency: str
    status: str
    tracker: str

# debsbom's '-f json' output is JSON-lines, not one JSON document; each
# line is parsed on its own so one bad line doesn't break the whole scan.
def parse_lines(text):
    findings = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        vulnerability = entry.get("vulnerability")
        if not isinstance(vulnerability, dict) or not vulnerability.get("id"):
            continue
        package, _, version = entry.get("package", "").partition("@")
        if not package:
            continue
        findings.append(Finding(
            cve=vulnerability["id"],
            package=package,
            version=version,
            urgency=vulnerability.get("urgency", ""),
            status=vulnerability.get("status", ""),
            tracker=vulnerability.get("tracker", "")))
    return findings

def cache_path(sbom_path):
    return sbom_path + ".issues.json"

# A cache older than its SBOM is stale (SBOM was rebuilt since). Any
# other problem reading it is treated the same as "no cache yet". Kept
# public so callers that must never trigger a real scan (e.g. the TUI's
# read-only 'issues' tool) can read the cache without calling scan().
def read_cache(sbom_path):
    path = cache_path(sbom_path)
    try:
        if os.path.getmtime(path) < os.path.getmtime(sbom_path):
            return None
    except OSError:
        return None
    try:
        with open(path) as f:
            raw = json.load(f)
        return [Finding(**entry) for entry in raw]
    except (OSError, ValueError, TypeError):
        return None

# Write beside itself and rename into place, so a reader never sees a
# half-written file.
def _write_cache(sbom_path, findings):
    path = cache_path(sbom_path)
    temporary = "%s.new" % path
    with open(temporary, "w") as f:
        json.dump([finding._asdict() for finding in findings], f)
    os.replace(temporary, path)

# Command line for the built-in scanner: debsbom's container image, SBOM
# mounted read-only, plus a persistent cache dir for its security-tracker
# database so '--update-db' doesn't redownload it on every scan.
def _debsbom_cmd(sbom_path, distro):
    run_cmd = ["run", "--rm",
              "-v", "%s:/sbom.json:ro,z" % sbom_path,
              "-v", "%s:/root/.cache/debsbom:z" % ContainerEngine.cache("debsbom"),
              DEBSBOM_IMAGE]
    # '-t spdx' is required: the mounted name '/sbom.json' has no
    # extension for debsbom to sniff the format from.
    scan_cmd = ["debsbom", "sec-scan", "--update-db", "-t", "spdx", "-f", "json"]
    if distro:
        scan_cmd += ["--distro", distro]
    scan_cmd.append("/sbom.json")
    return run_cmd + scan_cmd

# Runs the scan and returns its findings, using a fresh cache instead
# unless 'rescan' is set. 'distro' is the Debian release to check against
# (debsbom defaults to 'trixie' when None). If settings.sbom2cve_program
# is set, it replaces the container: run as 'PROGRAM SBOM_PATH', expected
# to write the same JSON-lines shape as 'debsbom sec-scan -f json'.
def scan(sbom_path, distro=None, rescan=False):
    # Resolve to an absolute path: a relative one reaches podman as a
    # named-volume request instead of a bind mount.
    sbom_path = os.path.realpath(sbom_path)
    if not rescan:
        cached = read_cache(sbom_path)
        if cached is not None:
            return cached

    program = settings.load()["sbom2cve_program"]
    if program:
        output = subprocess.check_output([program, sbom_path])
    else:
        # ContainerEngine.cache() doesn't create the directory itself,
        # and podman refuses to bind-mount one that doesn't exist yet.
        os.makedirs(ContainerEngine.cache("debsbom"), exist_ok=True)
        output = ContainerEngine.check_output(_debsbom_cmd(sbom_path, distro))

    findings = parse_lines(output.decode("utf-8", "replace"))
    _write_cache(sbom_path, findings)
    return findings

# Aggregate counts for a caller to render however it likes. 'unique_cves'
# differs from 'total' since one CVE can hit more than one package.
def stats(findings):
    return {
        "total": len(findings),
        "unique_cves": len({finding.cve for finding in findings}),
        "packages": len({finding.package for finding in findings}),
        "by_urgency": Counter(finding.urgency for finding in findings),
        "by_status": Counter(finding.status for finding in findings),
        "by_package": Counter(finding.package for finding in findings),
    }

# debsbom's own '--min-urgency' choices, most to least severe.
URGENCY_ORDER = ["high", "medium", "low", "unimportant", "end-of-life", "not-yet-assigned"]

# 'package' narrows by name (regex, case-insensitive); 'min_urgency' drops
# anything less severe, per URGENCY_ORDER. A finding with an urgency not
# in that list is dropped rather than guessed into a bucket.
def filter_findings(findings, package=None, min_urgency=None):
    if package:
        try:
            regex = re.compile(package, re.IGNORECASE)
        except re.error as e:
            raise ValueError("'%s' is not a usable pattern: %s" % (package, e))
        findings = [f for f in findings if regex.search(f.package)]
    if min_urgency:
        if min_urgency not in URGENCY_ORDER:
            raise ValueError("'min_urgency' must be one of %s, not '%s'" %
                             (", ".join(URGENCY_ORDER), min_urgency))
        cutoff = URGENCY_ORDER.index(min_urgency)
        findings = [f for f in findings if f.urgency in URGENCY_ORDER
                   and URGENCY_ORDER.index(f.urgency) <= cutoff]
    return findings

# 'seine issues SPEC...' scans the SBOM a prior 'seine build --sbom' left
# behind; 'seine issues --sbom=FILE' scans FILE directly.
class IssuesCmd(Cmd):
    NAME = "issues"
    SHORT_OPTIONS = "h"
    LONG_OPTIONS = ["help", "sbom=", "filter=", "min-urgency=", "rescan"]
    USAGE = """
Usage:
  seine issues SPEC... [--filter=PKG] [--min-urgency=LEVEL] [--rescan]
  seine issues --sbom=FILE.spdx.json [--filter=PKG] [--min-urgency=LEVEL] [--rescan]

First form: scans the SBOM a previous 'seine build --sbom' of SPEC left
behind. Second form: scans FILE directly, no specification needed.
Both check the result against debsbom's own security tracker, or a
'sbom2cve_program' from settings.json if one is configured there.

  --filter=PKG          only findings against a package matching PKG
                         (a regex, case-insensitive)
  --min-urgency=LEVEL    only findings at or above LEVEL -- one of
                         high, medium, low, unimportant, end-of-life,
                         not-yet-assigned (the default: everything)
  --rescan               ignore a cached scan and run a fresh one
"""

    def main(self, argv):
        try:
            opts, args = getopt.getopt(argv, self.SHORT_OPTIONS, self.LONG_OPTIONS)
        except getopt.GetoptError as err:
            sys.stderr.write("%s\n%s" % (err, self.USAGE))
            sys.exit(1)
        sbom_file = package = min_urgency = None
        rescan = False
        for o, a in opts:
            if o in ("-h", "--help"):
                print(self.USAGE)
                return
            elif o == "--sbom":
                sbom_file = a
            elif o == "--filter":
                package = a
            elif o == "--min-urgency":
                min_urgency = a
            elif o == "--rescan":
                rescan = True

        if sbom_file:
            if args:
                sys.stderr.write("error: --sbom does not take a specification file\n")
                sys.exit(1)
            sbom_path, distro = sbom_file, None
        else:
            if not args:
                sys.stderr.write(
                    "error: issues command expects one or more specification "
                    "files, or --sbom=FILE\n")
                sys.exit(1)
            from seine.build import BuildCmd
            build = BuildCmd()
            build.options = dict(build.options, ansible_library=[])
            try:
                build.load_all(args)
                build.parse()
            except OSError as e:
                sys.stderr.write("error: couldn't open specification file: %s\n" % e)
                sys.exit(2)
            except ValueError as e:
                sys.stderr.write("error: specification is invalid: %s\n" % e)
                sys.exit(3)
            distro = build.spec["distribution"]["release"]
            sbom_path = output_path(build.image._output)
            if not os.path.isfile(sbom_path):
                sys.stderr.write(
                    "error: no SBOM for this build yet -- run 'seine build "
                    "--sbom' first\n")
                sys.exit(4)

        try:
            findings = scan(sbom_path, distro=distro, rescan=rescan)
            findings = filter_findings(findings, package=package, min_urgency=min_urgency)
        except ValueError as e:
            sys.stderr.write("error: %s\n" % e)
            sys.exit(1)
        except (OSError, subprocess.CalledProcessError) as e:
            sys.stderr.write("error: scan failed: %s\n" % e)
            sys.exit(5)

        if not findings:
            print("no known CVEs found")
            return
        width = max(len(f.package) for f in findings)
        for f in findings:
            print("%-16s %-*s %-18s %s" % (f.cve, width, f.package, f.urgency, f.status))
