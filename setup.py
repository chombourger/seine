import os
import shutil

from setuptools import setup, find_packages
from setuptools.command.build_py import build_py as _build_py

class build_py(_build_py):
    # docs/*.md is a sibling of seine/, so package_data (scoped inside
    # seine/) can't reach it -- copied in here before the normal build,
    # so both 'pip install .' and the .deb ship what 'docs' (ai.py)
    # reads. Guarded: a tarball without docs/ just skips it.
    def run(self):
        here = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(here, "docs")
        if os.path.isdir(src):
            dst = os.path.join(here, "seine", "data", "docs")
            os.makedirs(dst, exist_ok=True)
            for name in os.listdir(src):
                if name.endswith(".md"):
                    shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
        super().run()

setup(
    name="seine",
    version="0.1",
    url="https://github.com/chombourger/seine",
    author="Cedric Hombourger",
    author_email="chombourger@gmail.com",
    packages=find_packages(),
    package_data={"seine": ["data/*.yml", "data/*.txt", "data/prompt/*.txt",
                            "data/module/*", "data/cross/*",
                            "data/docs/*.md"]},
    cmdclass={"build_py": build_py},
    entry_points = {
        'console_scripts': ['seine=seine.cli:main'],
    },
    install_requires=[
        'pyyaml>=3.12',
        'ansible-core>=2.15',
        'jinja2>=3.0',
    ],
    extras_require={
        # rich<14: textual 2.1.2's own 'Style.meta' caches its dict via
        # marshal.dumps/loads -- rich 15 changed how that gets built and
        # the round-trip breaks under Python 3.13 ('bad marshal data
        # (unknown type code)'), crashing any render that touches a
        # styled Text with meta (e.g. #chatlog's clickable tool rows).
        # pip has no reason to avoid rich 15 on its own: textual only
        # declares 'rich>=13.3.3', no upper bound.
        # pyte: pure-Python VT100 emulation for the Remote Target screen's
        # console pane (seine/tui/target.py) -- no C extension, small
        # enough to sit alongside textual rather than its own extra.
        'tui': ['textual', 'rich<14', 'pyte'],
        # The optional AI chat, seine/tui/ai.py -- never a dependency of
        # 'tui' itself, only of the one module that imports it.
        'ai': ['litellm', 'jsonpath-ng', 'ruamel.yaml'],
    },
)
