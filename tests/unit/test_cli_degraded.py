"""``review-bot DIFF`` when no model is available and the policy degrades to rules (P4-S8)."""

import json
from pathlib import Path

import pytest
from audit_records import data_of
from eval_cases import CALC_DIFF, CALC_FILE

from review_bot.cli import main
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.providers.mock import MockProvider

DEGRADES = b'version = "d"\n[routing]\ndegrade_to_rules = true\n'
UNAVAILABLE_EXIT = 7
AUTH_EXIT = 8


def write_diff(tmp_path: Path) -> Path:
    path = tmp_path / "change.diff"
    path.write_text(CALC_DIFF, encoding="utf-8")
    return path


def review(tmp_path: Path, error: ProviderError, *, json_format: bool = False) -> int:
    """Review the diff with a swallowed exception under a policy that degrades to rules."""
    policy_path = tmp_path / "p.toml"
    policy_path.write_bytes(DEGRADES)
    argv = [
        str(write_diff(tmp_path)),
        "--provider",
        "mock",
        "--threshold",
        "low",
        "--policy",
        str(policy_path),
    ]
    if json_format:
        argv += ["--format", "json"]
    return main(argv, provider=MockProvider(error=error))


def test_a_degraded_review_prints_the_rule_findings_says_why_and_exits_with_the_providers_code(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = review(tmp_path, ProviderError(ErrorCategory.UNAVAILABLE, "Claude is down."))

    out, err = capsys.readouterr()
    assert exit_code == UNAVAILABLE_EXIT
    assert out.startswith(f"{CALC_FILE}:4 [medium/")
    assert "review-bot: warning: degraded: no model was available" in err
    assert "review-bot: error: unavailable: Claude is down." in err
    assert data_of(audit_log_in_tmp_path, "run.end") == {
        "exit_code": UNAVAILABLE_EXIT,
        "error": "unavailable",
    }


def test_a_degraded_json_report_is_rules_only(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    review(tmp_path, ProviderError(ErrorCategory.RATE_LIMIT, "Slow down."), json_format=True)

    document = json.loads(capsys.readouterr().out)
    assert document["degraded"] == "rules_only"
    assert [finding["source"] for finding in document["findings"]] == ["rule"]


def test_a_review_that_isnt_degraded_has_a_null_degraded(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(
        [str(write_diff(tmp_path)), "--provider", "mock", "--format", "json"],
        provider=MockProvider(),
    )

    assert json.loads(capsys.readouterr().out)["degraded"] is None


def test_under_the_builtin_policy_an_unavailable_model_fails_as_before(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = ProviderError(ErrorCategory.UNAVAILABLE, "Down.")

    exit_code = main(
        [str(write_diff(tmp_path)), "--provider", "mock"], provider=MockProvider(error=error)
    )

    out, err = capsys.readouterr()
    assert (exit_code, out) == (UNAVAILABLE_EXIT, "")
    assert "degraded" not in err


def test_a_failure_that_isnt_routable_fails_even_when_the_policy_degrades(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = review(tmp_path, ProviderError(ErrorCategory.AUTH, "Bad key."))

    out, _ = capsys.readouterr()
    assert (exit_code, out) == (AUTH_EXIT, "")
