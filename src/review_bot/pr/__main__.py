"""Entry point: ``python -m review_bot.pr``."""

import sys

from review_bot.pr.cli import main

if __name__ == "__main__":
    sys.exit(main())
