"""Entrypoint copied only into the trusted D18 runner, never the worker."""

import sys

sys.path.insert(0, "/app/d18")
from benchmark import main

if __name__ == "__main__":
    main()
