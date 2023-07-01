# seine - Singular Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import subprocess

from seine.utils import ContainerEngine

class SBOM:
    def __init__(self, options={}):
        self.options = options

    def _output_file(self, image):
        output = None
        if 'sbom' in self.options and self.options['sbom'] is True:
            suffix = '-sbom.json'
            output = os.path.realpath(image)
            if output.endswith('.img'):
                output = output.removesuffix('.img')
            output = output + suffix
        return output

    def generate(self, image):
        image = os.path.realpath(image)
        output = self._output_file(image)
        if output is not None:
            dir = os.path.dirname(image)
            run_cmd = ['run', '-v', '{}:{}:z'.format(dir, dir), '-t', 'docker.io/anchore/syft']
            syft_cmd = ['-q', '-o', 'spdx-json', '--file', output, image]
            cmd = [*run_cmd, *syft_cmd]
            ContainerEngine.run(cmd)
