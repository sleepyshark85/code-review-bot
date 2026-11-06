"""Unit tests for `review`'s secret check: the provider is never called when the scan hits."""

import asyncio
from pathlib import Path

import pytest

from review_bot.errors import SecretDetectedError
from review_bot.providers.mock import MockProvider
from review_bot.review import review_diff
from review_bot.settings import Settings

AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"


def review_expecting_secrets(path: Path, text: str) -> tuple[str, MockProvider]:
    path.write_text(text, encoding="utf-8")
    provider = MockProvider()
    with pytest.raises(SecretDetectedError) as caught:
        asyncio.run(review_diff(path, Settings(provider="mock"), provider))
    return str(caught.value), provider


def test_review_diff_with_a_secret_names_it_without_the_path_and_calls_no_provider(
    tmp_path: Path,
) -> None:
    message, provider = review_expecting_secrets(
        tmp_path / f"{AWS_KEY}.diff", f"not a diff\n{AWS_KEY}\n"
    )

    assert message == (
        "secret: 1 possible secret in the diff (aws-access-key at diff line 2), so it was not "
        "sent to the model. Remove it and rotate it if it is real, then run again."
    )
    assert provider.requests == []


def test_review_diff_with_seven_secrets_names_the_first_five_and_counts_the_rest(
    tmp_path: Path,
) -> None:
    message, _ = review_expecting_secrets(tmp_path / "change.diff", f"+{AWS_KEY}\n" * 7)

    named = "; ".join(f"aws-access-key at diff line {line}" for line in range(1, 6))
    assert message == (
        f"secret: 7 possible secrets in the diff ({named}; 2 more), so it was not sent to the "
        "model. Remove them and rotate any that are real, then run again."
    )
