"""Entry point so the worker can be started as ``python -m app.vmm``."""

import sys

from .worker import main

if __name__ == "__main__":
    sys.exit(main())
