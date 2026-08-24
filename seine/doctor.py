# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Every check says whether something a real module assumes is actually
# there -- no privileged action, nothing built. seine.tui.doctor renders
# the same list.

import importlib.util
import os
import shutil
import subprocess

from seine import settings
from seine.imager import DEFAULT_HYPERVISORS
from seine.sbom import DEBSBOM_IMAGE
from seine.utils import ContainerEngine, HOST_ARCH

class Check:
    def __init__(self, group, name, status, detail):
        self.group = group
        self.name = name
        # "ok"/"warn"/"missing", not True/False -- a missing cross-arch
        # qemu is a note, not a failure like a missing podman.
        self.status = status
        self.detail = detail

def _run(argv, timeout=5):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result

def _first_line(text):
    text = (text or "").strip()
    return text.splitlines()[0] if text else ""

# 'binary --version', reduced to one line -- missing or unresponsive
# both count as "missing"; diagnosing why is not this function's job.
def _binary(group, name, argv=None):
    path = shutil.which(name)
    if path is None:
        return Check(group, name, "missing", "not found")
    result = _run(argv or [name, "--version"])
    version = _first_line(result.stdout) if result is not None else path
    return Check(group, name, "ok", version or path)

GROUP_ENGINE = "Container engine (seine.utils.ContainerEngine)"
GROUP_IMAGING = "Imaging (seine.imager)"
GROUP_ANSIBLE = "Ansible (seine.ansible_runner)"
GROUP_SIGNING = "Signing (seine.signing)"
GROUP_SBOM = "SBOM (seine.sbom)"
GROUP_STORAGE = "Storage (seine.utils.ContainerEngine.build_dir)"
GROUP_AI = "Optional AI integration (seine.tui.ai)"
GROUP_TARGET = "Optional remote target (seine.tui.target)"
GROUP_TESTING = "Optional test automation (seine.testing)"

def check_podman():
    return _binary(GROUP_ENGINE, "podman")

def check_crun():
    return _binary(GROUP_ENGINE, "crun")

def check_passt():
    # passt has no '--version' worth trusting across releases; found is
    # the whole question.
    path = shutil.which("passt")
    status = "ok" if path else "missing"
    return Check(GROUP_ENGINE, "passt", status, path or "not found")

def check_guestfs():
    found = importlib.util.find_spec("guestfs") is not None
    return Check(GROUP_IMAGING, "python3-guestfs",
                "ok" if found else "missing",
                "importable" if found else "not importable")

# A note, not an error: '/target' and its AI tools are an optional
# feature, unlike guestfs which every real build needs.
def check_mtda():
    found = importlib.util.find_spec("mtda.client") is not None
    return Check(GROUP_TARGET, "python3-mtda",
                "ok" if found else "warn",
                "importable" if found else "not importable, '/target' disabled")

# 'seine test' is optional the same way '/target' is: a note, not an
# error, since most builds never drive real hardware.
def check_robotframework():
    found = importlib.util.find_spec("robot") is not None
    return Check(GROUP_TESTING, "robotframework",
                "ok" if found else "warn",
                "importable" if found else "not importable, 'seine test' disabled")

def check_kvm():
    path = "/dev/kvm"
    if not os.path.exists(path):
        return Check(GROUP_IMAGING, "/dev/kvm", "missing", "device does not exist")
    ok = os.access(path, os.R_OK | os.W_OK)
    return Check(GROUP_IMAGING, "/dev/kvm", "ok" if ok else "missing",
                "accessible" if ok else "exists but not accessible (group 'kvm'?)")

# Host architecture's hypervisor is the one that matters; the others are
# only for cross-building, so their absence is a note, not a failure.
def check_hypervisors():
    checks = []
    for architecture, path in sorted(DEFAULT_HYPERVISORS.items()):
        name = os.path.basename(path)
        found = shutil.which(name) or (path if os.path.exists(path) else None)
        if found:
            checks.append(Check(GROUP_IMAGING, name, "ok", "present"))
        elif architecture == HOST_ARCH:
            checks.append(Check(GROUP_IMAGING, name, "missing",
                                "needed to build for this host's architecture"))
        else:
            checks.append(Check(GROUP_IMAGING, name, "warn",
                                "not found (only needed to cross-build %s)" % architecture))
    return checks

