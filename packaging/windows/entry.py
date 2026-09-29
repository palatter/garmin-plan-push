"""The standalone build's entry point: the `gpp` command, frozen."""

import sys

from gpp.cli import main

if __name__ == "__main__":
    sys.exit(main())
