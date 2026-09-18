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

class ApptGetBuildsTheChrootRecipe(avocado.Test):
    def test_the_command_names_dpkg_chroot_directory_not_rootdir(self):
        with patch.dict(os.environ, ENV), patch("subprocess.run") as run:
            apt_action._apt_get("/merged", "install", ["vim"], False)
        script = run.call_args.args[0][-1]
        self.assertIn("DPkg::Chroot-Directory=/rootfs", script)
        self.assertNotIn("RootDir", script)
        self.assertIn("APT::Architecture=arm64", script)
        self.assertIn("install vim", script)

    def test_simulating_skips_the_real_install_and_the_mounts(self):
        with patch.dict(os.environ, ENV), patch("subprocess.run") as run:
            apt_action._apt_get("/merged", "install", ["vim"], True)
        args, script = run.call_args.args[0], run.call_args.args[0][-1]
        self.assertNotIn("--cap-add=sys_admin", args)
        self.assertNotIn("mount -t proc", script)
        # Still simulated once, so a would-be change is still reported.
        self.assertIn("-s | grep", script)

class ChangedIsReadFromAptGetsOwnOutput(avocado.Test):
    def test_an_inst_or_remv_line_marks_the_task_changed(self):
        class Proc:
            returncode = 0
            stdout = apt_action.CHANGED_MARKER + "\n"
        action = apt_action.ActionModule.__new__(apt_action.ActionModule)
        action._task = type("Task", (), {"args": {"name": "vim"},
                                         "check_mode": False})()
        with patch.dict(os.environ, ENV), \
             patch.object(apt_action.ActionBase, "run", return_value={}), \
             patch.object(apt_action, "_merged_dir", return_value="/merged"), \
             patch.object(apt_action, "_apt_get", return_value=Proc()):
            result = action.run()
        self.assertTrue(result["changed"])

    def test_no_marker_means_nothing_changed(self):
        class Proc:
            returncode = 0
            stdout = ""
        action = apt_action.ActionModule.__new__(apt_action.ActionModule)
        action._task = type("Task", (), {"args": {"name": "vim"},
                                         "check_mode": False})()
        with patch.dict(os.environ, ENV), \
             patch.object(apt_action.ActionBase, "run", return_value={}), \
             patch.object(apt_action, "_merged_dir", return_value="/merged"), \
             patch.object(apt_action, "_apt_get", return_value=Proc()):
            result = action.run()
        self.assertFalse(result["changed"])

class RunFailsClosedOnAnUnsupportedState(avocado.Test):
    def test_state_latest_is_rejected(self):
        from ansible.errors import AnsibleActionFail
        action = apt_action.ActionModule.__new__(apt_action.ActionModule)
        action._task = type("Task", (), {"args": {"name": "vim", "state": "latest"}})()
        with self.assertRaises(AnsibleActionFail):
            with patch.object(apt_action.ActionBase, "run", return_value={}):
                action.run()
