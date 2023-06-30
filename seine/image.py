# seine - Singular Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

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
        self._cid = None
        self._iid = None
        self.targetBootstrap = None
        self._from = None
        self._image = None
        self._keep = options["keep"]
        self._output = None
        self._tarball = None
        self._verbose = options["verbose"]

    def __del__(self):
        if self._tarball:
            self._unlink(self._tarball, "root file-system as a tarball")

    def _unlink(self, path, descr):
        if self._keep:
            print("keeping '%s' (%s) as requested" % (path, descr))
        else:
            os.unlink(path)

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

        image = spec["image"]
        if "filename" not in image:
            raise ValueError("output 'filename' not specified in 'image' section!")
        self._output = image["filename"]

        spec = self._parse_playbooks(spec)

        # Make selected 'baseline' visible in the parsed spec (for our test-suite)
        if self._from:
            spec["baseline"] = self._from

        self.spec = spec
        return self.spec

    def _parse_playbooks(self, spec):

        playbooks = spec["playbook"] if "playbook" in spec else []
        if type(playbooks) != type([]):
            raise ValueError("'playbook' shall be a list of Ansible playbooks!")

        # Check provided playbooks
        index = 1
        for playbook in playbooks:
            if type(playbook) != type({}):
                raise ValueError("playbook #%d is not a dictionary!" % index)
            playbook["hosts"] = "localhost"
            if "priority" not in playbook:
                playbook["priority"] = 500
            index = index + 1

        # Order them by ascending priority
        playbooks = sorted(playbooks, key=lambda p: p["priority"])

        # Get selected baseline and remove the "priority" setting since not understood
        # by Ansible (and not needed anymore)
        for playbook in playbooks:
            if "baseline" in playbook:
                if self._from is None:
                    # highest prio 'baseline' wins
                    self._from = playbook["baseline"]
                playbook.pop("baseline", None)
            playbook.pop("priority", None)

        spec["playbook"] = playbooks
        return spec

    def rootfs(self):
        if self._from is None:
            self._from = self.targetBootstrap.name

        ansible = self.spec["playbook"]
        ansiblefile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(ansible, ansiblefile)
        ansiblefile.close()

        iidfile = tempfile.NamedTemporaryFile(mode="r", delete=False)

        dockerfile = tempfile.NamedTemporaryFile(mode="w", delete=False)
        dockerfile.write(IMAGE_ANSIBLE_SCRIPT.format(
            self._from, self.hostBootstrap.name,
            "-v" if self._verbose else "",
            os.path.basename(ansiblefile.name)))
        dockerfile.close()

        try:
            self._iid = None
            cmd = [ "build", "--rm", "--iidfile", iidfile.name,
                    "-v", "/tmp:/host-tmp:ro", "-f", dockerfile.name]
            if self._verbose == False:
                cmd.append("-q")
            ContainerEngine.run(cmd, check=True)
            iidfile.seek(0)
            self._iid = iidfile.readline()
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
            self._cid = ContainerEngine.check_output(["container", "create", self._iid]).strip()
            ContainerEngine.run(["container", "export", "-o", image.name, self._cid], check=True)
            self._tarball = image.name
        except subprocess.CalledProcessError:
            os.unlink(image.name)
            raise
        finally:
            if self._cid:
                ContainerEngine.run(["container", "rm", self._cid], check=False)
                self._cid = None
            if self._iid:
                ContainerEngine.run(["image", "rm", self._iid], check=False)
                self._iid = None
            ContainerEngine.run(["image", "prune", "-f"], check=False)

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
            if self._from is None and ContainerEngine.hasImage(self.targetBootstrap.name) == False:
                self.targetBootstrap.create(self.hostBootstrap)

            # Assemble the root file-system
            self.rootfs()
            self.build_tarball()

            # Prepare target partitions and disk image
            imager = Imager(self)
            self._size_partitions()
            script = self.partitionHandler.script("/dev/sdb", Imager.TARGET_DIR)
            self._empty_disk()

            # Produce the target image
            imager.create(script, Imager.TARGET_DIR)

            # Rename the image
            os.rename(self._image, self._output)

        except:
            if self._image is not None:
                os.unlink(self._image)
            raise

IMAGE_ANSIBLE_SCRIPT = """
FROM {0} AS playbooks
COPY --from={1} /opt/seine /opt/seine
RUN apt-get update -qqy && \
    apt-get install -qqy /opt/seine/seine-ansible*.deb && \
    ansible-playbook {2} /host-tmp/{3}
FROM playbooks as clean
RUN apt-get autoremove -qy seine-ansible && \
    apt-get clean -y &&                     \
    rm -rf /var/lib/apt/lists/* &&          \
    rm -f /usr/bin/qemu-*-static
CMD /bin/true
"""
