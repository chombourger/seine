#!/usr/bin/python3

import os
import sys

path_to_self    = os.path.realpath(__file__)
path_to_sources = os.path.dirname(path_to_self)
sys.path.append(path_to_sources)

from seine.cli import main

if __name__ == "__main__":
    main()