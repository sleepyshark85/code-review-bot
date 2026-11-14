"""P4-S8: report the rules' findings when every model is down.

Written from the spec's criteria and Interfaces, Phase 4 design.md ("Second provider and routing",
"Merge gating", Report keys) and ADR 0021, in parallel with the developer and before their code
was read.

Every degraded run builds its own providers from the policy, as P4-S7's suite does: nothing
injects a provider, so the routing and the degrading are the commands' own. Each route is a fake
at the outside boundary:
- Claude: the SDK's `query`, replaced by a scripted `FakeSdk` (P4-S7's `use_fake_claude`).
- OpenAI-compatible: P4-S2's scripted `HttpTransport` (`fake_openai.use_fake_transport`).
The runs that review normally, used as controls, are P2-S5's `MockProvider` runs on a twin pull
request (same files, same fixed commit dates, so the same SHAs and fingerprints), or a Claude run
that answers with no findings. Nothing reaches a model, the network or GitHub.

Readings, each used in one place:
- "The rules' findings" are what a reviewed run of the same diff reports when the model finds
  nothing: the same `report/v1` findings, the same comments planned and posted.
- AC2's "the summary starts" is read literally: the summary's text begins with that sentence.
- AC6's findings enter precision through shadow labels on the degraded report's would-be
  comments, built by hand as `pr label` writes them (P3-S13's way), since AC6 is about `metrics`.
- AC7's "`agent` stage has `degraded` with the last category" is one `agent` record that is a
  `degraded` event, or carries a `degraded` key, and whose `category` is the last attempt's.
"""

import importlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from fake_openai import KEY, FakeTransport, connection_failure, fake, http, use_fake_transport
from fake_sdk import FakeSdk
from test_p1_s4_audit_log import read_records
from test_p2_s2_review_pr_event import NUMBER, REPO_NAME
from test_p2_s5_comment_sync import APP, EVAL_LINE, Run, ai, text
from test_p2_s10_metrics import (
    EVENT_AT,
    PRICES,
    cost,
    latency,
    load,
    metrics,
    outcome_counts,
    over_budget,
    precision,
    rewrite,
    split,
    timed,
    tokens,
)
from test_p3_s3_claude_drives_tools import cap_result
from test_p4_s2_openai_compatible_provider import EXIT_CODES, KEY_VARIABLE
from test_p4_s5_gate_decision import (
    AI_LOGIC,
    AI_LOGIC_CLASS,
    MONEY,
    MONEY_CLASS,
    MONEY_CONFIG,
    POLICY_VARIABLE,
    PRICES_FIXED,
    PRICES_V1,
    GatePR,
    blockers,
    gate,
    money_blocker,
    money_fingerprint,
    repo_config,
)
from test_p4_s5_gate_decision import (
    PRICES as PRICES_FILE,
)
from test_p4_s7_fallback_routing import (
    CLAUDE,
    CLAUDE_FAILURES,
    CLAUDE_KEY_VARIABLE,
    OPENAI,
    Router,
    claude_answers,
    claude_fails,
    routing_policy,
    use_fake_claude,
)

from review_bot.errors import ErrorCategory
from review_bot.providers.base import TokenUsage
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

