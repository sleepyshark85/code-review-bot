"""Token prices: their flags, and what tokens cost at them (P2-S10 AC5, P3-S7 AC4, P4-S6).

Prices are never stored in code; the user passes both, or neither. ``pr metrics``, ``pr
compare`` and ``evals compare`` take them as flags, and ``--provider openai`` as settings.
"""

import argparse
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from review_bot.errors import ConfigError

__all__ = [
    "Prices",
    "add_price_arguments",
    "prices_from_flags",
    "read_price",
    "token_cost_usd",
]

# Digits, with an optional fraction, so a sign, an exponent, nan or inf is refused.
_PRICE: Final = re.compile(r"[0-9]+(\.[0-9]+)?")
# Prices are per million tokens.
_TOKENS_PER_PRICE: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class Prices:
    """The user's token prices, in USD per million tokens; never stored in code (design)."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


def token_cost_usd(input_tokens: int, output_tokens: int, prices: Prices) -> Decimal:
    """What the tokens cost at ``prices``, before rounding to cents."""
    spent = input_tokens * prices.input_usd_per_mtok + output_tokens * prices.output_usd_per_mtok
    return spent / _TOKENS_PER_PRICE


def read_price(text: str) -> Decimal:
    """``text`` as a price in USD per million tokens: a number of 0 or more, such as 0.25.

    Raises:
        ValueError: Saying what a price looks like, if ``text`` has a sign, an exponent, or
            isn't a number.
    """
    if not _PRICE.fullmatch(text):
        raise ValueError(
            f"{text!r} isn't a price: pass USD per million tokens as a number of 0 or more, such "
            "as 15 or 0.25"
        )
    return Decimal(text)


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
    try:
        return read_price(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
