"""Command-line entry point: ``python -m review_bot``."""

import sys

from review_bot.cli import main

if __name__ == "__main__":
    sys.exit(main())
