"""A pull request run with merge gating (P4-S5): its decision, marker, summary and audit."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from audit_records import read_records
from git_samples import commit, init_repo

from review_bot.audit import AuditLog
from review_bot.findings import Finding
from review_bot.git import GitRepo
from review_bot.governance.gate import GateDecision, GateReason
from review_bot.governance.policy import BUILTIN_POLICY, Policy, parse_policy
from review_bot.pr.comments import read_summary_marker
from review_bot.pr.event import PullRequestRef
from review_bot.pr.fake_github import FakeGitHubClient
from review_bot.pr.outcome import Outcome, Reason
from review_bot.pr.runner import run_pull_request
from review_bot.pr.shadow import Mode, ShadowReport, render_shadow_json
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
GITHUB_TOKEN = "gh" + "p_" + "a1B2" * 9
POLICY = parse_policy(
    b'version = "gated"\n'
    b'[[gate.classes]]\nrule = "decimal-money"\nmin_severity = "high"\n'
    b'approved_by = "@owner"\napproved_on = 2026-12-01\n'
    b'[[gate.classes]]\nrule = "ai:security"\nmin_severity = "high"\n'
    b'approved_by = "@owner"\napproved_on = 2026-12-01\n'
    b'[[gate.classes]]\nrule = "possible-secret"\n'
    b'approved_by = "@owner"\napproved_on = 2026-12-01\n',
    source="policy.toml",
)
MONEY_CONFIG = (
    'gate = ["decimal-money", "ai:security", "possible-secret"]\n\n'
    '[[domain_rules]]\nrule = "decimal-money"\npaths = ["billing/**"]\n'
)
PRICES = "from decimal import Decimal\n\ntotal = 9.99\n"
FIXED = 'from decimal import Decimal\n\ntotal = Decimal("9.99")\n'


class PullRequest:
    """A pull request on a local clone, pushed to and run on with one fake GitHub."""

    def __init__(self, tmp_path: Path, config: str = MONEY_CONFIG) -> None:
        self.repo = init_repo(tmp_path / "repo")
        self.base = commit(self.repo, {".review-bot.toml": config, "README.md": "Read me.\n"})
        self.head = self.base
        self.github = FakeGitHubClient()
        self.audit = AuditLog(tmp_path / "audit.jsonl", clock=lambda: NOW)

    def push(self, files: dict[str, str]) -> None:
        self.head = commit(self.repo, files)

    @property
    def ref(self) -> PullRequestRef:
        return PullRequestRef(
            repo="owner/repo",
            number=7,
            base_sha=self.base,
            head_sha=self.head,
            is_fork=False,
            is_draft=False,
            event_at=NOW,
            action="synchronize",
        )

    def run(
        self,
        *findings: Finding,
        mode: Mode = Mode.COMMENT,
        policy: Policy = POLICY,
        settings: Settings | None = None,
        gating_on: bool = False,
        provider: MockProvider | None = None,
    ) -> ShadowReport:
        return asyncio.run(
            run_pull_request(
                self.ref,
                mode=mode,
                git=GitRepo(self.repo, timeout_s=60),
                make_provider=lambda: provider or MockProvider(findings=list(findings)),
                github=self.github if mode is Mode.COMMENT else None,
                issue_reader=None,
                settings=settings or Settings(provider="mock"),
                policy=policy,
                audit=self.audit,
                environ={},
                clock=lambda: NOW,
                gating_on=gating_on,
            )
        )

    def marker(self) -> str:
        assert self.github.summary is not None
        return self.github.summary.rsplit("\n", maxsplit=1)[-1]

    def gate_records(self) -> list[dict[str, Any]]:
        return [
            record["data"]
            for record in read_records(self.audit.path)
            if (record["stage"], record["event"]) == ("policy", "gate")
        ]


def gate_json(shadow: ShadowReport) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(render_shadow_json(shadow))
    return document["gate"]


def security_finding(line: int = 1, *, severity: str = "critical") -> Finding:
    return Finding(
        file="app.py",
        line=line,
        severity=severity,
        category="security",
        rationale="Unsafe.",
        confidence=0.95,
    )


@pytest.fixture
def pr(tmp_path: Path) -> PullRequest:
    return PullRequest(tmp_path)


def test_a_shadow_run_reports_what_would_block_and_records_it(pr: PullRequest) -> None:
    pr.push({"billing/prices.py": PRICES})

    shadow = pr.run(mode=Mode.SHADOW)

    gate = gate_json(shadow)
    (blocker,) = gate["blocking"]
    assert (gate["enabled"], gate["decision"], gate["reason"]) == (True, "block", None)
    assert gate["classes"] == ["decimal-money", "ai:security", "possible-secret"]
    assert {key: blocker[key] for key in ("class", "file", "line", "severity")} == {
        "class": "decimal-money",
        "file": "billing/prices.py",
        "line": 3,
        "severity": "high",
    }
    assert "Would block the merge: 1 finding (decimal-money)" in shadow.summary_body
    assert pr.gate_records() == [
        {
            "enabled": True,
            "decision": "block",
            "reason": None,
            "classes": ["decimal-money", "ai:security", "possible-secret"],
            "fingerprints": [blocker["fingerprint"]],
            "policy_version": "gated",
        }
    ]


def test_a_class_the_policy_doesnt_approve_fails_the_run_as_invalid_config(
    pr: PullRequest,
) -> None:
    pr.push({"billing/prices.py": PRICES})

    shadow = pr.run(mode=Mode.SHADOW, policy=BUILTIN_POLICY)

    assert (shadow.outcome, shadow.reason) == (Outcome.FAILED, Reason.INVALID_CONFIG)
    assert "decimal-money" in str(shadow.error)
    assert "builtin-v1" in str(shadow.error)
    assert gate_json(shadow) == {
        "enabled": None,
        "decision": "unjudged",
        "reason": "no_config",
        "classes": [],
        "blocking": [],
    }


def test_a_rule_blocker_holds_across_pushes_until_the_line_is_fixed(pr: PullRequest) -> None:
    pr.push({"billing/prices.py": PRICES})
    first = pr.run()
    (posted,) = pr.github.comments
    fingerprint = first.gate.blocking[0].fingerprint
    assert fingerprint in posted.body
    assert f"gate={fingerprint}:decimal-money" in pr.marker()

    pr.push({"README.md": "Read me again.\n"})
    second = pr.run()

    assert second.gate.decision is GateDecision.BLOCK
    assert [blocker.fingerprint for blocker in second.gate.blocking] == [fingerprint]

    pr.push({"billing/prices.py": FIXED})
    third = pr.run()

    assert (third.outcome, third.gate.decision, third.gate.blocking) == (
        Outcome.REVIEWED,
        GateDecision.PASS,
        (),
    )
    assert "gate=" not in pr.marker()


def test_a_secret_in_a_hunk_the_incremental_run_doesnt_review_still_blocks(
    pr: PullRequest,
) -> None:
    pr.push({"config.py": f"KEY = '{GITHUB_TOKEN}'\n"})
    assert pr.run().outcome is Outcome.BLOCKED_SECRET
    pr.push({"app.py": "x = 1\n"})

    shadow = pr.run()

    assert shadow.gate.decision is GateDecision.BLOCK
    assert [(b.gate_class, b.file, b.line) for b in shadow.gate.blocking] == [
        ("possible-secret", "config.py", 1)
    ]
    assert GITHUB_TOKEN not in render_shadow_json(shadow)
    assert GITHUB_TOKEN not in (pr.github.summary or "")


def test_an_ai_blocker_carries_from_a_trusted_marker_when_nothing_new_is_reviewed(
    pr: PullRequest,
) -> None:
    pr.push({"app.py": "x = 1\n"})
    first = pr.run(security_finding())
    assert first.gate.decision is GateDecision.BLOCK
    pr.push({"README.md": "Read me again.\n"})

    second = pr.run()

    assert second.gate.decision is GateDecision.BLOCK
    assert [b.fingerprint for b in second.gate.blocking] == [first.gate.blocking[0].fingerprint]


def test_a_run_that_cant_check_the_whole_diff_carries_every_trusted_entry_and_keeps_reviewed(
    pr: PullRequest,
) -> None:
    pr.push({"billing/prices.py": PRICES, "app.py": "x = 1\n"})
    first = pr.run(security_finding())
    reviewed_marker = pr.marker()
    pr.push({"big.py": "y = 2\n" * 400})

    shadow = pr.run(settings=Settings(provider="mock", max_diff_bytes=1_000))

    assert (shadow.outcome, shadow.reason) == (Outcome.SKIPPED, Reason.TOO_LARGE)
    assert shadow.gate.decision is GateDecision.BLOCK
    assert {b.fingerprint for b in shadow.gate.blocking} == {
        b.fingerprint for b in first.gate.blocking
    }
    assert pr.marker() == reviewed_marker


def test_a_skipped_run_with_no_blocker_is_unjudged_and_still_writes_the_summary(
    tmp_path: Path,
) -> None:
    pr = PullRequest(tmp_path)
    pr.push({"app.py": "x = 1\n"})
    pr.run()
    pr.push({"big.py": "y = 2\n" * 400})
    writes = len([call for call in pr.github.calls if call.name == "write_summary"])

    shadow = pr.run(settings=Settings(provider="mock", max_diff_bytes=1_000))

    assert shadow.gate.decision is GateDecision.UNJUDGED
    assert len([call for call in pr.github.calls if call.name == "write_summary"]) == writes + 1


def test_an_already_reviewed_head_keeps_its_body_and_rewrites_only_the_gate(
    pr: PullRequest,
) -> None:
    pr.push({"billing/prices.py": PRICES})
    pr.run()
    body = pr.github.summary

    shadow = pr.run()

    assert shadow.reason is Reason.ALREADY_REVIEWED
    assert shadow.gate.decision is GateDecision.BLOCK
    assert pr.github.summary == body


def test_an_edited_summary_rebuilds_ai_blockers_from_the_bots_comments(pr: PullRequest) -> None:
    pr.push({"app.py": "x = 1\n"})
    first = pr.run(security_finding())
    pr.github.edit_summary("All clear.", by="mallory")

    shadow = pr.run()

    assert shadow.since is None
    assert shadow.gate.rebuilt
    assert (shadow.gate.decision, shadow.gate.reason) == (GateDecision.BLOCK, None)
    assert [b.fingerprint for b in shadow.gate.blocking] == [first.gate.blocking[0].fingerprint]
    assert "A tamper occurred" in shadow.summary_body
    assert " tampered" not in pr.marker()


def test_an_inline_comment_someone_else_edited_keeps_the_run_tampered(pr: PullRequest) -> None:
    pr.push({"app.py": "x = 1\n"})
    pr.run(security_finding())
    (posted,) = pr.github.comments
    pr.github.edit_comment(posted.comment_id, posted.body, by="mallory")
    pr.push({"README.md": "Read me again.\n"})

    first = pr.run()
    pr.push({"README.md": "Read me once more.\n"})
    second = pr.run()

    for shadow in (first, second):
        assert (shadow.gate.decision, shadow.gate.reason) == (
            GateDecision.BLOCK,
            GateReason.TAMPERED,
        )
        assert len(shadow.gate.blocking) == 1
    marker = read_summary_marker(pr.github.summary or "")
    assert marker is not None
    assert marker.tampered


@pytest.mark.parametrize("gating_on", [True, False])
def test_an_invalid_config_while_gating_may_be_on_edits_no_summary(
    tmp_path: Path, gating_on: bool
) -> None:
    pr = PullRequest(tmp_path)
    pr.push({"billing/prices.py": PRICES})
    pr.run()
    gated_summary = pr.github.summary
    pr.base = commit(pr.repo, {".review-bot.toml": "unknown = 1\n"})
    pr.push({"app.py": "x = 1\n"})

    shadow = pr.run(gating_on=gating_on)

    assert shadow.reason is Reason.INVALID_CONFIG
    assert shadow.gate.reason is GateReason.NO_CONFIG
    assert pr.github.summary == gated_summary
    assert pr.github.calls[-1].name == "read_summary"


def test_an_invalid_config_with_gating_off_writes_as_phase_3(tmp_path: Path) -> None:
    pr = PullRequest(tmp_path, config="unknown = 1\n")
    pr.push({"app.py": "x = 1\n"})

    pr.run()

    assert [call.name for call in pr.github.calls] == ["read_summary", "write_summary"]


def test_a_run_with_gating_on_but_invalid_config_and_no_summary_creates_none(
    tmp_path: Path,
) -> None:
    pr = PullRequest(tmp_path, config="unknown = 1\n")
    pr.push({"app.py": "x = 1\n"})

    pr.run(gating_on=True)

    assert pr.github.summary is None


def test_a_withheld_blocker_still_blocks_and_is_named_by_place(tmp_path: Path) -> None:
    pr = PullRequest(tmp_path, config=f"max_open_comments = 1\n{MONEY_CONFIG}")
    pr.push({"app.py": "x = 1\ny = 2\n"})

    shadow = pr.run(security_finding(1), security_finding(2))

    (withheld,) = shadow.withheld
    assert withheld.reviewed.finding.line == 2
    assert [b.line for b in shadow.gate.blocking] == [1, 2]
    fingerprint = withheld.reviewed.fingerprint
    assert f"- `app.py:2`: ai:security, fingerprint `{fingerprint}`" in shadow.summary_body


def test_a_withheld_blocker_sharing_an_old_resolved_comments_fingerprint_keeps_blocking(
    tmp_path: Path,
) -> None:
    pr = PullRequest(tmp_path, config=f"max_open_comments = 1\n{MONEY_CONFIG}")
    pr.push({"app.py": "x = eval(y)\n"})
    first = pr.run(security_finding(1))
    (fingerprint,) = [blocker.fingerprint for blocker in first.gate.blocking]
    pr.push({"app.py": "x = 1\n"})
    second = pr.run()
    assert [resolve.comment.fingerprint for resolve in second.resolves] == [fingerprint]
    pr.push({"app.py": "a = 0\nx = eval(y)\n"})
    third = pr.run(security_finding(1), security_finding(2))
    assert [item.reviewed.fingerprint for item in third.withheld] == [fingerprint]
    pr.push({"README.md": "Read me again.\n"})

    fourth = pr.run()

    assert fourth.gate.decision is GateDecision.BLOCK
    assert fingerprint in [blocker.fingerprint for blocker in fourth.gate.blocking]


def test_a_summary_whose_marker_was_edited_away_is_a_tamper_and_rebuilds(pr: PullRequest) -> None:
    pr.push({"app.py": "x = 1\n"})
    first = pr.run(security_finding())
    pr.github.edit_summary("All clear.", by="mallory")

    shadow = pr.run()

    assert shadow.since is None
    assert shadow.gate.rebuilt
    assert [b.fingerprint for b in shadow.gate.blocking] == [first.gate.blocking[0].fingerprint]
    assert "A tamper occurred" in shadow.summary_body
