
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
    TARGET_DIR = "/dev/image"

    def __init__(self, partitionHandler, options={}):
        self.partitionHandler = partitionHandler
        self.options = options
        self.hostBootstrap = None
        self.iid = None
        self.targetBootstrap = None
        self._image = None
        self._output = None
        self._tarball = None

    def __del__(self):
        if self._tarball:
            os.unlink(self._tarball)

    def parse(self, spec):
        self.spec = spec
        if "distribution" not in self.spec:
            raise ValueError("'distribution' not found in provided specification!")
        if "image" not in self.spec:
            raise ValueError("'image' not found in provided specification!")
        if "playbook" not in self.spec:
            raise ValueError("'playbook' not found in provided specification!")

        distro = self.spec["distribution"]
        if "source" not in distro:
            distro["source"] = "debian"
        if "release" not in distro:
            distro["release"] = "buster"
        if "architecture" not in distro:
            distro["architecture"] = "amd64"
        if "uri" not in distro:
            distro["uri"] = "http://ftp.debian.org/debian"

        image = self.spec["image"]
        if "filename" not in image:
            raise ValueError("output 'filename' not specified in 'image' section!")
        self._output = image["filename"]
        return self.spec

    def rootfs(self):
        ansible = self.spec["playbook"]
        for playbook in ansible:
            playbook["hosts"] = "localhost"
        ansiblefile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(ansible, ansiblefile)
        ansiblefile.close()

        iidfile = tempfile.NamedTemporaryFile(mode="r", delete=False)

        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write("""
            FROM {0} AS playbooks
            RUN ansible-playbook /host-tmp/{1}
            FROM playbooks as clean
            RUN apt-get autoremove -qy ansible && \
                apt-get clean -y &&               \
                rm -f /usr/bin/qemu-*-static
            CMD /bin/true
        """
        .format(self.targetBootstrap.name, os.path.basename(ansiblefile.name)))
        dockerfile.close()

        try:
            self.iid = None
            subprocess.run([
                "podman", "build", "--rm",
                "--iidfile", iidfile.name,
                "-v", "/tmp:/host-tmp:ro",
                "-f", dockerfile.name],
                check=True)
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
            self.cid = subprocess.check_output(["podman", "container", "create", self.iid]).strip()
            subprocess.run(["podman", "container", "export", "-o", image.name, self.cid], check=True)
            self._tarball = image.name
        except subprocess.CalledProcessError:
            os.unlink(image.name)
            raise
        finally:
            if self.cid:
                subprocess.run(["podman", "container", "rm", self.cid], check=False)
                self.cid = None
            if self.iid:
                subprocess.run(["podman", "image", "rm", self.iid], check=False)
                self.iid = None

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
            script = self.partitionHandler.script("/dev/ubdb", Image.TARGET_DIR)
            self._empty_disk()

            # Produce the target image
            imager.create(script, Image.TARGET_DIR)

            # Rename the image
            os.rename(self._image, self._output)

        except:
            if self._image is not None:
                os.unlink(self._image)
            raise


