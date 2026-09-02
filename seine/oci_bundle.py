# seine - Slim Embedded Images Now Easy
# SPDX-License-Identifier Apache-2.0

import os
import shutil

BUNDLE_DIR = "/usr/share/seine/oci"

_attempted = False

def import_bundled():
    global _attempted
    if _attempted:
        return
    _attempted = True
    if os.path.isdir(BUNDLE_DIR) == False:
        return
    from seine.utils import ContainerEngine
    for release in sorted(os.listdir(BUNDLE_DIR)):
        release_dir = os.path.join(BUNDLE_DIR, release)
        images = os.path.join(release_dir, "images.tar.gz")
        if os.path.isfile(images):
            ContainerEngine.run(["load", "-i", images])
        chroots = os.path.join(release_dir, "chroots")
        if os.path.isdir(chroots):
            shutil.copytree(chroots, ContainerEngine.cache("chroots"),
                            dirs_exist_ok=True)