def check_ansible_playbook():
    return _binary(GROUP_ANSIBLE, "ansible-playbook")

# 'ansible-galaxy collection list NAME' exits 0 whether or not NAME is
# installed -- it prints nothing when it is not -- so presence is read
# off the output, not the exit code.
def check_podman_collection():
    result = _run(["ansible-galaxy", "collection", "list", "containers.podman"])
    if result is None:
        return Check(GROUP_ANSIBLE, "containers.podman", "missing",
                    "ansible-galaxy not found")
    found = any(line.split()[:1] == ["containers.podman"]
               for line in (result.stdout or "").splitlines())
    return Check(GROUP_ANSIBLE, "containers.podman", "ok" if found else "missing",
                "installed" if found else "collection not installed")

def check_gnupg():
    path = shutil.which("gpg")
    return Check(GROUP_SIGNING, "gnupg", "ok" if path else "missing",
                path or "not found (only needed for --sign-key)")

# The one check that takes what a caller is about to do into account:
# a sign key set on the environment or the command line is fine either
# way, so this is a note, never a failure of its own.
def check_sign_key(options=None):
    options = options or {}
    key = options.get("sign_key") or os.environ.get("SEINE_SIGN_KEY")
    if key:
        return Check(GROUP_SIGNING, "sign key", "ok", "set")
    return Check(GROUP_SIGNING, "sign key", "warn",
                "SEINE_SIGN_KEY not set and no --sign-key on this command")

def check_debsbom_image():
    # Not pulled -- only asked whether the registry can be reached at
    # all, and only when a caller opts in: this has to stay fast and
    # offline-safe by default.
    result = _run(["podman", "manifest", "inspect", DEBSBOM_IMAGE], timeout=10)
    ok = result is not None and result.returncode == 0
    return Check(GROUP_SBOM, "debsbom image reachable",
                "ok" if ok else "warn",
                DEBSBOM_IMAGE if ok else "could not reach %s" % DEBSBOM_IMAGE)

def check_storage():
    build_dir = ContainerEngine.build_dir()
    os.makedirs(build_dir, exist_ok=True)
    free = shutil.disk_usage(build_dir).free
    return Check(GROUP_STORAGE, build_dir, "ok", "%.1f GiB free" % (free / 1024**3))

# Same shape as check_sign_key(): configured but missing the one
# credential is a note, not a failure. No model set at all isn't
# reported here -- that's the feature being off, not a machine missing
# something.
def check_llm():
    model = os.environ.get("SEINE_LLM_MODEL") or settings.load().get("llm_model")
    if not model:
        return None
    if os.environ.get("SEINE_LLM_API_KEY"):
        return Check(GROUP_AI, "llm_model", "ok", model)
    return Check(GROUP_AI, "llm_model", "warn",
                "%s configured but SEINE_LLM_API_KEY is not set" % model)

# Cheap and offline by default; 'pull' opts into the one check that
# touches the network for real.
def run(options=None, pull=False):
    checks = [check_podman(), check_crun(), check_passt(),
             check_guestfs(), check_kvm()] + check_hypervisors() + [
             check_ansible_playbook(), check_podman_collection(),
             check_gnupg(), check_sign_key(options),
             check_storage(), check_mtda(), check_robotframework()]
    llm = check_llm()
    if llm is not None:
        checks.append(llm)
    if pull:
        checks.append(check_debsbom_image())
    return checks

MARKS = {"ok": "✔", "warn": "-", "missing": "✗"}

# The one rendering of a check list. Group headers appear once, in the
# order run() already grouped them in.
def render(checks):
    lines = []
    group = None
    for check in checks:
        if check.group != group:
            group = check.group
            if lines:
                lines.append("")
            lines.append(group)
        lines.append("  %s %-24s %s" % (MARKS[check.status], check.name, check.detail))
    errors = sum(1 for c in checks if c.status == "missing")
    notes = sum(1 for c in checks if c.status == "warn")
    lines.append("")
    lines.append("%d error%s · %d note%s"
                 % (errors, "" if errors == 1 else "s", notes, "" if notes == 1 else "s"))
    return "\n".join(lines)

def errors(checks):
    return sum(1 for c in checks if c.status == "missing")
