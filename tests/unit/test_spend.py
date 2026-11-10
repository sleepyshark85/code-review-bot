import argparse
from decimal import Decimal

import pytest

from review_bot.spend import DollarBudget, usd_amount

BUDGET = DollarBudget(max_usd=Decimal("7.20"), max_cost_usd=Decimal("0.60"))


@pytest.mark.parametrize(
    ("planned_calls", "covered"), [(0, True), (12, True), (13, False)], ids=["none", "at", "over"]
)
def test_dollar_budget_covers_planned_calls_only_at_their_cap_within_the_allowance(
    planned_calls: int, *, covered: bool
) -> None:
    assert BUDGET.covers(planned_calls) is covered


def test_dollar_budget_plans_each_call_at_its_cap() -> None:
    assert BUDGET.planned_usd(13) == Decimal("7.80")


@pytest.mark.parametrize(
    ("spent_usd", "has_room"),
    [("0", True), ("6.60", True), ("6.61", False), ("7.20", False)],
    ids=["nothing-spent", "one-cap-left", "less-than-a-cap-left", "all-spent"],
)
def test_dollar_budget_has_room_only_while_another_call_at_its_cap_fits(
    spent_usd: str, *, has_room: bool
) -> None:
    assert BUDGET.has_room(Decimal(spent_usd)) is has_room


def test_dollar_budget_charges_the_reported_cost_or_the_cap_if_none_was_reported() -> None:
    assert BUDGET.charge(Decimal("0.1234")) == Decimal("0.1234")
    assert BUDGET.charge(None) == Decimal("0.60")


@pytest.mark.parametrize(("max_usd", "max_cost_usd"), [("0", "0.60"), ("7.20", "0"), ("-1", "1")])
def test_dollar_budget_refuses_an_amount_not_above_0(max_usd: str, max_cost_usd: str) -> None:
    with pytest.raises(ValueError, match="above 0"):
        DollarBudget(max_usd=Decimal(max_usd), max_cost_usd=Decimal(max_cost_usd))


@pytest.mark.parametrize("text", ["0.01", "8.10", "1000"])
def test_usd_amount_reads_a_decimal_above_0_exactly(text: str) -> None:
    assert usd_amount(text) == Decimal(text)


@pytest.mark.parametrize("text", ["0", "-0.30", "nan", "inf", "", "abc"])
def test_usd_amount_refuses_anything_but_a_finite_decimal_above_0(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="isn't an amount of USD above 0"):
        usd_amount(text)
