import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from git_samples import commit, git, init_repo

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.pr.cli import main
from review_bot.providers.base import LLMProvider, ReviewRequest, ReviewResponse
from review_bot.providers.mock import MockProvider

REPO_NAME = "owner/repo"
MERGED_AT = "2026-11-07T08:09:10Z"


class ProviderPerCall:
    """Answers the n-th request as the n-th scripted provider does."""

    def __init__(self, *providers: MockProvider) -> None:
        self._providers = list(providers)
        self.requests: list[ReviewRequest] = []

    async def review(self, request: ReviewRequest) -> ReviewResponse:
        self.requests.append(request)
        return await self._providers[len(self.requests) - 1].review(request)


@pytest.fixture(autouse=True)
def outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def merged_pulls(repo: Path, count: int) -> list[dict[str, Any]]:
    """``count`` merged pull requests, each changing ``app.py`` once on top of the last."""
    base = commit(repo, {"app.py": "value = 0\n"})
    pulls = []
    for number in range(1, count + 1):
        head = commit(repo, {"app.py": f"value = {number}\n"})
        pulls.append(
            {
                "number": number,
                "baseRefOid": base,
                "headRefOid": head,
                "mergedAt": MERGED_AT,
                "isCrossRepository": False,
            }
        )
        base = head
    return pulls


def write_pulls(tmp_path: Path, pulls: object) -> Path:
    path = tmp_path / "pulls.json"
    path.write_text(json.dumps(pulls), encoding="utf-8")
    return path


def backfill(
    tmp_path: Path,
    repo: Path,
    pulls: object,
    *flags: str,
    provider: LLMProvider,
    max_calls: int = 10,
) -> int:
    return main(
        [
            "backfill",
            "--pulls",
            str(write_pulls(tmp_path, pulls)),
            "--github-repo",
            REPO_NAME,
            "--repo",
            str(repo),
            "--out",
            str(tmp_path / "out"),
            "--max-calls",
            str(max_calls),
            "--provider",
            "mock",
            *flags,
        ],
        provider=provider,
    )


def report(tmp_path: Path, number: int) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (tmp_path / "out" / str(number) / "shadow.json").read_text(encoding="ascii")
    )
    return document


def last_line(out: str) -> dict[str, int]:
    *_, line = out.rstrip("\n").split("\n")
    prefix, _, pairs = line.partition(": ")
    assert prefix == "backfill"
    return {name: int(count) for name, count in (pair.split("=") for pair in pairs.split())}


def test_backfill_writes_one_backfill_report_per_pull_request_each_its_own_run(
    tmp_path: Path, repo: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 3)
    provider = MockProvider(findings=[])

    exit_code = backfill(tmp_path, repo, pulls, provider=provider)

    assert exit_code == 0
    assert len(provider.requests) == 3
    documents = [report(tmp_path, number) for number in (1, 2, 3)]
    for pull, document in zip(pulls, documents, strict=True):
        assert (document["mode"], document["origin"], document["outcome"]) == (
            "shadow",
            "backfill",
            "reviewed",
        )
        assert document["pr"] == {
            "repo": REPO_NAME,
            "number": pull["number"],
            "base_sha": pull["baseRefOid"],
            "head_sha": pull["headRefOid"],
        }
        assert document["event_at"] == "2026-11-07T08:09:10+00:00"
    run_ids = {document["run_id"] for document in documents}
    assert len(run_ids) == 3
    assert {record["run_id"] for record in read_records(audit_log_in_tmp_path)} == run_ids
    out = capsys.readouterr().out
    assert "backfill: entries=3 already_in_out=0 run=3\n" in out
    assert last_line(out) == {
        "reviewed": 3,
        "blocked_secret": 0,
        "skipped": 0,
        "failed": 0,
        "too_large": 0,
    }


def test_backfill_keeps_an_existing_report_and_doesnt_count_it_towards_max_calls(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 2)
    existing = tmp_path / "out" / "1" / "shadow.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"kept as it is\n")
    provider = MockProvider(findings=[])

    exit_code = backfill(tmp_path, repo, pulls, provider=provider, max_calls=1)

    assert exit_code == 0
    assert existing.read_bytes() == b"kept as it is\n"
    assert len(provider.requests) == 1
    assert report(tmp_path, 2)["outcome"] == "reviewed"
    assert "backfill: entries=2 already_in_out=1 run=1\n" in capsys.readouterr().out


