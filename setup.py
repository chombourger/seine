from setuptools import setup, find_packages
setup(
    name="seine",
    version="0.1",
    url="https://github.com/chombourger/seine",
    author="Cedric Hombourger",
    author_email="chombourger@gmail.com",
    packages=find_packages(),
    entry_points = {
        'console_scripts': ['seine=seine.cli:main'],
    },
    install_requires=[
        'pyyaml>=3.12',
    ],
)
