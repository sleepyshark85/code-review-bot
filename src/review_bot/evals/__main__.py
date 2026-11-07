"""Entry point: ``python -m review_bot.evals``."""

import sys

from review_bot.evals.cli import main

if __name__ == "__main__":
    sys.exit(main())
