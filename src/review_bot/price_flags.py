"""The token-price flags shared by ``pr metrics`` and ``evals compare`` (P2-S10 AC5, P3-S7 AC4).

Prices are never stored in code; the user passes both, or neither.
"""

import argparse
import re
from decimal import Decimal
from typing import Final

from review_bot.errors import ConfigError
from review_bot.pr.metrics import Prices

__all__ = ["add_price_arguments", "prices_from_flags"]

# Digits, with an optional fraction, so a sign, an exponent, nan or inf is refused.
_PRICE: Final = re.compile(r"[0-9]+(\.[0-9]+)?")


def add_price_arguments(parser: argparse.ArgumentParser) -> None:
    """Add ``--price-input-usd-per-mtok`` and ``--price-output-usd-per-mtok`` to ``parser``."""
    parser.add_argument(
        "--price-input-usd-per-mtok", type=_price, help="USD per million input tokens"
    )
    parser.add_argument(
        "--price-output-usd-per-mtok", type=_price, help="USD per million output tokens"
    )


def prices_from_flags(input_price: Decimal | None, output_price: Decimal | None) -> Prices | None:
    """Both prices, or None for neither.

    Raises:
        ConfigError: Naming the missing flag if only one is given.
    """
    if input_price is None and output_price is None:
        return None
    if input_price is None or output_price is None:
        missing = "input" if input_price is None else "output"
        raise ConfigError(
            f"--price-{missing}-usd-per-mtok is missing. Pass both prices to show the cost, or "
            "neither. Run with --help for usage."
        )
    return Prices(input_price, output_price)


def _price(text: str) -> Decimal:
    if not _PRICE.fullmatch(text):
        raise argparse.ArgumentTypeError(
            f"{text!r} isn't a price: pass USD per million tokens as a number of 0 or more, such "
            "as 15 or 0.25"
        )
    return Decimal(text)