UNAVAILABLE_EXIT = EXIT_CODES[ErrorCategory.UNAVAILABLE]
RULES_ONLY = "rules_only"
DEGRADED_HEADLINE = "Degraded: no model was available, so only the built-in rules ran."
SWALLOWED = "swallowed-exception"
JOBS = "src/jobs.py"
JOBS_V1 = ["def run(job):", "    try:", "        job()", "    except Exception:", "        pass"]
TASKS = "src/tasks.py"
TASKS_V1 = ["def go(task):", "    try:", "        task()", "    except Exception:", "        pass"]
CLEAN = "src/clean.py"
LABELLED_AT = "2026-11-14T09:00:00+00:00"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key, price, cap, policy, CI flag, log path or proxy from the machine running the tests."""
    for name in (
        KEY_VARIABLE,
        CLAUDE_KEY_VARIABLE,
        POLICY_VARIABLE,
        "REVIEW_BOT_GATE",
        "REVIEW_BOT_OPENAI_BASE_URL",
        "REVIEW_BOT_MAX_COST_USD",
        "REVIEW_BOT_PRICE_INPUT_USD_PER_MTOK",
        "REVIEW_BOT_PRICE_OUTPUT_USD_PER_MTOK",
        "REVIEW_BOT_AUDIT_LOG",
        "REVIEW_BOT_THRESHOLD",
        "REVIEW_BOT_MAX_DIFF_BYTES",
        "REVIEW_BOT_MAX_REVIEW_BYTES",
        "REVIEW_BOT_CONTEXT",
        "CI",
        "GITHUB_EVENT_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-11-12T10:00:00+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-11-12T10:00:00+00:00")


# --- The policy -----------------------------------------------------------------------------------


def degrade_policy(*classes: str, degrade: bool | None = True, fallback: bool = True) -> str:
    """Claude, then `openai:m1`, for diff mode; `degrade_to_rules` as given (None leaves it out).

    `classes` are P4-S5's `[[gate.classes]]` entries, appended after `[routing]`.
    """
    text_ = routing_policy(
        (CLAUDE, ("diff",)),
        (OPENAI, ("diff",)),
        fallback=OPENAI if fallback else None,
        version="degrade-1",
    )
    if degrade is not None:
        text_ += "" if fallback else "[routing]\n"
        text_ += f"degrade_to_rules = {'true' if degrade else 'false'}\n"
    return text_ + "".join(classes)


AC1_POLICY = degrade_policy()


def every_route_unavailable() -> tuple[FakeSdk, FakeTransport]:
    """AC1's routes: Claude's CLI is missing, and the fallback's host refuses the connection."""
    return claude_fails(ErrorCategory.UNAVAILABLE), fake(connection_failure("refused"))


# --- A pull request whose runs route --------------------------------------------------------------


class DegradingPR(GatePR):
    """P4-S5's gating pull request, whose policy also routes and may degrade to the rules."""

    def __init__(  # noqa: PLR0913 - keyword-only, one per thing a PR is set up with
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        *,
        config: str | None = None,
        classes: Sequence[str] = (),
        policy: str | None = None,
    ) -> None:
        super().__init__(tmp_path, capsys, monkeypatch, config=config, classes=classes)
        self.policy_path.write_text(policy or degrade_policy(*classes), encoding="utf-8")

    def routed(
        self,
        *,
        claude: FakeSdk,
        transport: FakeTransport | None = None,
        mode: str = "comment",
        key: bool = True,
        flags: Sequence[str] = (),
    ) -> Run:
        """`pr run --provider claude`, building its providers from the policy, as CI's job does."""
        if key:
            self.monkeypatch.setenv(KEY_VARIABLE, KEY)
        else:
            self.monkeypatch.delenv(KEY_VARIABLE, raising=False)
        use_fake_claude(self.monkeypatch, claude)
        use_fake_transport(self.monkeypatch, transport or fake())
        pr_cli = importlib.import_module("review_bot.pr.cli")
        self._runs += 1
        event_path = self._tmp_path / f"event-{self._runs}.json"
        event_path.write_text(json.dumps(self.payload()), encoding="utf-8")
        out = self._tmp_path / f"out-{self._runs}"
        extra = {"github": self.github.client} if mode == "comment" else {}
        calls_before = len(self.github.calls())
        self._capsys.readouterr()
        exit_code = pr_cli.main(
            [
                "run", "--event", str(event_path), "--repo", str(self.repo.path),
                "--out", str(out), "--provider", "claude", "--mode", mode,
                "--max-retries", "0", *flags,
            ],
            **extra,
        )  # fmt: skip
        captured = self._capsys.readouterr()
        return Run(exit_code, captured.err, out, self.github.calls()[calls_before:])

    def degraded(self, *, mode: str = "comment", **kwargs: Any) -> Run:
        """A run in which every route is `unavailable` (AC1)."""
        claude, transport = every_route_unavailable()
        return self.routed(claude=claude, transport=transport, mode=mode, **kwargs)


