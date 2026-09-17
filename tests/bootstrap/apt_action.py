#!/usr/bin/env python3
# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier: Apache-2.0

import avocado
import importlib.util
import os
import sys

from unittest.mock import patch

path_to_self = os.path.realpath(__file__)
path_to_sources = os.path.join(os.path.dirname(path_to_self), "..", "..")
sys.path.append(path_to_sources)

# Not a normal package (ansible loads it by path, not import) -- loaded
# the same way here.
PLUGIN = os.path.join(path_to_sources, "seine", "data", "ansible",
                      "action_plugins", "apt.py")
spec = importlib.util.spec_from_file_location("apt_action", PLUGIN)
apt_action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apt_action)

ENV = {
    apt_action.ENV_CID: "deadbeef",
    apt_action.ENV_ROOT: "/build/containers",
    apt_action.ENV_RUNROOT: "/build/containers/run",
    apt_action.ENV_ARCH: "arm64",
    apt_action.ENV_HOST_IMAGE: "bootstrap/host",
}

class NamesAcceptAStringOrAList(avocado.Test):
    def test_a_list_is_kept_as_is(self):
        self.assertEqual(apt_action._names(["vim", "curl"]), ["vim", "curl"])

    def test_a_comma_or_space_separated_string_is_split(self):
        self.assertEqual(apt_action._names("vim, curl"), ["vim", "curl"])

class InstalledParsesDpkgQueryOutput(avocado.Test):
    def test_only_fully_installed_packages_are_reported(self):
        class Proc:
            stdout = ("vim install ok installed\n"
                     "curl deinstall ok config-files\n")
        with patch.dict(os.environ, ENV), patch("subprocess.run", return_value=Proc()):
            self.assertEqual(apt_action._installed("/merged", ["vim", "curl"]),
                            {"vim"})

class ApptGetBuildsTheChrootRecipe(avocado.Test):
    def test_the_command_names_dpkg_chroot_directory_not_rootdir(self):
        with patch.dict(os.environ, ENV), patch("subprocess.run") as run:
            apt_action._apt_get("/merged", "install", ["vim"])
        script = run.call_args.args[0][-1]
        self.assertIn("DPkg::Chroot-Directory=/rootfs", script)
        self.assertNotIn("RootDir", script)
        self.assertIn("APT::Architecture=arm64", script)
        self.assertIn("install vim", script)

class RunFailsClosedOnAnUnsupportedState(avocado.Test):
    def test_state_latest_is_rejected(self):
        from ansible.errors import AnsibleActionFail
        action = apt_action.ActionModule.__new__(apt_action.ActionModule)
        action._task = type("Task", (), {"args": {"name": "vim", "state": "latest"}})()
        with self.assertRaises(AnsibleActionFail):
            with patch.object(apt_action.ActionBase, "run", return_value={}):
                action.run()
