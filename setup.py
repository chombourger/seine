from setuptools import setup, find_packages
setup(
    name="seine",
    version="0.1",
    url="https://github.com/chombourger/seine",
    author="Cedric Hombourger",
    author_email="chombourger@gmail.com",
    packages=find_packages(),
    package_data={"seine": ["data/*.yml", "data/module/*", "data/cross/*"]},
    entry_points = {
        'console_scripts': ['seine=seine.cli:main'],
    },
    install_requires=[
        'pyyaml>=3.12',
        'ansible-core>=2.15',
        'jinja2>=3.0',
    ],
    extras_require={
        'tui': ['textual'],
    },
)
