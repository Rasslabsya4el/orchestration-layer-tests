"""`python -m tools.agentops ...` entry point (requirement 2)."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