def test_backfill_over_max_calls_refuses_before_any_call_naming_both_counts(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 5)
    pulls[0]["isCrossRepository"] = True
    provider = MockProvider()

    exit_code = backfill(tmp_path, repo, pulls, provider=provider, max_calls=3)

    assert exit_code == 2
    assert provider.requests == []
    assert "would make up to 5 calls, more than --max-calls 3" in capsys.readouterr().err
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.parametrize(
    ("event", "flags"),
    [
        ("pull_request", ["--allow-dispatch"]),
        ("push", ["--allow-dispatch"]),
        ("schedule", ["--allow-dispatch"]),
        (None, ["--allow-dispatch"]),
        ("workflow_dispatch", []),
    ],
)
def test_backfill_in_ci_refuses_before_any_call_or_creating_out_unless_an_allowed_dispatch(  # noqa: PLR0913, PLR0917 - parameters and fixtures pytest passes
    event: str | None,
    flags: list[str],
    tmp_path: Path,
    repo: Path,
    ci_run: Callable[[str | None], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    ci_run(event)
    provider = MockProvider()

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 1), *flags, provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    assert not (tmp_path / "out").exists()
    assert (
        "backfill runs in CI only from a workflow_dispatch run given --allow-dispatch"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize("ci", [True, False], ids=["ci-workflow-dispatch", "outside-ci"])
def test_backfill_given_allow_dispatch_runs_in_a_ci_workflow_dispatch_and_outside_ci(
    ci: bool,
    tmp_path: Path,
    repo: Path,
    audit_log_in_tmp_path: Path,
    ci_run: Callable[[str | None], None],
) -> None:
    if ci:
        ci_run("workflow_dispatch")
    provider = MockProvider(findings=[])

    exit_code = backfill(
        tmp_path, repo, merged_pulls(repo, 1), "--allow-dispatch", provider=provider
    )

    assert exit_code == 0
    assert len(provider.requests) == 1


def test_backfill_with_a_bad_entry_exits_19_naming_it_before_any_call(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 2)
    pulls[1]["headRefOid"] = "abc"
    provider = MockProvider()

    exit_code = backfill(tmp_path, repo, pulls, provider=provider)

    assert exit_code == 19
    assert provider.requests == []
    assert "entry 2: headRefOid isn't a 40-hex-digit SHA" in capsys.readouterr().err


def test_backfill_skips_a_fork_and_a_missing_commit_and_runs_the_rest(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 3)
    pulls[0]["isCrossRepository"] = True
    pulls[1]["headRefOid"] = "e" * 40
    provider = MockProvider(findings=[])

    exit_code = backfill(tmp_path, repo, pulls, provider=provider)

    assert exit_code == 0
    assert len(provider.requests) == 1
    assert [(report(tmp_path, n)["outcome"], report(tmp_path, n)["reason"]) for n in (1, 2, 3)] == [
        ("skipped", "fork"),
        ("skipped", "missing_commit"),
        ("reviewed", None),
    ]
    captured = capsys.readouterr()
    assert "pull request 2's base or head commit isn't in --repo" in captured.err
    assert "git fetch origin refs/pull/2/head:refs/pull/2/head" in captured.err
    assert "fetch the base branch's full history" in captured.err
    assert last_line(captured.out) == {
        "reviewed": 1,
        "blocked_secret": 0,
        "skipped": 2,
        "failed": 0,
        "fork": 1,
        "missing_commit": 1,
        "too_large": 0,
    }


def test_backfill_counts_a_pull_request_over_the_review_byte_cap_as_too_large(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()

    exit_code = backfill(
        tmp_path, repo, merged_pulls(repo, 1), "--max-review-bytes", "5", provider=provider
    )

    assert exit_code == 0
    assert provider.requests == []
    assert last_line(capsys.readouterr().out)["too_large"] == 1


def test_backfill_continues_past_a_provider_failure_and_exits_with_the_first(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = ProviderPerCall(
        MockProvider(findings=[]),
        MockProvider(error=ProviderError(ErrorCategory.RATE_LIMIT, "Wait.")),
        MockProvider(error=ProviderError(ErrorCategory.AUTH, "Log in.")),
        MockProvider(findings=[]),
    )

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 4), provider=provider)

    assert exit_code == 9
    assert [report(tmp_path, n)["outcome"] for n in (1, 2, 3, 4)] == [
        "reviewed",
        "failed",
        "failed",
        "reviewed",
    ]
    counts = last_line(capsys.readouterr().out)
    assert (counts["failed"], counts["rate_limit"], counts["auth"]) == (2, 1, 1)


def test_backfill_makes_every_call_with_no_retries_whatever_is_configured(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "2")

    backfill(tmp_path, repo, merged_pulls(repo, 2), "--max-retries", "3", provider=MockProvider())

    assert [report(tmp_path, n)["caps"]["max_retries"] for n in (1, 2)] == [0, 0]


def test_backfill_refuses_an_invalid_retries_setting_before_forcing_it_to_0(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REVIEW_BOT_MAX_RETRIES", "abc")
    provider = MockProvider()

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 1), provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    assert "REVIEW_BOT_MAX_RETRIES" in capsys.readouterr().err


def test_backfill_reviews_a_failed_report_again_and_keeps_the_others(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 3)
    first = ProviderPerCall(
        MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Again.")),
        MockProvider(findings=[]),
    )
    pulls[2]["isCrossRepository"] = True
    backfill(tmp_path, repo, pulls, provider=first)
    kept = {n: (tmp_path / "out" / str(n) / "shadow.json").read_bytes() for n in (2, 3)}
    capsys.readouterr()
    second = MockProvider(findings=[])

    exit_code = backfill(tmp_path, repo, pulls, provider=second, max_calls=1)

    assert exit_code == 0
    assert len(second.requests) == 1
    assert report(tmp_path, 1)["outcome"] == "reviewed"
    assert {n: (tmp_path / "out" / str(n) / "shadow.json").read_bytes() for n in (2, 3)} == kept
    assert "backfill: entries=3 already_in_out=2 run=1\n" in capsys.readouterr().out


def test_backfill_counts_a_failed_report_towards_max_calls(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 1)
    backfill(
        tmp_path,
        repo,
        pulls,
        provider=MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Again.")),
    )
    capsys.readouterr()
    provider = MockProvider()

    exit_code = backfill(tmp_path, repo, pulls, provider=provider, max_calls=0)

    assert exit_code == 2
    assert provider.requests == []
    assert "would make up to 1 calls, more than --max-calls 0" in capsys.readouterr().err


def test_backfill_stopped_by_git_after_a_provider_failure_exits_with_the_git_error(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 1)
    git(repo, "checkout", "-q", "--orphan", "unrelated")
    unrelated = commit(repo, {"other.py": "x = 1\n"})
    pulls.append({**pulls[0], "number": 2, "headRefOid": unrelated})
    provider = MockProvider(error=ProviderError(ErrorCategory.RATE_LIMIT, "Wait."))

    exit_code = backfill(tmp_path, repo, pulls, provider=provider)

    assert exit_code == 19
    assert report(tmp_path, 1)["outcome"] == "failed"
    assert not (tmp_path / "out" / "2").exists()
    captured = capsys.readouterr()
    assert "have no merge base" in captured.err
    assert "backfill: entries=2 already_in_out=0 run=1\n" in captured.out


def test_backfill_with_an_invalid_github_repo_exits_2(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "backfill",
            "--pulls",
            str(write_pulls(tmp_path, [])),
            "--github-repo",
            "not a repo",
            "--repo",
            str(repo),
            "--out",
            str(tmp_path / "out"),
            "--max-calls",
            "1",
        ]
    )

    assert exit_code == 2
    assert "OWNER/NAME" in capsys.readouterr().err


def test_backfill_stops_at_a_git_failure_with_exit_19_and_still_shows_the_counts(
    tmp_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(init_repo(tmp_path / "elsewhere"), 2)
    not_a_clone = tmp_path / "empty"
    not_a_clone.mkdir()
    provider = MockProvider()

    exit_code = backfill(tmp_path, not_a_clone, pulls, provider=provider)

    assert exit_code == 19
    assert provider.requests == []
    assert not (tmp_path / "out" / "1").exists()
    captured = capsys.readouterr()
    assert "git rev-parse failed" in captured.err
    assert "backfill: entries=2 already_in_out=0 run=0\n" in captured.out
    assert read_records(audit_log_in_tmp_path)[-1]["data"] == {"exit_code": 19, "error": "GitError"}


# Context mode and the dollar budget (P3-S8).

CONTEXT_BUDGET = ("--context", "repo", "--max-usd", "1.80", "--max-cost-usd", "0.60")


def totals_line(out: str) -> str:
    *_, totals, _ = out.rstrip("\n").split("\n")
    return totals


@pytest.mark.parametrize(
    "flags",
    [
        ("--context", "repo"),
        ("--context", "repo", "--max-usd", "1.80"),
        ("--context", "repo", "--max-cost-usd", "0.60"),
        ("--max-usd", "1.80"),
    ],
    ids=["neither", "no-max-cost-usd", "no-max-usd", "max-usd-without-a-cap"],
)
def test_backfill_with_a_dollar_budget_missing_an_amount_refuses_before_any_call(
    flags: tuple[str, ...], tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 3), *flags, provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    err = capsys.readouterr().err
    assert "needs both --max-usd and --max-cost-usd" in err
    assert "up to 3 calls" in err
    assert list((tmp_path / "out").iterdir()) == []


def test_backfill_in_context_mode_with_every_report_already_written_runs_without_amounts(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    existing = tmp_path / "out" / "1" / "shadow.json"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"kept as it is\n")
    provider = MockProvider()

    exit_code = backfill(
        tmp_path, repo, merged_pulls(repo, 1), "--context", "repo", provider=provider
    )

    assert exit_code == 0
    assert provider.requests == []
    assert "backfill: entries=1 already_in_out=1 run=0\n" in capsys.readouterr().out


@pytest.mark.parametrize("context", ["repo", "off"])
def test_backfill_whose_planned_calls_at_their_cap_exceed_max_usd_refuses_naming_them(
    context: str, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()
    flags = ("--context", context, "--max-usd", "1.50", "--max-cost-usd", "0.60")

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 3), *flags, provider=provider)

    assert exit_code == 2
    assert provider.requests == []
    assert (
        "up to 3 calls of up to --max-cost-usd 0.60 each, 1.80 USD in all, more than --max-usd "
        "1.50" in capsys.readouterr().err
    )


def test_backfill_in_context_mode_within_its_budget_reviews_each_pull_requests_head(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider(
        findings=[], tool_calls=[("read", {"path": "app.py"})], cost_usd=Decimal("0.25")
    )

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 2), *CONTEXT_BUDGET, provider=provider)

    assert exit_code == 0
    assert provider.tool_results[0].count("value = 1") == 1
    assert provider.tool_results[1].count("value = 2") == 1
    document = report(tmp_path, 2)
    assert document["report"]["mode"] == "context"
    assert document["caps"]["max_cost_usd"] == "0.60"
    assert totals_line(capsys.readouterr().out) == (
        "backfill: entries=2 already_in_out=0 run=2 spent_usd=0.50"
    )


