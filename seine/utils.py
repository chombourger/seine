
import subprocess

class ContainerEngine:
    def hasImage(name):
        result = subprocess.run(["podman", "image", "exists", name], check=False)
        return result.returncode == 0