def make_pr(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> DegradingPR:
    root.mkdir(parents=True, exist_ok=True)
    return DegradingPR(root, capsys, monkeypatch, **kw)


@pytest.fixture
def pr(tmp_path, capsys, monkeypatch) -> DegradingPR:
    return make_pr(tmp_path / "pr", capsys, monkeypatch)


@pytest.fixture
def twin(tmp_path, capsys, monkeypatch) -> DegradingPR:
    """A second PR with the same files, reviewed normally, to compare a degraded run with."""
    return make_pr(tmp_path / "twin", capsys, monkeypatch)


def report(run: Run) -> dict[str, Any]:
    document: dict[str, Any] = run.shadow["report"]
    return document


def findings(run: Run) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = report(run)["findings"]
    return found


def audit(run: Run) -> list[dict[str, Any]]:
    return read_records(run.out / "audit.jsonl")


def rules_records(records: Sequence[dict[str, Any]]) -> list[Any]:
    return [r["data"] for r in records if (r["stage"], r["event"]) == ("static", "rules")]


def degraded_records(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """AC7: the `agent` records saying the run degraded (see the module's readings)."""
    return [
        r["data"]
        for r in records
        if r["stage"] == "agent" and (r["event"] == "degraded" or "degraded" in r["data"])
    ]


def planned(run: Run) -> list[tuple[Any, ...]]:
    return [
        (e["action"], e["fingerprint"], e["rule_part"], e["source"], e["file"], e["line"])
        for e in run.entries
    ]


def push_jobs(*prs: DegradingPR, files: dict[str, str] | None = None) -> None:
    for each in prs:
        each.push(files or {JOBS: text(JOBS_V1)})


# --- AC1: every route unavailable, degrade on: the rules' findings, outcome `degraded` ------------


def test_p4_s8_ac1_shadow_outcome_is_degraded_with_reason_unavailable(pr):
    push_jobs(pr)

    run = pr.degraded(mode="shadow")

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("degraded", "unavailable"), run.stderr


def test_p4_s8_ac1_report_degraded_is_rules_only(pr):
    push_jobs(pr)

    run = pr.degraded(mode="shadow")

    assert report(run)["degraded"] == RULES_ONLY


def test_p4_s8_ac1_exit_code_is_the_providers(pr):
    push_jobs(pr)

    run = pr.degraded(mode="shadow")

    assert run.exit_code == UNAVAILABLE_EXIT, run.stderr


def test_p4_s8_ac1_findings_are_exactly_the_rules_findings(pr, twin):
    push_jobs(pr, twin)

    run = pr.degraded(mode="shadow")
    control, _ = twin.review([], mode="shadow")

    assert control.shadow["outcome"] == "reviewed", "precondition"
    assert findings(run) == findings(control)
    assert [(f["source"], f["rule"], f["file"]) for f in findings(run)] == [
        ("rule", SWALLOWED, JOBS)
    ]


def test_p4_s8_ac1_both_routes_were_tried(pr):
    push_jobs(pr)
    claude, transport = every_route_unavailable()

    run = pr.routed(claude=claude, transport=transport, mode="shadow")

    assert len(claude.calls) == 1
    assert len(transport.sent) == 1
    assert [a["route"] for a in report(run)["route"]] == [CLAUDE, OPENAI]


def test_p4_s8_ac1_without_a_fallback_the_lone_unavailable_route_degrades(
    tmp_path, capsys, monkeypatch
):
    """ "Every route" is one route when the policy names no fallback."""
    pr = make_pr(tmp_path, capsys, monkeypatch, policy=degrade_policy(fallback=False))
    push_jobs(pr)

    run = pr.routed(claude=claude_fails(ErrorCategory.UNAVAILABLE), mode="shadow")

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("degraded", "unavailable"), run.stderr
    assert run.exit_code == UNAVAILABLE_EXIT


@pytest.mark.parametrize(
    ("claude_category", "reply", "last"),
    [
        pytest.param(
            ErrorCategory.UNAVAILABLE, http(503), ErrorCategory.TRANSIENT, id="then_transient"
        ),
        pytest.param(
            ErrorCategory.TRANSIENT, http(429), ErrorCategory.RATE_LIMIT, id="then_rate_limit"
        ),
        pytest.param(
            ErrorCategory.RATE_LIMIT,
            connection_failure("refused"),
            ErrorCategory.UNAVAILABLE,
            id="rate_limit_then_unavailable",
        ),
    ],
)
def test_p4_s8_ac1_any_routable_last_failure_degrades_with_its_category_and_exit_code(
    pr, claude_category, reply, last
):
    push_jobs(pr)

    run = pr.routed(claude=claude_fails(claude_category), transport=fake(reply), mode="shadow")

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("degraded", str(last)), run.stderr
    assert run.exit_code == EXIT_CODES[last]


def test_p4_s8_ac1_a_diff_without_rule_findings_degrades_to_no_findings(pr):
    pr.push({CLEAN: "value = 1\n"})

    run = pr.degraded(mode="shadow")

    assert run.shadow["outcome"] == "degraded", run.stderr
    assert findings(run) == []
    assert run.exit_code == UNAVAILABLE_EXIT


# --- AC2: comment mode posts the rule findings and keeps `reviewed=` ------------------------------


def test_p4_s8_ac2_rule_findings_are_planned_and_posted_as_in_a_reviewed_run(pr, twin):
    push_jobs(pr, twin)

    run = pr.degraded()
    control, _ = twin.review([])

    assert control.shadow["outcome"] == "reviewed", "precondition"
    assert planned(run) == planned(control)
    assert [action for action, *_ in planned(run)] == ["create"]
    assert run.posted == control.posted


def test_p4_s8_ac2_summary_starts_with_the_degraded_sentence(pr):
    push_jobs(pr)

    pr.degraded()

    assert pr.summary().startswith(DEGRADED_HEADLINE), pr.summary()[:300]


def test_p4_s8_ac2_first_run_keeps_reviewed_none(pr):
    push_jobs(pr)

    pr.degraded()

    assert pr.marker()["reviewed"] == "none"


def test_p4_s8_ac2_summary_keeps_the_previous_reviewed_sha(pr):
    pr.push({CLEAN: "value = 1\n"})
    first, _ = pr.review([])
    assert first.shadow["outcome"] == "reviewed", "precondition"
    reviewed = pr.head
    push_jobs(pr)

    pr.degraded()

    assert pr.marker()["reviewed"] == reviewed
    assert pr.marker()["reviewed"] != pr.head, "the merge gate must see this head as not reviewed"


def test_p4_s8_ac2_next_run_reviews_from_the_kept_sha(pr):
    """The degraded push's change reaches the model on the next run, which starts from `A`."""
    pr.push({CLEAN: "value = 1\n"})
    pr.review([])
    since = pr.head
    push_jobs(pr)
    pr.degraded()
    pr.push({"src/later.py": "later = 2\n"})

    run, provider = pr.review([])

    assert (run.shadow["scope"], run.shadow["since"]) == ("incremental", since)
    sent = "\n".join(request.user_prompt for request in provider.requests)
    assert JOBS_V1[3].strip() in sent, "the degraded push's change is reviewed by the model"
    assert pr.marker()["reviewed"] == pr.head


def test_p4_s8_ac2_summary_is_written_with_gating_on_too(tmp_path, capsys, monkeypatch):
    pr = make_pr(tmp_path, capsys, monkeypatch, config=MONEY_CONFIG, classes=(MONEY_CLASS,))
    push_jobs(pr)

    run = pr.degraded()

    assert "write_summary" in run.writes
    assert pr.summary().startswith(DEGRADED_HEADLINE), pr.summary()[:300]
    assert pr.marker()["reviewed"] == "none"


# --- AC3: degrade off, or a last failure that isn't routable: fails as today ----------------------


@pytest.mark.parametrize(
    "policy",
    [degrade_policy(degrade=False), degrade_policy(degrade=None)],
    ids=["degrade_false", "key_left_out"],
)
def test_p4_s8_ac3_degrade_off_fails_as_today(tmp_path, capsys, monkeypatch, policy):
    pr = make_pr(tmp_path, capsys, monkeypatch, policy=policy)
    push_jobs(pr)

    run = pr.degraded(mode="shadow")

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", "unavailable"), run.stderr
    assert run.exit_code == UNAVAILABLE_EXIT


def test_p4_s8_ac3_builtin_policy_fails_as_today(pr):
    pr.monkeypatch.delenv(POLICY_VARIABLE)
    push_jobs(pr)

    run = pr.routed(claude=claude_fails(ErrorCategory.UNAVAILABLE), mode="shadow")

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", "unavailable"), run.stderr
    assert run.exit_code == UNAVAILABLE_EXIT


def test_p4_s8_ac3_degrade_off_comment_summary_is_the_failed_one(tmp_path, capsys, monkeypatch):
    pr = make_pr(tmp_path, capsys, monkeypatch, policy=degrade_policy(degrade=False))
    push_jobs(pr)

    run = pr.degraded()

    assert run.posted == []
    assert not pr.summary().startswith(DEGRADED_HEADLINE)
    assert "`failed`" in pr.summary()


NOT_ROUTABLE = [
    ErrorCategory.AUTH,
    ErrorCategory.REFUSAL,
    ErrorCategory.INVALID_OUTPUT,
    ErrorCategory.PERMANENT,
]


@pytest.mark.parametrize("category", NOT_ROUTABLE, ids=str)
def test_p4_s8_ac3_unroutable_primary_failure_fails_with_degrade_on(pr, category):
    push_jobs(pr)

    run = pr.routed(claude=FakeSdk(CLAUDE_FAILURES[category]), transport=fake(), mode="shadow")

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", str(category)), run.stderr
    assert run.exit_code == EXIT_CODES[category]


@pytest.mark.parametrize(
    ("reply", "category"),
    [
        pytest.param(http(401), ErrorCategory.AUTH, id="auth"),
        pytest.param(http(400), ErrorCategory.PERMANENT, id="permanent"),
    ],
)
def test_p4_s8_ac3_unroutable_last_failure_after_an_unavailable_primary_fails(pr, reply, category):
    """Only the last failure counts: an `unavailable` primary doesn't make the run degrade."""
    push_jobs(pr)

    run = pr.routed(
        claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=fake(reply), mode="shadow"
    )

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", str(category)), run.stderr
    assert run.exit_code == EXIT_CODES[category]


def test_p4_s8_ac3_a_cost_cap_failure_fails_with_degrade_on(tmp_path, capsys, monkeypatch):
    pr = make_pr(tmp_path, capsys, monkeypatch, policy=degrade_policy(fallback=False))
    push_jobs(pr)

    run = pr.routed(
        claude=FakeSdk(cap_result("error_max_budget_usd")),
        mode="shadow",
        flags=("--max-cost-usd", "0.60"),
    )

    assert run.shadow["outcome"] == "failed", run.stderr
    assert run.exit_code == EXIT_CODES[ErrorCategory.PERMANENT]


# --- AC4: the gate on a degraded run: `block` on a rule blocker, else `unjudged` ------------------


def money_pr(tmp_path, capsys, monkeypatch, **kw: Any) -> DegradingPR:
    return make_pr(tmp_path, capsys, monkeypatch, config=MONEY_CONFIG, classes=(MONEY_CLASS,), **kw)


def test_p4_s8_ac4_money_rule_finding_blocks_a_degraded_run(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    push_jobs(pr, files={JOBS: text(JOBS_V1), PRICES_FILE: text(PRICES_V1)})

    run = pr.degraded()

    assert run.shadow["outcome"] == "degraded", run.stderr
    assert gate(run)["decision"] == "block"
    assert blockers(run) == [money_blocker()]


def test_p4_s8_ac4_marker_lists_the_money_blocker_and_keeps_reviewed(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({CLEAN: "value = 1\n"})
    pr.review([])
    reviewed = pr.head
    push_jobs(pr, files={JOBS: text(JOBS_V1), PRICES_FILE: text(PRICES_V1)})

    pr.degraded()

    marker = pr.marker()
    assert marker["gate"] == [(money_fingerprint(), MONEY)]
    assert marker["reviewed"] == reviewed


def test_p4_s8_ac4_no_blocker_is_unjudged_never_pass(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    push_jobs(pr, files={JOBS: text(JOBS_V1), PRICES_FILE: text(PRICES_FIXED)})

    run = pr.degraded()

    assert run.shadow["outcome"] == "degraded", run.stderr
    assert (gate(run)["decision"], blockers(run)) == ("unjudged", [])
    assert pr.marker()["gate"] == []


def test_p4_s8_ac4_a_carried_ai_blocker_still_blocks_a_degraded_run(tmp_path, capsys, monkeypatch):
    config = repo_config(AI_LOGIC, domain_rule=None)
    pr = make_pr(tmp_path, capsys, monkeypatch, config=config, classes=(AI_LOGIC_CLASS,))
    pr.push({APP: text(["def load(path):", "    data = read(path)", "    value = eval(data)"])})
    first, _ = pr.review([ai(EVAL_LINE, severity="critical")])
    assert gate(first)["decision"] == "block", "precondition"
    carried = [b["fingerprint"] for b in blockers(first)]
    push_jobs(pr)

    run = pr.degraded()

    assert run.shadow["outcome"] == "degraded", run.stderr
    assert gate(run)["decision"] == "block"
    assert [b["fingerprint"] for b in blockers(run)] == carried
    assert [fp for fp, _ in pr.marker()["gate"]] == carried


def test_p4_s8_ac4_shadow_mode_would_block_on_the_money_finding(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    push_jobs(pr, files={JOBS: text(JOBS_V1), PRICES_FILE: text(PRICES_V1)})

    run = pr.degraded(mode="shadow")

    assert (gate(run)["decision"], blockers(run)) == ("block", [money_blocker()])


# --- AC5: `review-bot DIFF` degrades too ----------------------------------------------------------


@pytest.fixture
def router(bot, write_diff, tmp_path, monkeypatch) -> Router:
    return Router(bot, write_diff, tmp_path, monkeypatch)


def diff_degraded(router: Router, sample_diff: Path, *flags: str) -> Any:
    claude, transport = every_route_unavailable()
    return router.review(
        AC1_POLICY, *flags, claude=claude, transport=transport, diff_path=sample_diff
    )


def diff_reviewed(router: Router, sample_diff: Path, *flags: str) -> Any:
    """The control: Claude answers with no findings, so only the rules report."""
    return router.review(AC1_POLICY, *flags, claude=claude_answers(), diff_path=sample_diff)


def test_p4_s8_ac5_json_report_is_rules_only_and_exit_is_the_providers(router, sample_diff):
    done = diff_degraded(router, sample_diff)

    assert done.exit_code == UNAVAILABLE_EXIT, done.stderr
    assert done.report["degraded"] == RULES_ONLY


def test_p4_s8_ac5_json_findings_are_the_rules_findings(router, sample_diff):
    degraded = diff_degraded(router, sample_diff)
    control = diff_reviewed(router, sample_diff)

    assert degraded.report["findings"] == control.report["findings"]
    assert {f["source"] for f in degraded.report["findings"]} == {"rule"}
    assert SWALLOWED in {f["rule"] for f in degraded.report["findings"]}


def test_p4_s8_ac5_text_output_prints_the_rule_findings_as_usual(router, sample_diff):
    degraded = diff_degraded(router, sample_diff, "--format", "text")
    control = diff_reviewed(router, sample_diff, "--format", "text")

    assert degraded.exit_code == UNAVAILABLE_EXIT, degraded.stderr
    assert degraded.stdout == control.stdout
    assert degraded.stdout.strip(), "the rule findings print"


def test_p4_s8_ac5_stderr_says_the_review_is_degraded_and_why(router, sample_diff):
    done = diff_degraded(router, sample_diff)

    assert "degraded" in done.stderr.lower(), done.stderr
    assert "unavailable" in done.stderr, done.stderr


def test_p4_s8_ac5_degraded_after_a_transient_fallback_exits_transient(router, sample_diff):
    done = router.review(
        AC1_POLICY,
        claude=claude_fails(ErrorCategory.UNAVAILABLE),
        transport=fake(http(503)),
        diff_path=sample_diff,
    )

    assert done.exit_code == EXIT_CODES[ErrorCategory.TRANSIENT], done.stderr
    assert done.report["degraded"] == RULES_ONLY
    assert "transient" in done.stderr


def test_p4_s8_ac5_degrade_off_prints_no_report(router, sample_diff):
    """AC3 in diff mode: a failed run writes no report, as today."""
    claude, transport = every_route_unavailable()

    done = router.review(
        degrade_policy(degrade=False), claude=claude, transport=transport, diff_path=sample_diff
    )

    done.fails_as(ErrorCategory.UNAVAILABLE)
    assert done.stdout == ""


# --- Interfaces: `report/v1` `degraded` is null or "rules_only", appended after `route` -----------


def test_p4_s8_interfaces_reviewed_report_has_degraded_null(router, sample_diff):
    done = diff_reviewed(router, sample_diff)

    assert done.exit_code == 1, done.stderr  # the sample's rule findings
    assert done.report["degraded"] is None


def test_p4_s8_interfaces_degraded_is_appended_after_route(router, sample_diff):
    keys = tuple(diff_degraded(router, sample_diff).report)

    assert keys.index("degraded") > keys.index("route"), keys


def test_p4_s8_interfaces_injected_mock_report_has_degraded_null(bot, sample_diff):
    run = bot.review(sample_diff, "--format", "json", provider=MockProvider(findings=[]))

    assert json.loads(run.stdout)["degraded"] is None


# --- AC6: `pr metrics` counts `degraded` apart, out of latency, tokens and cost -------------------


def shadow_label(document: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """An `accept` shadow label on `entry`, as `pr label` writes it (P2-S4 AC10)."""
    return {
        "schema": "feedback/v1",
        "channel": "shadow",
        "repo": REPO_NAME,
        "pr": NUMBER,
        "fingerprint": entry["fingerprint"],
        "file": entry["file"],
        "rule_part": entry["rule_part"],
        "anchor": entry["anchor"],
        "index": 0,
        "source": entry["source"],
        "severity": entry["severity"],
        "label": "accept",
        "reason": None,
        "run_id": document["run_id"],
        "model": document["report"]["model"],
        "prompt_version": document["report"]["prompt_version"],
        "recorded_at": LABELLED_AT,
    }


def collect_report(run: Run, reports: Path, name: str) -> Path:
    target = reports / name / "shadow.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(run.out / "shadow.json", target)
    return target


@pytest.fixture
def two_reports(tmp_path, capsys, monkeypatch) -> tuple[Path, Path]:
    """One reviewed run (1,000 in, 100 out, 60 s end to end) and one degraded run of a twin PR.

    The degraded report is given usage and slow times no degraded run would have, so that any of
    it reaching latency, tokens or cost shows.
    """
    reports = tmp_path / "reports"
    reviewed_pr = make_pr(tmp_path / "a", capsys, monkeypatch)
    degraded_pr = make_pr(tmp_path / "b", capsys, monkeypatch)
    push_jobs(reviewed_pr, degraded_pr)
    reviewed_run, _ = reviewed_pr.review(
        provider=MockProvider(findings=[], usage=TokenUsage(1_000, 100)), mode="shadow"
    )
    degraded_run = degraded_pr.degraded(mode="shadow")
    assert degraded_run.shadow["outcome"] == "degraded", degraded_run.stderr
    reviewed = collect_report(reviewed_run, reports, "reviewed")
    degraded = collect_report(degraded_run, reports, "degraded")
    rewrite(reviewed, timed(event_at=EVENT_AT, review_s=10, total_s=60))
    rewrite(degraded, timed(event_at=EVENT_AT, review_s=5_000, total_s=9_000))
    rewrite(degraded, _with_usage(load(reviewed)["report"]["usage"], factor=50))
    return reports, degraded


def _with_usage(usage: dict[str, Any], *, factor: int) -> Any:
    scaled = {k: v * factor if isinstance(v, int) else v for k, v in usage.items()}

    def change(document: dict[str, Any]) -> None:
        document["report"]["usage"] = scaled

    return change


def test_p4_s8_ac6_degraded_is_counted_as_its_own_outcome(two_reports, capsys):
    reports, _ = two_reports

    counts = outcome_counts(metrics(capsys, reports).doc)

    assert counts == {
        ("shadow", "event", "reviewed", None): 1,
        ("shadow", "event", "degraded", "unavailable"): 1,
    }


def test_p4_s8_ac6_text_names_the_degraded_outcome(two_reports, capsys):
    reports, _ = two_reports

    result = metrics(capsys, reports, json_format=False)

    assert result.exit_code == 0, result.stderr
    assert "degraded" in result.stdout, result.stdout


def test_p4_s8_ac6_degraded_run_is_left_out_of_latency(two_reports, capsys):
    reports, _ = two_reports

    doc = metrics(capsys, reports).doc

    assert latency(doc, "end_to_end") == (60, 60)
    assert latency(doc, "review") == (10, 10)


def test_p4_s8_ac6_degraded_run_is_left_out_of_tokens(two_reports, capsys):
    reports, _ = two_reports

    doc = metrics(capsys, reports).doc

    assert tokens(doc, "input") == (1_000, 1_000, 1_000)
    assert tokens(doc, "output") == (100, 100, 100)
    assert over_budget(doc) == 0


def test_p4_s8_ac6_degraded_run_is_left_out_of_cost(two_reports, capsys):
    """1,000 in at $15 and 100 out at $75 per million: $0.0225, so 0.02 half-even."""
    reports, _ = two_reports

    doc = metrics(capsys, reports, flags=PRICES).doc

    priced = cost(doc)
    assert priced is not None
    assert priced[0] == "0.02"


def test_p4_s8_ac6_degraded_findings_count_in_precision_by_source_rule(
    tmp_path, capsys, monkeypatch
):
    pr = make_pr(tmp_path / "pr", capsys, monkeypatch)
    push_jobs(pr, files={JOBS: text(JOBS_V1), TASKS: text(TASKS_V1)})
    run = pr.degraded(mode="shadow")
    assert run.shadow["outcome"] == "degraded", run.stderr
    entries = run.entries
    assert len(entries) == 2, "precondition: one would-be comment per swallowed exception"
    reports = tmp_path / "reports"
    collect_report(run, reports, "degraded")
    labels = tmp_path / "feedback.jsonl"
    records = [shadow_label(run.shadow, entry) for entry in entries]
    labels.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    result = metrics(capsys, reports, labels)

    shadow = precision(result.doc, "shadow")
    assert (shadow["accepted"], shadow["judged"], shadow["units"]) == (2, 2, 2), shadow
    assert split(shadow, "source")["rule"]["accepted"] == 2
    assert result.stderr_lines("orphan") == []


# --- AC7: the audit log says the run degraded, and the rules' records are as usual ----------------


def test_p4_s8_ac7_pr_run_audit_agent_stage_has_degraded_with_the_category(pr):
    push_jobs(pr)

    run = pr.degraded(mode="shadow")

    [record] = degraded_records(audit(run))
    assert record["category"] == "unavailable", record


def test_p4_s8_ac7_degraded_record_holds_the_last_category_not_the_first(pr):
    push_jobs(pr)

    run = pr.routed(
        claude=claude_fails(ErrorCategory.UNAVAILABLE), transport=fake(http(503)), mode="shadow"
    )

    [record] = degraded_records(audit(run))
    assert record["category"] == "transient", record


def test_p4_s8_ac7_rules_records_are_as_in_a_reviewed_run(pr, twin):
    push_jobs(pr, twin)

    run = pr.degraded(mode="shadow")
    control, _ = twin.review([], mode="shadow")

    assert rules_records(audit(run)) == rules_records(audit(control))
    assert rules_records(audit(run)), "the rules ran"


def test_p4_s8_ac7_diff_mode_audit_has_degraded_and_the_rules_records(router, sample_diff):
    degraded = diff_degraded(router, sample_diff)
    control = diff_reviewed(router, sample_diff)

    [record] = degraded_records(degraded.records)
    assert record["category"] == "unavailable", record
    assert rules_records(degraded.records) == rules_records(control.records)
    assert rules_records(degraded.records)


def test_p4_s8_ac7_a_reviewed_run_has_no_degraded_record(router, sample_diff):
    assert degraded_records(diff_reviewed(router, sample_diff).records) == []


def test_p4_s8_ac7_a_failed_run_has_no_degraded_record(router, sample_diff):
    claude, transport = every_route_unavailable()

    done = router.review(
        degrade_policy(degrade=False), claude=claude, transport=transport, diff_path=sample_diff
    )

    assert degraded_records(done.records) == []


def test_p4_s8_ac7_no_record_holds_diff_lines(pr):
    """As before (design.md, Report keys): no record holds diff lines or model text."""
    push_jobs(pr)

    run = pr.degraded(mode="shadow")

    log = (run.out / "audit.jsonl").read_text(encoding="ascii")
    assert "job()" not in log
