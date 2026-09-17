# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

# Shadows ansible.builtin.apt: runs apt-get on the build host, not
# inside the (maybe foreign-arch) target. Only 'name' and 'state:
# present|absent' are supported -- all any seine playbook uses.

import os
import subprocess

from ansible.plugins.action import ActionBase
from ansible.errors import AnsibleActionFail

ENV_CID = "SEINE_ROOTFS_CID"
ENV_ROOT = "SEINE_CONTAINER_ROOT"
ENV_RUNROOT = "SEINE_CONTAINER_RUNROOT"
ENV_ARCH = "SEINE_APT_ARCH"
ENV_HOST_IMAGE = "SEINE_APT_HOST_IMAGE"

MERGED = "/rootfs"

def _env(name):
    value = os.environ.get(name)
    if not value:
        raise AnsibleActionFail(f"{name} is not set -- the 'apt' action "
                                f"plugin only works inside a seine build")
    return value

def _podman(args):
    cmd = ["podman", "--root", _env(ENV_ROOT), "--runroot", _env(ENV_RUNROOT)]
    return subprocess.run(cmd + args, capture_output=True, text=True)

def _merged_dir(cid):
    proc = _podman(["inspect", cid, "--format", "{{.GraphDriver.Data.MergedDir}}"])
    if proc.returncode != 0:
        raise AnsibleActionFail(f"could not inspect container {cid}: {proc.stderr}")
    return proc.stdout.strip()

def _names(value):
    if isinstance(value, str):
        return [n.strip() for n in value.replace(",", " ").split()]
    return list(value)

# dpkg-query needs no chroot: it only reads the target's status file, and
# that format is architecture-independent.
def _installed(merged_dir, names):
    proc = _podman(["run", "--rm", "-v", f"{merged_dir}:{MERGED}",
                    _env(ENV_HOST_IMAGE), "dpkg-query",
                    f"--admindir={MERGED}/var/lib/dpkg",
                    "-W", "-f=${Package} ${Status}\n"] + names)
    installed = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[-1] == "installed":
            installed.add(parts[0])
    return installed

# A maintainer script (e.g. a kernel package's postinst) chroots into
# MERGED and expects a live /proc, /sys and /dev there, not empty dirs.
MOUNT_LIVE_FS = (f"mount -t proc proc {MERGED}/proc && "
                f"mount -t sysfs sys {MERGED}/sys && "
                f"mount --rbind /dev {MERGED}/dev && ")

def _apt_get(merged_dir, action, names):
    arch = _env(ENV_ARCH)
    apt_get = (f"apt-get "
              f"-o Dir::State={MERGED}/var/lib/apt "
              f"-o Dir::State::status={MERGED}/var/lib/dpkg/status "
              f"-o Dir::Cache={MERGED}/var/cache/apt "
              f"-o Dir::Etc={MERGED}/etc/apt "
              f"-o DPkg::Chroot-Directory={MERGED} "
              f"-o APT::Architecture={arch} "
              f"-o APT::Architectures::={arch} "
              f"-qqy {action} {' '.join(names)}")
    return _podman(["run", "--rm", "--cap-add=sys_admin",
                    "-v", f"{merged_dir}:{MERGED}",
                    _env(ENV_HOST_IMAGE), "sh", "-c",
                    MOUNT_LIVE_FS + apt_get])

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super(ActionModule, self).run(tmp, task_vars)
        args = self._task.args

        state = args.get("state", "present")
        if state not in ("present", "absent"):
            raise AnsibleActionFail(f"apt: state={state} is not supported "
                                    f"here (only present/absent)")
        if "name" not in args:
            raise AnsibleActionFail("apt: 'name' is required")
        names = _names(args["name"])

        merged_dir = _merged_dir(_env(ENV_CID))
        installed = _installed(merged_dir, names)
        wanted_change = ([n for n in names if n not in installed] if state == "present"
                         else [n for n in names if n in installed])

        result["changed"] = len(wanted_change) > 0
        if not wanted_change or self._task.check_mode:
            return result

        action = "install" if state == "present" else "remove"
        proc = _apt_get(merged_dir, action, wanted_change)
        if proc.returncode != 0:
            result["failed"] = True
            result["msg"] = f"apt-get {action} failed: {proc.stderr.strip()}"
        return result
