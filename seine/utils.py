
import os
import subprocess

class ContainerEngine:
    def hasImage(name):
        result = ContainerEngine.run(["image", "exists", name], check=False)
        return result.returncode == 0
    def _podman_cmd(cmd):
        home = os.path.expanduser("~")
        root = os.path.join(home, ".local", "share", "seine")
        cmd.insert(0, root)
        cmd.insert(0, "--root")
        cmd.insert(0, "podman")
        return cmd
    def run(cmd, check=False):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.run(cmd, check)
    def check_output(cmd):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.check_output(cmd)
    def Popen(cmd, stdin=None, stdout=None, stderr=None):
        cmd = ContainerEngine._podman_cmd(cmd)
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr)
