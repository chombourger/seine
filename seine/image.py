
import os
import subprocess
import tarfile
import tempfile
import yaml

from seine.bootstrap import HostBootstrap
from seine.bootstrap import TargetBootstrap
from seine.imager    import Imager
from seine.utils     import ContainerEngine

class Image:
    def __init__(self, partitionHandler, options={}):
        self.partitionHandler = partitionHandler
        self.options = options
        self.hostBootstrap = None
        self.iid = None
        self.targetBootstrap = None
        self._image = None
        self._output = None
        self._tarball = None
        self._verbose = options["verbose"]

    def __del__(self):
        if self._tarball:
            os.unlink(self._tarball)

    def parse(self, spec):
        if "image" not in spec:
            raise ValueError("'image' not found in provided specification!")

        distro = spec["distribution"] if "distribution" in spec else {}
        if "source" not in distro:
            distro["source"] = "debian"
        if "release" not in distro:
            distro["release"] = "buster"
        if "architecture" not in distro:
            distro["architecture"] = "amd64"
        if "uri" not in distro:
            distro["uri"] = "http://ftp.debian.org/debian"
        spec["distribution"] = distro

        playbook = spec["playbook"] if "playbook" in spec else {}
        spec["playbook"] = playbook

        image = spec["image"]
        if "filename" not in image:
            raise ValueError("output 'filename' not specified in 'image' section!")
        self._output = image["filename"]

        self.spec = spec
        return self.spec

    def _finalize_playbooks(self):
        if "playbook" in self.spec:
            for playbook in self.spec["playbook"]:
                playbook["hosts"] = "localhost"
                if "priority" not in playbook:
                    playbook["priority"] = 500
            self.spec["playbook"] = sorted(self.spec["playbook"], key=lambda p: p["priority"])
            for playbook in self.spec["playbook"]:
                playbook.pop("priority", None)

    def rootfs(self):
        self._finalize_playbooks()
        ansible = self.spec["playbook"]
        ansiblefile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(ansible, ansiblefile)
        ansiblefile.close()

        iidfile = tempfile.NamedTemporaryFile(mode="r", delete=False)

        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {0} AS playbooks
            RUN ansible-playbook {1} /host-tmp/{2}
            FROM playbooks as clean
            RUN apt-get autoremove -qy ansible && \
                apt-get clean -y &&               \
                rm -f /usr/bin/qemu-*-static
            CMD /bin/true
        """
        .format(
            self.targetBootstrap.name,
            "-v" if self._verbose else "",
            os.path.basename(ansiblefile.name)))
        dockerfile.close()

        try:
            self.iid = None
            cmd = [ "build", "--rm", "--iidfile", iidfile.name,
                    "-v", "/tmp:/host-tmp:ro", "-f", dockerfile.name]
            if self._verbose == False:
                cmd.append("-q")
            ContainerEngine.run(cmd, check=True)
            iidfile.seek(0)
            self.iid = iidfile.readline()
        except subprocess.CalledProcessError:
            raise
        finally:
            os.unlink(ansiblefile.name)
            os.unlink(dockerfile.name)
            os.unlink(iidfile.name)

    def build_tarball(self):
        try:
            self._tarball = None
            image = tempfile.NamedTemporaryFile(mode="w", delete=False, dir=os.getcwd())
            self.cid = ContainerEngine.check_output(["container", "create", self.iid]).strip()
            ContainerEngine.run(["container", "export", "-o", image.name, self.cid], check=True)
            self._tarball = image.name
        except subprocess.CalledProcessError:
            os.unlink(image.name)
            raise
        finally:
            if self.cid:
                ContainerEngine.run(["container", "rm", self.cid], check=False)
                self.cid = None
            if self.iid:
                ContainerEngine.run(["image", "rm", self.iid], check=False)
                self.iid = None
            ContainerEngine.run(["image", "prune"], check=False)

    def _size_partitions(self):
        tar = tarfile.open(self._tarball, "r")
        files = tar.getmembers()
        for f in files:
            self.partitionHandler.distribute(f)
        tar.close()
        self.partitionHandler.compute_sizes()
        self.partitionHandler.print_stats()

    def _empty_disk(self):
        size = self.partitionHandler.disk_size()
        image = tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=os.getcwd())
        image.truncate(size)
        image.close()
        self._image = image.name

    def build(self):
        try:
            # Create required bootstrap images
            distro = self.spec["distribution"]
            self.hostBootstrap = HostBootstrap(distro, self.options)
            self.targetBootstrap = TargetBootstrap(distro, self.options)
            if ContainerEngine.hasImage(self.hostBootstrap.name) == False:
                self.hostBootstrap.create()
            if ContainerEngine.hasImage(self.targetBootstrap.name) == False:
                self.targetBootstrap.create(self.hostBootstrap)

            # Assemble the root file-system
            self.rootfs()
            self.build_tarball()

            # Prepare target partitions and disk image
            imager = Imager(self)
            self._size_partitions()
            script = self.partitionHandler.script("/dev/ubdb", Imager.TARGET_DIR)
            self._empty_disk()

            # Produce the target image
            imager.create(script, Imager.TARGET_DIR)

            # Rename the image
            os.rename(self._image, self._output)

        except:
            if self._image is not None:
                os.unlink(self._image)
            raise