def test_backfill_stops_once_less_than_a_call_is_left_and_writes_no_report_for_the_rest(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 0.70 then 0.55 leave 0.55 of 1.80: less than one more call at 0.60.
    provider = ProviderPerCall(
        MockProvider(findings=[], cost_usd=Decimal("0.70")),
        MockProvider(findings=[], cost_usd=Decimal("0.55")),
    )

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 3), *CONTEXT_BUDGET, provider=provider)

    assert exit_code == 0
    assert len(provider.requests) == 2
    assert not (tmp_path / "out" / "3").exists()
    captured = capsys.readouterr()
    assert totals_line(captured.out) == (
        "backfill: entries=3 already_in_out=0 run=2 spent_usd=1.25 stopped=budget"
    )
    assert "backfill stopped: 1.25 USD spent" in captured.err
    assert "so it reviewed no more: 1 left, with no report" in captured.err


def test_backfill_counts_a_call_with_no_reported_cost_or_a_provider_failure_at_its_cap(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = ProviderPerCall(
        MockProvider(findings=[]),
        MockProvider(error=ProviderError(ErrorCategory.PERMANENT, "Over.", cap="cost")),
    )

    exit_code = backfill(tmp_path, repo, merged_pulls(repo, 2), *CONTEXT_BUDGET, provider=provider)

    assert exit_code == 12
    assert report(tmp_path, 2)["reason"] == "permanent"
    assert totals_line(capsys.readouterr().out).endswith("run=2 spent_usd=1.20")


def test_backfill_charges_nothing_for_a_pull_request_skipped_without_a_call(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pulls = merged_pulls(repo, 3)
    pulls[0]["isCrossRepository"] = True
    provider = MockProvider(findings=[], cost_usd=Decimal("0.60"))

    exit_code = backfill(tmp_path, repo, pulls, *CONTEXT_BUDGET, provider=provider)

    assert exit_code == 0
    assert len(provider.requests) == 2
    assert totals_line(capsys.readouterr().out).endswith("run=3 spent_usd=1.20")


def test_backfill_without_context_keeps_phase_2s_counts_and_caps(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider(findings=[], cost_usd=Decimal("5"))

    exit_code = backfill(
        tmp_path, repo, merged_pulls(repo, 1), "--max-cost-usd", "0.60", provider=provider
    )

    assert exit_code == 0
    assert provider.requests[0].tools is None
    document = report(tmp_path, 1)
    assert "max_tool_calls" not in document["caps"]
    assert document["over_budget"] is False
    assert totals_line(capsys.readouterr().out) == "backfill: entries=1 already_in_out=0 run=1"


@pytest.mark.parametrize("amount", ["0", "-1", "nan", "inf", "abc"])
def test_backfill_with_an_invalid_max_usd_exits_2_naming_it(
    amount: str, tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockProvider()

    exit_code = backfill(
        tmp_path, repo, merged_pulls(repo, 1), "--max-usd", amount, provider=provider
    )

    assert exit_code == 2
    assert provider.requests == []
    assert "--max-usd" in capsys.readouterr().err
