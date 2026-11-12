r"""P4-S5: see which findings would block a merge, per approved class.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md (Merge gating, Summary
marker, Blocker fingerprints) and ADR 0019, in parallel with the developer and before their code
was read.

Each test is a pull request with one or more pushes, as P2-S5's and P2-S7's suites drive them:
a real git repository in `tmp_path` (`.review-bot.toml` in the base commit), one
`FakeGitHubClient` for the whole PR, and in-process `review_bot.pr.cli.main(["run", ...],
provider=MockProvider(...), github=FAKE)` runs. The policy comes from `REVIEW_BOT_POLICY`
(P4-S1). Nothing reaches a model, the network or GitHub. Between pushes the test plays the
people on the PR through `People`, the one place that knows the fake's names for editing or
deleting a comment as someone else.

AC2's goldens (`tests/fixtures/p4_s5/phase_3_story_*.json`) were recorded on this branch at
3c5b63d, before any of this slice's code existed in `src/`: the GitHub calls, exit codes and
summaries of Phase 3 for one PR's nine runs. A run with gating off must still make them byte for
byte. They must never be re-recorded from the slice's code.

No secret appears in this file: each value is assembled at run time (P1-S3 Notes, P3-S17).

Readings, each used in one place:
- The marker grammar is design.md's, written out here independently (`GATE_MARKER`).
- A blocker's `class` is the policy class's `rule`, and `classes` lists the repository's `gate`.
- The summary names a blocker "by file, line and class" if its path, `:<line>` and class appear
  on one line of its visible text (`line_naming`).
- A secret blocker's fingerprint is `expected_fingerprint(path, "possible-secret", line, 0)`,
  P1-S9's encoding with ordinal 0 (design.md, Blocker fingerprints: "built as a finding's is").
"""

import json
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import read_records
from test_p1_s9_dedupe import expected_fingerprint
from test_p1_s11_markdown_output import PARSER, document_text, outside_code
from test_p2_s1_shadow_report_local import REFERENCE_TRIGGER
from test_p2_s5_comment_sync import (
    APP,
    APP_V1,
    EVAL,
    EVAL_LINE,
    OTHER_FILE,
    Run,
    ai,
    marker_of,
    replaced,
    text,
)
from test_p2_s7_review_new_commits import PullRequest
from test_p2_s13_shadow_backfill import ABSENT_SHA, MergedPull, PerPullProvider, backfill, history

from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider
from review_bot.secret_scan import scan_secrets

pytestmark = pytest.mark.integration

__all__ = ["backfill", "history"]  # fixtures, used by being imported

GOLDEN = REPO_ROOT / "tests" / "fixtures" / "p4_s5"
CONFIG_FILE = ".review-bot.toml"
POLICY_VARIABLE = "REVIEW_BOT_POLICY"
GATE_VARIABLE = "REVIEW_BOT_GATE"
USAGE_EXIT = 2
TRANSIENT_EXIT = 11
POLICY_VERSION = "gate-policy-1"
BUILTIN_VERSION = "builtin-v1"
MONEY = "decimal-money"
SECRET_CLASS = "possible-secret"  # noqa: S105 - a class name, not a secret
AI_LOGIC = "ai:logic"
AI_SECURITY = "ai:security"
PRICES = "billing/prices.py"
KEYS = "src/keys.py"
README = "README.md"
PRICES_V1 = ["from decimal import Decimal", "", "total = 9.99"]  # the money line is line 3
MONEY_LINE = PRICES_V1[2]
PRICES_FIXED = ["from decimal import Decimal", "", 'total = Decimal("9.99")']
DOMAIN_RULE = f'[[domain_rules]]\nrule = "{MONEY}"\npaths = ["billing/**"]\n'
BASE_FILES = {"src/base.py": "base = 1\n", README: "# Project\n"}
FIXED_DATE = "2026-11-12T10:00:00+00:00"
HEX16 = re.compile(r"[0-9a-f]{16}")
MAX_MARKER_ENTRIES = 25
SUMMARY_LIMIT = 60_000
# Generous: it catches a hang or super-linear work, not a slow CI machine.
BOUND_S = 60.0
NOT_ENABLED = {
    "enabled": False, "decision": "not_enabled", "reason": None, "classes": [], "blocking": [],
}  # fmt: skip
NO_CONFIG = {
    "enabled": None, "decision": "unjudged", "reason": "no_config", "classes": [], "blocking": [],
}  # fmt: skip
GATE_RECORD_KEYS = {"enabled", "decision", "reason", "classes", "fingerprints", "policy_version"}
# design.md, Summary marker: the whole last line, and nothing else, is a marker.
_ENTRY = r"[0-9a-f]{16}:[a-z0-9:-]+"
GATE_MARKER = re.compile(
    r"<!-- review-bot:v1 summary reviewed=(?P<reviewed>[0-9a-f]{40}|none)"
    rf"(?: gate=(?P<gate>{_ENTRY}(?:,{_ENTRY}){{0,24}}))?"
    r"(?: more=(?P<more>[1-9][0-9]{0,5}))?"
    r"(?P<tampered> tampered)? -->"
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside CI, under the built-in policy, and with fixed commit dates so SHAs are stable."""
    for name in (
        "CI",
        POLICY_VARIABLE,
        GATE_VARIABLE,
        "REVIEW_BOT_THRESHOLD",
        "REVIEW_BOT_AUDIT_LOG",
        "REVIEW_BOT_MAX_DIFF_BYTES",
        "REVIEW_BOT_MAX_REVIEW_BYTES",
        "REVIEW_BOT_CONTEXT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_AUTHOR_DATE", FIXED_DATE)
    monkeypatch.setenv("GIT_COMMITTER_DATE", FIXED_DATE)


# --- Secrets, assembled at run time -------------------------------------------------------------


def aws_key(seed: int = 0) -> str:
    """An AWS access key ID the scan reports; never written whole in this file."""
    tail = "Q7XW3RT9LP2MN5V" + "BCDEFGHJK"[seed]
    return "AK" + "IA" + tail


def test_p4_s5_this_file_holds_no_secret_the_scan_reports():
    assert scan_secrets(Path(__file__).read_text(encoding="utf-8")) == ()


def test_p4_s5_precondition_the_assembled_key_is_one_the_scan_reports():
    assert scan_secrets(f'+key = "{aws_key()}"\n')


# --- Policy, config and findings ----------------------------------------------------------------


def gate_class(rule: str, severity: str | None = "high", confidence: str | None = "0.9") -> str:
    """One `[[gate.classes]]` entry; None leaves a key out, to take its default."""
    lines = ["[[gate.classes]]", f'rule = "{rule}"']
    if severity is not None:
        lines.append(f'min_severity = "{severity}"')
    if confidence is not None:
        lines.append(f"min_confidence = {confidence}")
    lines += ['approved_by = "@owner"', "approved_on = 2026-11-01"]
    return "\n".join(lines) + "\n"


MONEY_CLASS = gate_class(MONEY)
SECRET_POLICY_CLASS = gate_class(SECRET_CLASS, "critical", "0.9")
AI_LOGIC_CLASS = gate_class(AI_LOGIC)


def policy_text(*classes: str, version: str = POLICY_VERSION) -> str:
    return f'version = "{version}"\n' + "".join(classes)


def repo_config(*classes: str, extra: str = "", domain_rule: str | None = DOMAIN_RULE) -> str:
    """A `.review-bot.toml` opting in to `classes`; top-level keys come before any table."""
    listed = ", ".join(json.dumps(name) for name in classes)
    return f"gate = [{listed}]\n{extra}{domain_rule or ''}"


MONEY_CONFIG = repo_config(MONEY)


def model(  # noqa: PLR0913 - keyword-only, one per finding field a test varies
    line: int,
    *,
    file: str = APP,
    severity: str = "high",
    category: str = "logic",
    confidence: float = 0.9,
    rule: str | None = None,
    rationale: str = "R.",
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        category=category,
        rationale=rationale,
        confidence=confidence,
        rule=rule,
    )


def money_fingerprint(line_text: str = MONEY_LINE, ordinal: int = 0) -> str:
    return expected_fingerprint(PRICES, MONEY, line_text, ordinal)


def secret_fingerprint(path: str, line_text: str) -> str:
    return expected_fingerprint(path, SECRET_CLASS, line_text, 0)


# --- The pull request, the people on it, and what a run left behind -----------------------------


class People:
    """What people do on the PR between runs, through the fake's own calls (the one seam)."""

    LOGIN = "maintainer"

    def __init__(self, client: Any) -> None:
        self.client = client

    def edit_summary(self, body: str | None = None) -> None:
        """A person edits the bot's summary, to `body` or leaving its text as it is."""
        self.client.edit_summary(self.client.summary if body is None else body, by=self.LOGIN)

    def delete_summary(self) -> None:
        self.client.delete_summary()

    def edit_comment(self, comment_id: int, body: str | None = None) -> None:
        """A person edits one of the bot's inline comments, keeping its text unless given."""
        (held,) = [c for c in self.client.comments if c.comment_id == comment_id]
        self.client.edit_comment(comment_id, held.body if body is None else body, by=self.LOGIN)

    def delete_comment(self, comment_id: int) -> None:
        self.client.delete_comment(comment_id)


class GatePR(PullRequest):
    """P2-S7's pull request, under a policy file and a base `.review-bot.toml`."""

    def __init__(  # noqa: PLR0913 - keyword-only, one per thing a gating PR is set up with
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        *,
        config: str | None = MONEY_CONFIG,
        classes: Sequence[str] = (MONEY_CLASS,),
        base_files: dict[str, str] | None = None,
    ) -> None:
        files = {**BASE_FILES, **(base_files or {})}
        if config is not None:
            files[CONFIG_FILE] = config
        super().__init__(tmp_path, capsys, base_files=files)
        self.monkeypatch = monkeypatch
        self.policy_path = tmp_path / "policy.toml"
        self.policy_path.write_text(policy_text(*classes), encoding="utf-8")
        monkeypatch.setenv(POLICY_VARIABLE, str(self.policy_path))
        self.people = People(self.github.client)

    def summary(self) -> str:
        return self.github.summary() or ""

    def marker(self) -> dict[str, Any]:
        """The summary's last line, read under design.md's grammar; it must be well formed."""
        last = self.summary().rsplit("\n", maxsplit=1)[-1]
        match = GATE_MARKER.fullmatch(last)
        assert match is not None, f"not a well-formed summary marker: {last[:300]!r}"
        entries = [tuple(e.split(":", 1)) for e in (match["gate"] or "").split(",") if e]
        return {
            "reviewed": match["reviewed"],
            "gate": entries,
            "more": None if match["more"] is None else int(match["more"]),
            "tampered": match["tampered"] is not None,
        }

    def visible_summary(self) -> str:
        return visible(self.summary())

    def over_max_diff_bytes(self, cap: int = 4_000) -> None:
        """The next runs read a whole diff over `max_diff_bytes`, so they can't check it."""
        self.monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", str(cap))

    def comment_on(self, path: str, line: int) -> Any:
        return self.comment_at(path, line)


def visible(body: str) -> str:
    """A Markdown body as GitHub shows it: escapes undone, code spans as their text."""
    return document_text(PARSER.parse(body))


def line_naming(text_: str, *parts: str) -> str | None:
    """The first line of `text_` holding every one of `parts`, or None."""
    return next((line for line in text_.splitlines() if all(p in line for p in parts)), None)


def gate(run: Run) -> dict[str, Any]:
    document: dict[str, Any] = run.shadow["gate"]
    return document


def blockers(run: Run) -> list[dict[str, Any]]:
    blocking: list[dict[str, Any]] = gate(run)["blocking"]
    return blocking


def fingerprints(run: Run) -> list[str]:
    return [b["fingerprint"] for b in blockers(run)]


def gate_record(run: Run) -> dict[str, Any]:
    records = [
        r
        for r in read_records(run.out / "audit.jsonl")
        if (r["stage"], r["event"]) == ("policy", "gate")
    ]
    assert len(records) == 1, [f"{r['stage']}.{r['event']}" for r in records]
    data: dict[str, Any] = records[0]["data"]
    return data


def money_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> GatePR:
    return GatePR(tmp_path, capsys, monkeypatch, **kw)


def written_everywhere(run: Run, pr: GatePR) -> str:
    """Everything a run wrote: its output files, its GitHub calls and the PR's summary."""
    files = "".join(
        path.read_bytes().decode("utf-8", "replace")
        for path in run.out.rglob("*")
        if path.is_file()
    )
    return files + repr([call.args for call in run.calls]) + pr.summary() + run.stderr


# --- AC1: a shadow run reports what would block -------------------------------------------------


def money_blocker(line: int = 3, fingerprint: str | None = None) -> dict[str, Any]:
    return {
        "fingerprint": fingerprint or money_fingerprint(),
        "class": MONEY,
        "file": PRICES,
        "line": line,
        "severity": "high",
    }


def test_p4_s5_ac1_shadow_gate_is_block_with_the_money_finding(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert gate(run) == {
        "enabled": True,
        "decision": "block",
        "reason": None,
        "classes": [MONEY],
        "blocking": [money_blocker()],
    }


def test_p4_s5_ac1_blocker_fingerprint_is_the_rule_findings(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], mode="shadow")

    (comment,) = [c for c in run.shadow["comments"] if c["rule_part"] == MONEY]
    assert fingerprints(run) == [comment["fingerprint"]]
    assert HEX16.fullmatch(comment["fingerprint"])


def test_p4_s5_ac1_shadow_md_summary_says_would_block(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], mode="shadow")

    shadow_md = visible((run.out / "shadow.md").read_text(encoding="utf-8"))
    assert "Would block the merge: 1 finding (decimal-money)" in shadow_md
    assert "Would block the merge: 1 finding (decimal-money)" in visible(run.shadow["summary_body"])


def test_p4_s5_ac1_shadow_run_uses_no_client_and_writes_no_summary(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], mode="shadow", with_client=True)

    assert run.calls == []
    assert pr.github.summary() is None


# --- AC2: gating off is Phase 3, byte for byte --------------------------------------------------


def normalised(value: str, shas: dict[str, str]) -> str:
    """`value` with each commit SHA, full or as its 7-character prefix, replaced by its name."""
    for sha, name in shas.items():
        value = value.replace(sha, f"<{name}>").replace(sha[:7], f"<{name}:7>")
    return value


def call_record(call: Any, shas: dict[str, str]) -> list[Any]:
    """A recorded call: its name and, for a write, what it wrote."""
    if call.name == "post_review":
        _, comments = call.args
        return [call.name, [[c.path, c.line, normalised(c.body, shas)] for c in comments]]
    if call.name == "update_comment":
        return [call.name, call.args[0], normalised(call.args[1], shas)]
    if call.name == "resolve_thread":
        return [call.name, call.args[0]]
    if call.name == "write_summary":
        return [call.name, normalised(call.args[1], shas)]
    return [call.name]


def phase_3_story(pr: PullRequest, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A PR's life in comment mode: what each run called on GitHub and left as the summary.

    Pushes: the money line and an AI finding; README only; a re-run of that head; the fix; a
    secret; the secret removed; a draft; a provider failure; a diff over `max_diff_bytes`.
    """
    shas: dict[str, str] = {pr.repo.base: "base"}
    records: list[dict[str, Any]] = []

    def push(name: str, files: dict[str, str | None]) -> None:
        shas[pr.push(files)] = name

    def record(label: str, run: Run) -> None:
        records.append(
            {
                "step": label,
                "exit_code": run.exit_code,
                "calls": [call_record(call, shas) for call in run.calls],
                "summary": normalised(pr.github.summary() or "", shas),
            }
        )

    push("push-1", {APP: text(APP_V1), PRICES: text(PRICES_V1)})
    record("1 money line and an AI finding", pr.review([ai(EVAL_LINE)])[0])
    push("push-2", {README: "# Project\n\nMore words.\n"})
    record("2 README only", pr.review([])[0])
    record("3 re-run of the same head", pr.review([ai(EVAL_LINE)])[0])
    push("push-4", {PRICES: text(PRICES_FIXED)})
    record("4 the fix", pr.review([])[0])
    push("push-5", {KEYS: f'key = "{aws_key()}"\n'})
    record("5 a secret", pr.review([])[0])
    push("push-6", {KEYS: "key = load_key()\n"})
    record("6 the secret removed", pr.review([])[0])
    payload = pr.payload()
    payload["pull_request"]["draft"] = True
    record("7 a draft", pr.review([], payload=payload)[0])
    push("push-8", {APP: text([*APP_V1, "    return None"])})
    failing = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))
    record("8 a provider failure", pr.review(provider=failing)[0])
    push("push-9", {"src/big.py": "".join(f"row_{n} = {n}\n" for n in range(2_000))})
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "4000")
    record("9 over max_diff_bytes", pr.review([])[0])
    monkeypatch.delenv("REVIEW_BOT_MAX_DIFF_BYTES")
    return records


STORY_CONFIGS = {
    "no_config_file": None,
    "config_without_gate": DOMAIN_RULE,
    "invalid_config": 'threshold = "severe"\n',
}


def golden_story(name: str) -> list[dict[str, Any]]:
    path = GOLDEN / f"phase_3_story_{name}.json"
    loaded: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def story_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str], config: str | None) -> PullRequest:
    files = dict(BASE_FILES)
    if config is not None:
        files[CONFIG_FILE] = config
    return PullRequest(tmp_path, capsys, base_files=files)


@pytest.mark.parametrize("name", STORY_CONFIGS)
def test_p4_s5_ac2_gating_off_makes_phase_3s_github_calls_and_summaries_byte_for_byte(
    tmp_path, capsys, monkeypatch, name
):
    pr = story_pr(tmp_path, capsys, STORY_CONFIGS[name])

    assert phase_3_story(pr, monkeypatch) == golden_story(name)


@pytest.mark.parametrize("name", ["no_config_file", "config_without_gate"])
def test_p4_s5_ac2_a_policy_approving_classes_changes_nothing_without_a_gate_key(
    tmp_path, capsys, monkeypatch, name
):
    """The policy approves the money and AI classes, but the repository doesn't opt in."""
    pr = story_pr(tmp_path, capsys, STORY_CONFIGS[name])
    policy = tmp_path / "policy.toml"
    policy.write_text(policy_text(MONEY_CLASS, AI_LOGIC_CLASS), encoding="utf-8")
    monkeypatch.setenv(POLICY_VARIABLE, str(policy))

    assert phase_3_story(pr, monkeypatch) == golden_story(name)


@pytest.mark.parametrize("config", [None, DOMAIN_RULE], ids=["no_config_file", "no_gate_key"])
@pytest.mark.parametrize("mode", ["shadow", "comment"])
def test_p4_s5_ac2_gate_is_not_enabled(tmp_path, capsys, monkeypatch, config, mode):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=config)
    pr.push({PRICES: text(PRICES_V1), APP: text(APP_V1)})

    run, _ = pr.review([ai(EVAL_LINE)], mode=mode)

    assert run.exit_code == 0, run.stderr
    assert gate(run) == NOT_ENABLED


def test_p4_s5_ac2_summary_marker_is_phase_3s_with_gating_off(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=DOMAIN_RULE)
    pr.push({PRICES: text(PRICES_V1)})

    pr.review([])

    assert pr.summary().splitlines()[-1] == f"<!-- review-bot:v1 summary reviewed={pr.head} -->"


def test_p4_s5_ac2_gating_off_still_finds_a_summary_written_with_gating_on(
    tmp_path, capsys, monkeypatch
):
    """design.md, Summary marker: the summary is recognised under the full grammar."""
    pr = money_pr(tmp_path, capsys, monkeypatch, config=DOMAIN_RULE)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])
    since = pr.head
    pr.set_summary(
        f"Summary.\n\n<!-- review-bot:v1 summary reviewed={since} gate={'ab' * 8}:{MONEY} -->"
    )
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert (run.shadow["scope"], run.shadow["since"]) == ("incremental", since)


# --- AC3: a gate class the policy or config can't back is invalid -------------------------------

AC3_CASES = {
    "builtin_policy_approves_none": ((), MONEY_CONFIG, (MONEY, BUILTIN_VERSION, "approve")),
    "other_class_approved": ((AI_LOGIC_CLASS,), MONEY_CONFIG, (MONEY, POLICY_VERSION, "approve")),
    "no_domain_rule": (
        (MONEY_CLASS,),
        repo_config(MONEY, domain_rule=None),
        (MONEY, POLICY_VERSION, "domain_rules"),
    ),
    "domain_rule_below_high": (
        (MONEY_CLASS,),
        repo_config(MONEY, domain_rule=DOMAIN_RULE + 'severity = "medium"\n'),
        (MONEY, POLICY_VERSION, "below"),
    ),
    "default_high_below_critical_class": (
        (gate_class(MONEY, "critical"),), MONEY_CONFIG, (MONEY, POLICY_VERSION, "critical")
    ),
    "default_severity_is_critical": (
        (gate_class(MONEY, None),), MONEY_CONFIG, (MONEY, POLICY_VERSION, "critical")
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", AC3_CASES)
@pytest.mark.parametrize("mode", ["shadow", "comment"])
def test_p4_s5_ac3_unbacked_gate_class_is_invalid_config_exit_2_without_a_model_call(
    tmp_path, capsys, monkeypatch, case, mode
):
    classes, config, named = AC3_CASES[case]
    pr = money_pr(tmp_path, capsys, monkeypatch, config=config, classes=classes)
    if not classes:
        monkeypatch.delenv(POLICY_VARIABLE)
    pr.push({PRICES: text(PRICES_V1)})

    run, provider = pr.review([ai(1, file=PRICES)], mode=mode)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("failed", "invalid_config")
    message = visible(run.shadow["summary_body"])
    for part in named:
        assert part in message, (part, message)
    assert gate(run) == NO_CONFIG


def test_p4_s5_ac3_domain_rule_at_the_class_severity_is_valid(tmp_path, capsys, monkeypatch):
    config = repo_config(MONEY, domain_rule=DOMAIN_RULE + 'severity = "critical"\n')
    pr = money_pr(
        tmp_path, capsys, monkeypatch, config=config, classes=(gate_class(MONEY, "critical"),)
    )
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert (gate(run)["decision"], blockers(run)[0]["severity"]) == ("block", "critical")


def test_p4_s5_ac3_unknown_gate_class_name_is_invalid(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=repo_config("ai:nonsense"))
    pr.push({PRICES: text(PRICES_V1)})

    run, provider = pr.review([], mode="shadow")

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []


# --- AC4: which findings count for a class ------------------------------------------------------

AI_SECURITY_CRITICAL = gate_class(AI_SECURITY, "critical", "0.9")
AC4_FINDINGS = {  # (severity, category, confidence) at EVAL_LINE, and whether it blocks
    "critical_095_blocks": (("critical", "security", 0.95), True),
    "high_doesnt": (("high", "security", 0.95), False),
    "confidence_085_doesnt": (("critical", "security", 0.85), False),
    "other_category_doesnt": (("critical", "logic", 0.95), False),
}


@pytest.mark.parametrize("case", AC4_FINDINGS)
def test_p4_s5_ac4_ai_security_class_counts_only_critical_at_090(
    tmp_path, capsys, monkeypatch, case
):
    (severity, category, confidence), blocks = AC4_FINDINGS[case]
    finding = model(EVAL_LINE, severity=severity, category=category, confidence=confidence)
    pr = money_pr(
        tmp_path,
        capsys,
        monkeypatch,
        config=repo_config(AI_SECURITY),
        classes=(AI_SECURITY_CRITICAL,),
    )
    pr.push({APP: text(APP_V1)})

    run, _ = pr.review([finding], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert gate(run)["decision"] == ("block" if blocks else "pass")
    if blocks:
        (blocker,) = blockers(run)
        assert (blocker["class"], blocker["file"], blocker["line"], blocker["severity"]) == (
            AI_SECURITY, APP, EVAL_LINE, "critical"
        )  # fmt: skip
        assert blocker["fingerprint"] == expected_fingerprint(APP, AI_SECURITY, EVAL, 0)


def test_p4_s5_ac4_model_finding_tagged_with_a_rule_blocks_under_no_class(
    tmp_path, capsys, monkeypatch
):
    config = repo_config(MONEY, AI_LOGIC)
    pr = money_pr(
        tmp_path, capsys, monkeypatch, config=config, classes=(MONEY_CLASS, AI_LOGIC_CLASS)
    )
    pr.push({APP: text(APP_V1)})
    tagged = model(EVAL_LINE, severity="critical", confidence=0.95, rule=MONEY)

    run, _ = pr.review([tagged], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert [f["rule"] for f in run.shadow["report"]["findings"]] == [MONEY], "reported, tagged"
    assert (gate(run)["decision"], blockers(run)) == ("pass", [])


def test_p4_s5_ac4_tagged_model_finding_in_billing_doesnt_add_a_second_blocker(
    tmp_path, capsys, monkeypatch
):
    config = repo_config(MONEY, AI_LOGIC)
    pr = money_pr(
        tmp_path, capsys, monkeypatch, config=config, classes=(MONEY_CLASS, AI_LOGIC_CLASS)
    )
    pr.push({PRICES: text(PRICES_V1)})
    tagged = model(3, file=PRICES, severity="critical", confidence=0.95, rule=MONEY)

    run, _ = pr.review([tagged], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert blockers(run) == [money_blocker()]


def test_p4_s5_ac4_threshold_critical_hides_and_unblocks_the_high_rule_finding(
    tmp_path, capsys, monkeypatch
):
    config = repo_config(MONEY, extra='threshold = "critical"\n')
    pr = money_pr(tmp_path, capsys, monkeypatch, config=config)
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert run.shadow["report"]["findings"] == []
    assert (gate(run)["decision"], blockers(run)) == ("pass", [])


def test_p4_s5_ac4_threshold_from_the_variable_unblocks_too(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    monkeypatch.setenv("REVIEW_BOT_THRESHOLD", "critical")

    run, _ = pr.review([], mode="shadow")

    assert (gate(run)["decision"], blockers(run)) == ("pass", [])


# --- AC5: a rule blocker is decided afresh on the whole diff, every push ------------------------


def test_p4_s5_ac5_unrelated_pushes_keep_blocking_and_the_fix_clears(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    first, _ = pr.review([])
    assert (gate(first)["decision"], blockers(first)) == ("block", [money_blocker()])
    comment = pr.comment_on(PRICES, 3)
    assert pr.marker()["gate"] == [(marker_of(comment.body)["fingerprint"], MONEY)]

    pr.push({README: "# Project\n\nMore words.\n"})
    readme, _ = pr.review([])
    assert readme.exit_code == 0, readme.stderr
    assert (gate(readme)["decision"], blockers(readme)) == ("block", [money_blocker()])
    assert pr.marker()["gate"] == [(money_fingerprint(), MONEY)]

    pr.push({OTHER_FILE: "other = 1\n"})
    unrelated, provider = pr.review([])
    assert MONEY_LINE not in "\n".join(r.user_prompt for r in provider.requests)
    assert (gate(unrelated)["decision"], blockers(unrelated)) == ("block", [money_blocker()])

    pr.push({PRICES: text(PRICES_FIXED)})
    fixed, _ = pr.review([])
    assert fixed.exit_code == 0, fixed.stderr
    assert (gate(fixed)["decision"], blockers(fixed)) == ("pass", [])
    assert pr.marker() == {"reviewed": pr.head, "gate": [], "more": None, "tampered": False}


def test_p4_s5_ac5_a_fix_below_the_anchor_elsewhere_in_the_file_clears(
    tmp_path, capsys, monkeypatch
):
    """The money line moves down and is fixed in one push; the whole-diff check finds nothing."""
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])
    pr.push({PRICES: text(["from decimal import Decimal", "", "# moved", "", PRICES_FIXED[2]])})

    run, _ = pr.review([])

    assert (gate(run)["decision"], blockers(run)) == ("pass", [])


TWINS = ["from decimal import Decimal", "", MONEY_LINE, "", MONEY_LINE]  # lines 3 and 5


def test_p4_s5_ac5_after_the_twin_above_goes_the_blocker_keeps_its_comments_fingerprint(
    tmp_path, capsys, monkeypatch
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(TWINS)})
    first, _ = pr.review([])
    upper, lower = pr.comment_on(PRICES, 3), pr.comment_on(PRICES, 5)
    lower_fp = marker_of(lower.body)["fingerprint"]
    assert lower_fp == money_fingerprint(ordinal=1)
    assert sorted(fingerprints(first)) == sorted([money_fingerprint(), lower_fp])
    pr.push({PRICES: text(PRICES_V1)})  # the upper twin and the blank below it are gone
    pr.github.outdate(upper.comment_id)
    pr.github.show_at(lower.comment_id, 3)

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert blockers(run) == [money_blocker(3, lower_fp)], "not the computed ordinal-0 fingerprint"
    assert pr.marker()["gate"] == [(lower_fp, MONEY)]


# --- AC6: a secret the whole diff adds blocks, and nothing shows it -----------------------------

SECRET_CONFIG = repo_config(SECRET_CLASS, domain_rule=None)


def secret_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> GatePR:
    return GatePR(
        tmp_path, capsys, monkeypatch, config=SECRET_CONFIG, classes=(SECRET_POLICY_CLASS,), **kw
    )


def keys_lines(seed: int = 0) -> list[str]:
    return ["import os", f'key = "{aws_key(seed)}"', "name = os.environ['NAME']"]


def test_p4_s5_ac6_secret_line_blocks_every_run_until_removed(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    lines = keys_lines()
    expected = {
        "fingerprint": secret_fingerprint(KEYS, lines[1]),
        "class": SECRET_CLASS,
        "file": KEYS,
        "line": 2,
        "severity": "critical",
    }
    pr.push({KEYS: text(lines)})
    first, provider = pr.review([])
    assert provider.requests == []
    assert first.shadow["outcome"] == "blocked_secret"
    assert (gate(first)["decision"], blockers(first)) == ("block", [expected])

    pr.push({README: "# Project\n\nMore words.\n"})
    second, _ = pr.review([])
    assert (gate(second)["decision"], blockers(second)) == ("block", [expected])
    pr.push({OTHER_FILE: "other = 1\n"})
    third, _ = pr.review([])
    assert (gate(third)["decision"], blockers(third)) == ("block", [expected])

    pr.push({KEYS: text(replaced(lines, 2, "key = os.environ['KEY']"))})
    cleared, _ = pr.review([])
    assert cleared.exit_code == 0, cleared.stderr
    assert blockers(cleared) == []
    assert gate(cleared)["decision"] == "pass"


def test_p4_s5_ac6_secret_blocks_in_shadow_mode_too(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({KEYS: text(keys_lines())})

    run, _ = pr.review([], mode="shadow")

    assert [(b["file"], b["line"], b["class"]) for b in blockers(run)] == [(KEYS, 2, SECRET_CLASS)]


def test_p4_s5_ac6_two_secret_lines_are_two_blockers(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({KEYS: text([*keys_lines(0), f'other = "{aws_key(1)}"'])})

    run, _ = pr.review([], mode="shadow")

    assert sorted((b["file"], b["line"]) for b in blockers(run)) == [(KEYS, 2), (KEYS, 4)]


def test_p4_s5_ac6_no_value_appears_anywhere(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({KEYS: text(keys_lines())})

    run, _ = pr.review([])

    everything = written_everywhere(run, pr)
    assert aws_key() not in everything
    assert aws_key()[4:] not in everything, "no part of the value either"


def test_p4_s5_ac6_secret_in_a_new_path_blocks_once_with_no_file_or_line(
    tmp_path, capsys, monkeypatch
):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    path = f"config/{aws_key(1)}.txt"
    pr.push({path: text([f'key = "{aws_key(2)}"', "plain = 1", f'again = "{aws_key(3)}"'])})

    run, _ = pr.review([])

    (blocker,) = blockers(run)
    assert (blocker["class"], blocker["file"], blocker["line"]) == (SECRET_CLASS, None, None)
    assert HEX16.fullmatch(blocker["fingerprint"])
    assert gate(run)["decision"] == "block"
    assert "a file path holding a possible secret" in pr.visible_summary()
    everything = written_everywhere(run, pr)
    for value in (aws_key(1), aws_key(2), aws_key(3), path):
        assert value not in everything, value
    assert gate_record(run)["fingerprints"] == [blocker["fingerprint"]]


def test_p4_s5_ac6_path_secret_and_a_line_secret_elsewhere_are_two_blockers(
    tmp_path, capsys, monkeypatch
):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({f"config/{aws_key(1)}.txt": "plain = 1\n", KEYS: text(keys_lines())})

    run, _ = pr.review([], mode="shadow")

    assert sorted((b["file"] or "", b["line"] or 0) for b in blockers(run)) == [("", 0), (KEYS, 2)]


def unblocked(run: Run) -> None:
    """No blocker; `unjudged` if the review stopped on the secret, else `pass` (AC11)."""
    assert blockers(run) == []
    expected = "unjudged" if run.shadow["outcome"] == "blocked_secret" else "pass"
    assert gate(run)["decision"] == expected


def test_p4_s5_ac6_secret_only_on_a_removed_line_doesnt_block(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch, base_files={KEYS: text(keys_lines())})
    pr.push({KEYS: text(replaced(keys_lines(), 2, "key = os.environ['KEY']"))})

    run, _ = pr.review([])

    unblocked(run)


def test_p4_s5_ac6_secret_only_on_a_context_line_doesnt_block(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch, base_files={KEYS: text(keys_lines())})
    pr.push({KEYS: text(replaced(keys_lines(), 3, "name = 'fixed'"))})
    assert aws_key() in pr.repo.pr_diff(pr.head), "the secret is a context line of the PR diff"

    run, _ = pr.review([])

    unblocked(run)


def test_p4_s5_ac6_secret_only_in_an_old_path_doesnt_block(tmp_path, capsys, monkeypatch):
    old = f"config/{aws_key(4)}.txt"
    pr = secret_pr(tmp_path, capsys, monkeypatch, base_files={old: "plain = 1\n"})
    pr.push({}, renames={old: "config/clean.txt"})

    run, _ = pr.review([])

    unblocked(run)


def test_p4_s5_ac6_secret_added_then_removed_inside_the_pr_doesnt_block(
    tmp_path, capsys, monkeypatch
):
    """The whole base...head diff is what counts, not the history."""
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({KEYS: text(keys_lines())})
    pr.push({KEYS: text(replaced(keys_lines(), 2, "key = os.environ['KEY']"))})

    run, _ = pr.review([])

    assert (gate(run)["decision"], blockers(run)) == ("pass", [])


# --- AC7: the summary says it blocks, and how to appeal and override ----------------------------


def test_p4_s5_ac7_summary_lists_the_blocker_and_says_how_to_appeal_and_override(
    tmp_path, capsys, monkeypatch
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    pr.review([])

    shown = pr.visible_summary()
    assert "Blocks the merge" in shown
    assert line_naming(shown, f"{PRICES}:3", MONEY), shown
    assert "reply" in shown.lower(), "how to appeal: reply on the thread"
    assert "/review-bot override:" in shown
    assert pr.marker()["gate"] == [(money_fingerprint(), MONEY)]


def test_p4_s5_ac7_secret_blocker_shows_its_fingerprint_and_the_review_override(
    tmp_path, capsys, monkeypatch
):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    lines = keys_lines()
    pr.push({KEYS: text(lines)})

    pr.review([])

    shown = pr.visible_summary()
    fingerprint = secret_fingerprint(KEYS, lines[1])
    assert "Blocks the merge" in shown
    assert line_naming(shown, f"{KEYS}:2", SECRET_CLASS), shown
    assert "/review-bot override " in shown
    assert line_naming(shown, fingerprint), "each such blocker's fingerprint is shown"
    assert pr.marker()["gate"] == [(fingerprint, SECRET_CLASS)]


def test_p4_s5_ac7_fingerprints_and_paths_are_inert_on_github(tmp_path, capsys, monkeypatch):
    """A 16-hex fingerprint outside code would link as a commit (P2-S1 AC11)."""
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({KEYS: text(keys_lines())})

    pr.review([])

    body_without_marker = pr.summary().rsplit("\n", 1)[0]
    outside = outside_code(PARSER.parse(body_without_marker))
    assert not REFERENCE_TRIGGER.findall(outside), REFERENCE_TRIGGER.findall(outside)


def test_p4_s5_ac7_a_summary_with_no_blocker_doesnt_say_it_blocks(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_FIXED)})

    pr.review([])

    assert "Blocks the merge" not in pr.visible_summary()
    assert pr.marker()["gate"] == []


# --- AC8: AI blockers carry from a trusted summary ----------------------------------------------

AI_CONFIG = repo_config(AI_LOGIC, extra="max_open_comments = 2\n", domain_rule=None)


class AiCarry:
    """Push 1: three AI blockers, two commented (A and C) and one withheld over the limit (B)."""

    def __init__(self, pr: GatePR) -> None:
        self.pr = pr
        pr.push({APP: text(APP_V1)})
        findings = [model(1, severity="critical"), model(EVAL_LINE, severity="critical"), model(6)]
        run, _ = pr.review(findings)
        assert run.exit_code == 0, run.stderr
        assert gate(run)["decision"] == "block"
        assert len(blockers(run)) == 3
        (withheld,) = run.shadow["withheld"]
        assert withheld["reason"] == "over_limit"
        self.b = withheld["fingerprint"]
        commented = {e["line"]: e["fingerprint"] for e in run.entries if e["action"] == "create"}
        assert sorted(commented) == [1, EVAL_LINE], "the high one is withheld"
        self.a, self.c = commented[EVAL_LINE], commented[1]
        self.a_comment = pr.comment_on(APP, EVAL_LINE)
        self.c_comment = pr.comment_on(APP, 1)

    def next_push(self) -> Run:
        """An unrelated push, and an incremental run that reports nothing new."""
        self.pr.push({OTHER_FILE: f"other = {self.pr._runs}\n"})
        run, _ = self.pr.review([])
        assert run.exit_code == 0, run.stderr
        assert run.shadow["scope"] == "incremental"
        return run


@pytest.fixture
def ai_carry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> AiCarry:
    pr = GatePR(tmp_path, capsys, monkeypatch, config=AI_CONFIG, classes=(AI_LOGIC_CLASS,))
    return AiCarry(pr)


def test_p4_s5_ac8_open_withheld_and_deleted_ai_blockers_all_carry(ai_carry):
    ai_carry.pr.people.delete_comment(ai_carry.c_comment.comment_id)

    run = ai_carry.next_push()

    assert gate(run)["decision"] == "block"
    assert gate(run)["reason"] is None
    assert sorted(fingerprints(run)) == sorted([ai_carry.a, ai_carry.b, ai_carry.c])
    assert sorted(fp for fp, _ in ai_carry.pr.marker()["gate"]) == sorted(fingerprints(run))


def test_p4_s5_ac8_withheld_and_deleted_are_listed_as_carried_with_no_comment(ai_carry):
    ai_carry.pr.people.delete_comment(ai_carry.c_comment.comment_id)

    ai_carry.next_push()

    shown = ai_carry.pr.visible_summary()
    for fingerprint in (ai_carry.b, ai_carry.c):
        listed = line_naming(shown, fingerprint)
        assert listed is not None, (fingerprint, shown)
        assert "carried from an earlier review, no comment" in listed, listed


def test_p4_s5_ac8_a_person_resolving_the_thread_doesnt_clear_it(ai_carry):
    ai_carry.pr.github.person_resolves(ai_carry.a_comment.comment_id)

    run = ai_carry.next_push()

    assert ai_carry.a in fingerprints(run)


def test_p4_s5_ac8_the_bot_resolving_it_because_its_line_went_drops_it(ai_carry):
    pr = ai_carry.pr
    pr.push({APP: text(replaced(APP_V1, EVAL_LINE, "    value = parse(data)"))})
    pr.github.outdate(ai_carry.a_comment.comment_id)

    run, _ = pr.review([])

    assert run.resolved_threads == [ai_carry.a_comment.thread_id]
    assert sorted(fingerprints(run)) == sorted([ai_carry.b, ai_carry.c])
    assert gate(run)["decision"] == "block"


def test_p4_s5_ac8_carried_blockers_survive_many_pushes(ai_carry):
    for _ in range(3):
        run = ai_carry.next_push()

    assert sorted(fingerprints(run)) == sorted([ai_carry.a, ai_carry.b, ai_carry.c])


def test_p4_s5_ac8_a_carried_blocker_whose_class_is_no_longer_enabled_isnt_carried(
    tmp_path, capsys, monkeypatch
):
    """The base branch drops `ai:logic` from `gate`; the money class stays enabled."""
    config = repo_config(MONEY, AI_LOGIC)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(MONEY_CLASS, AI_LOGIC_CLASS))
    pr.push({APP: text(APP_V1)})
    pr.review([ai(EVAL_LINE)])
    pr.advance_main({CONFIG_FILE: MONEY_CONFIG})
    pr.merge_main()
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert blockers(run) == []


# AC8 and AC11: when the whole diff can't be checked, every trusted entry carries.


class MixedPR(GatePR):
    """Push 1: a money blocker and an AI blocker, both commented, in a trusted summary."""

    def __init__(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        classes = (MONEY_CLASS, AI_LOGIC_CLASS)
        config = repo_config(MONEY, AI_LOGIC)
        super().__init__(tmp_path, capsys, monkeypatch, config=config, classes=classes)
        self.push({PRICES: text(PRICES_V1), APP: text(APP_V1)})
        run, _ = self.review([ai(EVAL_LINE)])
        assert sorted(b["class"] for b in blockers(run)) == [AI_LOGIC, MONEY]
        self.first_entries = sorted(self.marker()["gate"])
        self.first_reviewed = self.head


@pytest.fixture
def mixed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> MixedPR:
    return MixedPR(tmp_path, capsys, monkeypatch)


def assert_every_entry_carried(pr: MixedPR, run: Run, reason: str) -> None:
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", reason)
    assert gate(run)["decision"] == "block"
    assert sorted((b["fingerprint"], b["class"]) for b in blockers(run)) == pr.first_entries
    marker = pr.marker()
    assert sorted(marker["gate"]) == pr.first_entries
    assert marker["reviewed"] == pr.first_reviewed
    assert run.writes == ["write_summary"]


def test_p4_s5_ac8_oversized_push_fixing_the_money_line_carries_every_entry(mixed):
    mixed.push(
        {PRICES: text(PRICES_FIXED), "src/big.py": "".join(f"r_{n} = {n}\n" for n in range(2_000))}
    )
    mixed.over_max_diff_bytes()

    run, provider = mixed.review([])

    assert provider.requests == []
    assert_every_entry_carried(mixed, run, "too_large")


def test_p4_s5_ac8_unreadable_push_fixing_the_money_line_carries_every_entry(mixed):
    (mixed.repo.path / "src").mkdir(exist_ok=True)
    (mixed.repo.path / "src" / "latin1.py").write_bytes(b"name = 'caf\xe9'\n")
    mixed.push({PRICES: text(PRICES_FIXED)})

    run, _ = mixed.review([])

    assert_every_entry_carried(mixed, run, "unreadable")


def test_p4_s5_ac8_once_the_diff_is_readable_again_the_fixed_rule_blocker_drops(mixed):
    mixed.push(
        {PRICES: text(PRICES_FIXED), "src/big.py": "".join(f"r_{n} = {n}\n" for n in range(2_000))}
    )
    mixed.over_max_diff_bytes()
    mixed.review([])
    mixed.monkeypatch.delenv("REVIEW_BOT_MAX_DIFF_BYTES")
    mixed.push({"src/big.py": None})

    run, _ = mixed.review([])

    assert [b["class"] for b in blockers(run)] == [AI_LOGIC]


# AC8: a blocker withheld on a line whose earlier comment the bot resolved still carries.

SHELL = "    os.system(cmd)"
SHELL_LINES = [
    "def run(cmd):",
    SHELL,
    "    return 0",
    "def other(a):",
    "    b = a + 1",
    "    return b",
]
SECURITY_CONFIG = repo_config(AI_SECURITY, extra="max_open_comments = 1\n", domain_rule=None)


def shell_finding() -> Finding:
    """`ai:security` at `high`, 0.95: a blocker under the class, below a critical finding."""
    return model(2, severity="high", category="security", confidence=0.95)


def test_p4_s5_ac8_withheld_blocker_on_a_line_whose_old_comment_the_bot_resolved_still_carries(
    tmp_path, capsys, monkeypatch
):
    """Review round 1's laundering path: the re-added line's fingerprint F matches a comment the
    bot resolved earlier, but this time F is withheld, so that old resolution can't drop it."""
    pr = GatePR(
        tmp_path, capsys, monkeypatch, config=SECURITY_CONFIG, classes=(gate_class(AI_SECURITY),)
    )
    pr.push({APP: text(SHELL_LINES)})
    first, _ = pr.review([shell_finding()])
    (comment,) = pr.open_comments()
    fingerprint = marker_of(comment.body)["fingerprint"]
    assert fingerprint == expected_fingerprint(APP, AI_SECURITY, SHELL, 0)
    assert fingerprints(first) == [fingerprint]

    pr.push({APP: text(replaced(SHELL_LINES, 2, None))})
    pr.github.outdate(comment.comment_id)
    removed, _ = pr.review([])
    assert removed.resolved_threads == [comment.thread_id], "the bot resolves it: the line went"
    assert (gate(removed)["decision"], blockers(removed)) == ("pass", [])

    pr.push({APP: text(SHELL_LINES)})
    critical = model(5, severity="critical", confidence=0.95)  # takes the one open-comment slot
    readded, _ = pr.review([shell_finding(), critical])
    assert [(w["fingerprint"], w["reason"]) for w in readded.shadow["withheld"]] == [
        (fingerprint, "over_limit")
    ], "precondition: the re-added finding is withheld, with F"
    assert fingerprint in fingerprints(readded)
    assert gate(readded)["decision"] == "block"

    pr.push({README: "# Project\n\nMore words.\n"})
    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert fingerprint in fingerprints(run), "the bot's old resolution doesn't drop it"
    assert gate(run)["decision"] == "block"
    assert (fingerprint, AI_SECURITY) in pr.marker()["gate"]


# --- AC9: an untrusted or missing summary is rebuilt from the bot's inline comments -------------


def assert_full_review(run: Run) -> None:
    assert run.exit_code == 0, run.stderr
    assert (run.shadow["scope"], run.shadow["since"]) == ("full", None)


def test_p4_s5_ac9_person_edited_summary_reviews_whole_diff_and_rebuilds(ai_carry):
    pr = ai_carry.pr
    pr.people.edit_summary()
    pr.push({OTHER_FILE: "other = 1\n"})

    run, provider = pr.review([])

    assert_full_review(run)
    assert EVAL in "\n".join(r.user_prompt for r in provider.requests), "the whole diff is sent"
    assert (gate(run)["decision"], gate(run)["reason"]) == ("block", None)
    assert ai_carry.a in fingerprints(run) and ai_carry.c in fingerprints(run)
    assert ai_carry.b not in fingerprints(run), "a withheld blocker can't be rebuilt"
    marker = pr.marker()
    assert not marker["tampered"]
    assert marker["reviewed"] == pr.head
    assert "tamper" in pr.visible_summary().lower(), "the summary says a tamper occurred"


def test_p4_s5_ac9_edited_summary_rewritten_to_clear_the_gate_still_rebuilds(ai_carry):
    """The laundering try: a person rewrites the marker to an empty gate at the head."""
    pr = ai_carry.pr
    pr.push({OTHER_FILE: "other = 1\n"})
    pr.people.edit_summary(f"All good.\n\n<!-- review-bot:v1 summary reviewed={pr.head} -->")

    run, _ = pr.review([])

    assert run.shadow["outcome"] == "reviewed", "not skipped already_reviewed"
    assert_full_review(run)
    assert ai_carry.a in fingerprints(run)
    assert gate(run)["decision"] == "block"


def test_p4_s5_ac9_rebuild_skips_bot_resolved_low_and_disabled_class_comments(
    tmp_path, capsys, monkeypatch
):
    """Rebuilt: open and person-resolved comments of an enabled class meeting its minimums."""
    pr = GatePR(
        tmp_path,
        capsys,
        monkeypatch,
        config=repo_config(AI_LOGIC, domain_rule=None),
        classes=(AI_LOGIC_CLASS,),
    )
    lines = [f"    step_{n} = compute({n})" for n in range(1, 8)]
    pr.push({APP: text(lines)})
    findings = [
        model(1),  # open: rebuilt
        model(2),  # person-resolved: rebuilt
        model(3, confidence=0.85),  # below 0.9: not a blocker, not rebuilt
        model(4, severity="medium"),  # below high
        model(5, category="security", severity="critical", confidence=0.99),  # class not enabled
    ]
    first, _ = pr.review(findings)
    assert len(first.posted) == 5, first.calls
    assert sorted(b["line"] for b in blockers(first)) == [1, 2]
    pr.github.person_resolves(pr.comment_on(APP, 2).comment_id)
    pr.people.delete_summary()
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert_full_review(run)
    expected = {marker_of(pr.comment_on(APP, 1).body)["fingerprint"]}
    expected.add(marker_of(pr.comment_at(APP, 2, resolved_by="person").body)["fingerprint"])
    assert set(fingerprints(run)) == expected
    assert gate(run)["reason"] is None


MALFORMED = {
    "no_marker": "Summary.",
    "uppercase_hex_entry": "reviewed={head} gate=" + "AB" * 8 + ":ai:logic",
    "unknown_class": "reviewed={head} gate=" + "ab" * 8 + ":ai:nonsense",
    "empty_gate": "reviewed={head} gate=",
    "more_without_25_entries": "reviewed={head} gate=" + "ab" * 8 + ":ai:logic more=3",
    "more_zero": "reviewed={head} gate="
    + ",".join(f"{n:016x}:ai:logic" for n in range(25))
    + " more=0",
    "more_leading_zero": "reviewed={head} gate="
    + ",".join(f"{n:016x}:ai:logic" for n in range(25))
    + " more=05",
    "more_7_digits": "reviewed={head} gate="
    + ",".join(f"{n:016x}:ai:logic" for n in range(25))
    + " more=1000000",
    "26_entries": "reviewed={head} gate=" + ",".join(f"{n:016x}:ai:logic" for n in range(26)),
    "tampered_twice": "reviewed={head} tampered tampered",
    "fields_out_of_order": "reviewed={head} tampered gate=" + "ab" * 8 + ":ai:logic",
    "double_space": "reviewed={head}  gate=" + "ab" * 8 + ":ai:logic",
    "trailing_comma": "reviewed={head} gate=" + "ab" * 8 + ":ai:logic,",
    "unknown_field": "reviewed={head} owner=me",
}


@pytest.mark.parametrize("case", MALFORMED)
def test_p4_s5_ac9_malformed_marker_reads_nothing_and_rebuilds(ai_carry, case):
    pr = ai_carry.pr
    pr.push({OTHER_FILE: "other = 1\n"})
    fields = MALFORMED[case].format(head=pr.head)
    pr.set_summary(
        f"Summary.\n\n<!-- review-bot:v1 summary {fields} -->" if "=" in fields else fields
    )

    run, _ = pr.review([])

    assert_full_review(run)
    assert ai_carry.a in fingerprints(run)


def test_p4_s5_ac9_marker_over_2000_characters_is_malformed(ai_carry):
    pr = ai_carry.pr
    pr.push({OTHER_FILE: "other = 1\n"})
    entries = ",".join(f"{n:016x}:ai:logic" for n in range(25))
    line = f"<!-- review-bot:v1 summary reviewed={pr.head} gate={entries} more=999999 -->"
    padded = (
        line.replace(" -->", " " * (2_001 - len(line)) + " -->") if len(line) <= 2_000 else line
    )
    assert len(padded) > 2_000
    pr.set_summary(f"Summary.\n\n{padded}")

    run, _ = pr.review([])

    assert_full_review(run)


def test_p4_s5_ac9_deleted_summary_with_bot_comments_reviews_whole_diff_and_rebuilds(ai_carry):
    pr = ai_carry.pr
    pr.people.delete_summary()
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert_full_review(run)
    assert sorted(fingerprints(run)) == sorted([ai_carry.a, ai_carry.c])
    assert not pr.marker()["tampered"]
    assert "tamper" in pr.visible_summary().lower()


def test_p4_s5_ac9_deleted_summary_without_bot_comments_is_a_fresh_review_with_no_tamper(
    tmp_path, capsys, monkeypatch
):
    """Limits: a PR whose only AI blocker was withheld loses it, and no tamper is reported.

    Withheld as `possible_secret`, so the bot posts no inline comment at all.
    """
    config = repo_config(AI_LOGIC, domain_rule=None)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(AI_LOGIC_CLASS,))
    pr.push({APP: text(APP_V1)})
    first, _ = pr.review([model(EVAL_LINE, rationale=f"The key {aws_key()} leaks.")])
    assert (len(blockers(first)), first.posted) == (1, [])
    pr.people.delete_summary()
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert_full_review(run)
    assert (gate(run)["decision"], gate(run)["reason"], blockers(run)) == ("pass", None, [])
    assert "tamper" not in pr.visible_summary().lower()


def assert_tamper_not_silent(pr: GatePR, run: Run) -> None:
    """AC9's path: the whole diff is reviewed, and the run is `tampered` or says a tamper
    occurred, so a lost withheld blocker never passes unremarked."""
    assert_full_review(run)
    said = "tamper" in pr.visible_summary().lower()
    assert gate(run)["reason"] == "tampered" or said, (gate(run), pr.visible_summary())


def test_p4_s5_ac9_a_line_appended_under_the_marker_is_a_tamper_not_a_fresh_summary(
    tmp_path, capsys, monkeypatch
):
    """Review round 1's laundering path. The PR's only `ai:` blocker is withheld, so there is no
    inline comment; a person appends a line under the summary's marker, so the marker is no
    longer its last line. The bot's issue comment was last edited by someone else (AC9)."""
    config = repo_config(AI_LOGIC, domain_rule=None)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(AI_LOGIC_CLASS,))
    pr.push({APP: text(APP_V1)})
    first, _ = pr.review([model(EVAL_LINE, rationale=f"The key {aws_key()} leaks.")])
    (blocker,) = blockers(first)
    assert (first.posted, pr.marker()["gate"]) == ([], [(blocker["fingerprint"], AI_LOGIC)])
    pr.people.edit_summary(pr.summary() + "\n\nThanks, will look.")
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert_tamper_not_silent(pr, run)
    assert pr.marker()["reviewed"] == pr.head or pr.marker()["tampered"]

    pr.push({OTHER_FILE: "other = 2\n"})
    later, _ = pr.review([])

    assert_tamper_not_silent(pr, later), "the edited comment still stands, so still not trusted"


def test_p4_s5_ac9_a_line_appended_under_the_marker_with_inline_comments_rebuilds(ai_carry):
    """The same edit where A and C have inline comments: they are rebuilt; B is lost, said."""
    pr = ai_carry.pr
    pr.people.edit_summary(pr.summary() + "\nnote")
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert_tamper_not_silent(pr, run)
    assert {ai_carry.a, ai_carry.c} <= set(fingerprints(run))


def test_p4_s5_ac9_rebuild_cant_finish_without_the_whole_diff_checks(ai_carry):
    pr = ai_carry.pr
    pr.people.edit_summary()
    pr.push({"src/big.py": "".join(f"r_{n} = {n}\n" for n in range(2_000))})
    pr.over_max_diff_bytes()

    run, _ = pr.review([])

    assert (gate(run)["decision"], gate(run)["reason"]) == ("block", "tampered")
    assert ai_carry.a in fingerprints(run)
    assert pr.marker()["tampered"]
    assert pr.marker()["reviewed"] is not None


def test_p4_s5_ac9_tampered_marker_makes_the_next_run_try_again_and_succeed(ai_carry):
    pr = ai_carry.pr
    pr.people.edit_summary()
    pr.push({"src/big.py": "".join(f"r_{n} = {n}\n" for n in range(2_000))})
    pr.over_max_diff_bytes()
    pr.review([])
    assert pr.marker()["tampered"]
    pr.monkeypatch.delenv("REVIEW_BOT_MAX_DIFF_BYTES")
    pr.push({"src/big.py": None})

    run, _ = pr.review([])

    assert_full_review(run)
    assert (gate(run)["decision"], gate(run)["reason"]) == ("block", None)
    assert not pr.marker()["tampered"]
    assert ai_carry.a in fingerprints(run)


def test_p4_s5_ac9_a_bot_written_tampered_marker_is_untrusted(ai_carry):
    """A ` tampered` marker the bot itself last wrote still takes the rebuild path."""
    pr = ai_carry.pr
    pr.push({OTHER_FILE: "other = 1\n"})
    pr.set_summary(f"Summary.\n\n<!-- review-bot:v1 summary reviewed={pr.head} tampered -->")

    run, _ = pr.review([])

    assert_full_review(run)


def test_p4_s5_limits_huge_hostile_summary_is_read_quickly_as_malformed(ai_carry):
    pr = ai_carry.pr
    pr.push({OTHER_FILE: "other = 1\n"})
    entry = "ab" * 8 + ":ai:logic,"
    pr.set_summary(
        "x" * 1_000_000
        + f"\n<!-- review-bot:v1 summary reviewed={pr.head} gate="
        + entry * 200_000
        + " -->"
    )

    started = time.monotonic()
    run, _ = pr.review([])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert_full_review(run)


# --- AC10: a withheld blocker still blocks ------------------------------------------------------


def test_p4_s5_ac10_over_limit_blocker_blocks_and_is_named_by_file_and_line(ai_carry):
    pr = ai_carry.pr
    shown = pr.visible_summary()
    assert line_naming(shown, f"{APP}:6"), shown
    assert ai_carry.b in [fp for fp, _ in pr.marker()["gate"]]


def test_p4_s5_ac10_possible_secret_withheld_blocker_still_blocks(tmp_path, capsys, monkeypatch):
    pr = GatePR(
        tmp_path,
        capsys,
        monkeypatch,
        config=repo_config(AI_LOGIC, domain_rule=None),
        classes=(AI_LOGIC_CLASS,),
    )
    pr.push({APP: text(APP_V1)})
    leaky = model(EVAL_LINE, rationale=f"The key {aws_key()} is used here.")

    run, _ = pr.review([leaky])

    assert run.exit_code == 0, run.stderr
    (withheld,) = run.shadow["withheld"]
    assert withheld["reason"] == "possible_secret"
    assert fingerprints(run) == [withheld["fingerprint"]]
    assert gate(run)["decision"] == "block"
    assert line_naming(pr.visible_summary(), f"{APP}:{EVAL_LINE}")
    assert aws_key() not in written_everywhere(run, pr)


def test_p4_s5_ac10_withheld_blocker_in_shadow_mode_would_block(tmp_path, capsys, monkeypatch):
    config = repo_config(AI_LOGIC, extra="max_open_comments = 1\n", domain_rule=None)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(AI_LOGIC_CLASS,))
    pr.push({APP: text(APP_V1)})

    run, _ = pr.review([model(1, severity="critical"), model(EVAL_LINE)], mode="shadow")

    assert len(run.shadow["withheld"]) == 1
    assert len(blockers(run)) == 2


# --- AC11: every outcome has a decision, and skips write the summary with gating on -------------


def test_p4_s5_ac11_reviewed_with_no_blocker_is_pass_and_reviewed_advances(
    tmp_path, capsys, monkeypatch
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_FIXED)})

    run, _ = pr.review([])

    assert (gate(run)["decision"], gate(run)["reason"]) == ("pass", None)
    assert pr.marker()["reviewed"] == pr.head


def test_p4_s5_ac11_no_new_changes_decides_and_advances_reviewed(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])
    pr.advance_main({"src/base.py": "base = 2\n"})
    head = pr.merge_main()

    run, provider = pr.review([])

    assert provider.requests == []
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("reviewed", "no_new_changes")
    assert gate(run)["decision"] == "block"
    assert pr.marker()["reviewed"] == head


def test_p4_s5_ac11_already_reviewed_is_decided_again_keeping_the_body(ai_carry):
    pr = ai_carry.pr
    headline = pr.summary().split("\n\n", 1)[0]
    entries_before = sorted(pr.marker()["gate"])

    run, provider = pr.review([])

    assert provider.requests == []
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", "already_reviewed")
    assert gate(run)["decision"] == "block"
    assert run.writes == ["write_summary"]
    assert pr.summary().split("\n\n", 1)[0] == headline, "the previous summary's body is kept"
    assert sorted(pr.marker()["gate"]) == entries_before
    assert pr.marker()["reviewed"] == pr.head


def test_p4_s5_ac11_provider_failure_is_block_with_a_blocker_and_keeps_reviewed(mixed):
    mixed.push({OTHER_FILE: "other = 1\n"})
    failing = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))

    run, _ = mixed.review(provider=failing)

    assert run.exit_code == TRANSIENT_EXIT, run.stderr
    assert gate(run)["decision"] == "block"
    assert mixed.marker()["reviewed"] == mixed.first_reviewed
    assert "write_summary" in run.writes


def test_p4_s5_ac11_provider_failure_with_no_blocker_is_unjudged_never_pass(
    tmp_path, capsys, monkeypatch
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_FIXED)})
    failing = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))

    run, _ = pr.review(provider=failing)

    assert run.exit_code == TRANSIENT_EXIT, run.stderr
    assert (gate(run)["decision"], gate(run)["reason"]) == ("unjudged", None)
    assert "write_summary" in run.writes
    assert pr.marker()["reviewed"] == "none"


def test_p4_s5_ac11_provider_failure_still_finds_the_new_rule_blocker(
    tmp_path, capsys, monkeypatch
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    failing = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))

    run, _ = pr.review(provider=failing, mode="shadow")

    assert (gate(run)["decision"], blockers(run)) == ("block", [money_blocker()])


SKIPS = {
    "nothing_to_review": {README: "# Project\n\nWords.\n"},
    "empty": {},
}


@pytest.mark.parametrize("reason", SKIPS)
def test_p4_s5_ac11_a_skip_with_no_blocker_is_unjudged_and_writes_the_summary(
    tmp_path, capsys, monkeypatch, reason
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push(SKIPS[reason])

    run, _ = pr.review([])

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", reason)
    assert (gate(run)["decision"], blockers(run)) == ("unjudged", [])
    assert run.writes == ["write_summary"], "a skip edits the summary with gating on"
    assert pr.marker()["reviewed"] == "none"


def test_p4_s5_ac11_too_large_with_no_summary_and_no_bot_comments_is_unjudged(
    tmp_path, capsys, monkeypatch
):
    """Limits: no summary, no bot comments and no whole-diff check fails open, as Q5 accepts."""
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push(
        {PRICES: text(PRICES_V1), "src/big.py": "".join(f"r_{n} = {n}\n" for n in range(2_000))}
    )
    pr.over_max_diff_bytes()

    run, _ = pr.review([])

    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", "too_large")
    assert (gate(run)["decision"], blockers(run)) == ("unjudged", [])
    assert run.writes == ["write_summary"]


def test_p4_s5_ac11_blocked_secret_with_a_money_blocker_blocks(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1), KEYS: text(keys_lines())})

    run, _ = pr.review([])

    assert run.shadow["outcome"] == "blocked_secret"
    assert (gate(run)["decision"], blockers(run)) == ("block", [money_blocker()])
    assert pr.marker()["reviewed"] == "none"


def test_p4_s5_ac11_blocked_secret_without_a_blocker_is_unjudged(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_FIXED), KEYS: text(keys_lines())})

    run, _ = pr.review([])

    assert (gate(run)["decision"], blockers(run)) == ("unjudged", [])


def draft(pr: GatePR) -> dict[str, Any]:
    payload = pr.payload()
    payload["pull_request"]["draft"] = True
    return payload


def fork(pr: GatePR) -> dict[str, Any]:
    payload = pr.payload()
    payload["pull_request"]["head"]["repo"] = {"full_name": "someone/fork"}
    return payload


def closed(pr: GatePR) -> dict[str, Any]:
    return pr.payload(action="closed")


def head_not_in_clone(pr: GatePR) -> dict[str, Any]:
    payload = pr.payload()
    payload["pull_request"]["head"]["sha"] = "0f" * 20
    return payload


REFUSALS = {"draft": draft, "fork": fork, "action": closed}


@pytest.mark.parametrize("refusal", REFUSALS)
def test_p4_s5_ac11_refused_run_is_unjudged_no_config(tmp_path, capsys, monkeypatch, refusal):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, provider = pr.review([], payload=REFUSALS[refusal](pr))

    assert (run.out / "shadow.json").exists(), (run.exit_code, run.stderr)
    assert provider.requests == []
    assert gate(run) == NO_CONFIG
    assert gate_record(run)["reason"] == "no_config"


def test_p4_s5_ac11_backfill_missing_commit_is_unjudged_no_config(backfill, history):
    """`missing_commit` is `pr backfill`'s outcome (P2-S13 AC4); `pr run` has none."""
    present = history.merged(61)
    missing = MergedPull(62, present.base, ABSENT_SHA)

    run = backfill.run([present, missing], history.repo.path, provider=PerPullProvider())

    assert run.exit_code == 0, run.stderr
    document = run.shadow(62)
    assert document["reason"] == "missing_commit"
    assert document["gate"] == NO_CONFIG
    assert run.shadow(61)["gate"] == NOT_ENABLED


def test_p4_s5_ac11_no_credential_run_is_unjudged_no_config(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    run = pr.run([], provider_name="claude", provider=None)

    assert run.shadow["reason"] == "no_credential"
    assert gate(run) == NO_CONFIG


# Runs that never read a valid config: the base branch's config made invalid, or `pr run`'s
# form of a missing commit, a head not in the clone, which is a git error (exit 19, no report).
NO_CONFIG_RUNS = ("invalid_config", "head_not_in_clone")
GIT_ERROR_EXIT = 19


def no_config_run(pr: GatePR, kind: str) -> Run:
    if kind == "invalid_config":
        pr.advance_main({CONFIG_FILE: 'threshold = "severe"\n'})
        run, _ = pr.review([])
        assert run.shadow["reason"] == "invalid_config"
        assert gate(run) == NO_CONFIG
        return run
    run, _ = pr.review([], payload=head_not_in_clone(pr))
    assert run.exit_code == GIT_ERROR_EXIT, run.stderr
    return run


@pytest.mark.parametrize("kind", NO_CONFIG_RUNS)
def test_p4_s5_ac11_with_gate_on_a_no_config_run_doesnt_create_the_summary(
    tmp_path, capsys, monkeypatch, kind
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    monkeypatch.setenv(GATE_VARIABLE, "on")

    run = no_config_run(pr, kind)

    assert "write_summary" not in [c.name for c in run.calls]
    assert pr.github.summary() is None


@pytest.mark.parametrize("kind", NO_CONFIG_RUNS)
def test_p4_s5_ac11_with_gate_on_a_no_config_run_doesnt_recreate_a_deleted_summary(
    tmp_path, capsys, monkeypatch, kind
):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])
    pr.people.delete_summary()
    monkeypatch.setenv(GATE_VARIABLE, "on")
    pr.push({OTHER_FILE: "other = 1\n"})

    run = no_config_run(pr, kind)

    assert "write_summary" not in [c.name for c in run.calls]
    assert pr.github.summary() is None


@pytest.mark.parametrize("kind", NO_CONFIG_RUNS)
def test_p4_s5_ac11_a_gate_marker_alone_stops_a_no_config_run_editing_the_summary(
    tmp_path, capsys, monkeypatch, kind
):
    """`REVIEW_BOT_GATE` unset, but the previous marker has `gate=`."""
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])
    assert pr.marker()["gate"]
    before = pr.summary()
    pr.push({OTHER_FILE: "other = 1\n"})

    run = no_config_run(pr, kind)

    assert "write_summary" not in [c.name for c in run.calls]
    assert pr.summary() == before


@pytest.mark.parametrize("field", ["more", "tampered"])
def test_p4_s5_ac11_more_or_tampered_alone_stops_a_no_config_run_editing_the_summary(
    tmp_path, capsys, monkeypatch, field
):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=DOMAIN_RULE)
    pr.push({PRICES: text(PRICES_V1)})
    entries = ",".join(f"{n:016x}:{AI_LOGIC}" for n in range(25))
    extra = f" gate={entries} more=3" if field == "more" else " tampered"
    hand_edited = f"Edited.\n\n<!-- review-bot:v1 summary reviewed=none{extra} -->"
    pr.people.edit_summary(hand_edited)

    run = no_config_run(pr, "invalid_config")

    assert "write_summary" not in [c.name for c in run.calls]


def test_p4_s5_ac11_without_gate_signals_a_no_config_run_writes_as_phase_3(
    tmp_path, capsys, monkeypatch
):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=DOMAIN_RULE)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])

    run = no_config_run(pr, "invalid_config")

    assert run.writes == ["write_summary"]
    assert (
        pr.summary().splitlines()[-1]
        == "<!-- review-bot:v1 summary reviewed=" + pr.marker()["reviewed"] + " -->"
    )


@pytest.mark.parametrize("value", ["yes", "ON", "1", "", "true"])
@pytest.mark.parametrize("mode", ["shadow", "comment"])
def test_p4_s5_interfaces_invalid_review_bot_gate_is_exit_2_every_run(
    tmp_path, capsys, monkeypatch, value, mode
):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=None)
    pr.push({APP: text(APP_V1)})
    monkeypatch.setenv(GATE_VARIABLE, value)

    run, provider = pr.review([], mode=mode)

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert GATE_VARIABLE in run.stderr


@pytest.mark.parametrize("value", ["on", "off"])
def test_p4_s5_interfaces_valid_review_bot_gate_values_run(tmp_path, capsys, monkeypatch, value):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    monkeypatch.setenv(GATE_VARIABLE, value)

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert gate(run)["decision"] == "block", "the variable doesn't turn the gate on or off"


# --- AC12: one audit record per run that reaches a decision -------------------------------------


def test_p4_s5_ac12_audit_records_the_gate_with_every_fingerprint(mixed):
    run, _ = mixed.review([], mode="shadow")

    record = gate_record(run)
    assert set(record) == GATE_RECORD_KEYS
    assert record["enabled"] is True
    assert (record["decision"], record["reason"]) == ("block", None)
    assert sorted(record["classes"]) == sorted([MONEY, AI_LOGIC])
    assert sorted(record["fingerprints"]) == sorted(fingerprints(run))
    assert record["policy_version"] == POLICY_VERSION


def test_p4_s5_ac12_gating_off_has_a_not_enabled_record(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch, config=None)
    monkeypatch.delenv(POLICY_VARIABLE)
    pr.push({APP: text(APP_V1)})

    run, _ = pr.review([])

    record = gate_record(run)
    assert record == {
        "enabled": False, "decision": "not_enabled", "reason": None, "classes": [],
        "fingerprints": [], "policy_version": BUILTIN_VERSION,
    }  # fmt: skip


def test_p4_s5_ac12_refused_run_has_a_no_config_record(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, _ = pr.review([], payload=draft(pr))

    record = gate_record(run)
    assert (record["enabled"], record["decision"], record["reason"]) == (
        None,
        "unjudged",
        "no_config",
    )
    assert record["policy_version"] == POLICY_VERSION


def test_p4_s5_ac12_a_run_ending_on_a_git_error_writes_no_report_or_gate_record(
    tmp_path, capsys, monkeypatch
):
    """It reaches no decision, so, as in Phase 3, there is no report to hold one."""
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})

    run, provider = pr.review([], payload=head_not_in_clone(pr))

    assert run.exit_code == GIT_ERROR_EXIT, run.stderr
    assert provider.requests == []
    assert not (run.out / "shadow.json").exists()
    audit = run.out / "audit.jsonl"
    if audit.exists():
        gate_records = [
            r for r in read_records(audit) if (r["stage"], r["event"]) == ("policy", "gate")
        ]
        assert gate_records == []


def test_p4_s5_ac12_record_holds_no_diff_text_or_path_secret(tmp_path, capsys, monkeypatch):
    pr = secret_pr(tmp_path, capsys, monkeypatch)
    pr.push({f"config/{aws_key(1)}.txt": "plain = 1\n"})

    run, _ = pr.review([])

    dumped = json.dumps(gate_record(run))
    assert aws_key(1) not in dumped
    assert "plain = 1" not in dumped


# --- AC13: a class that can never reach `high` is invalid ---------------------------------------


@pytest.mark.parametrize(
    ("rule", "severity"), [("swallowed-exception", "medium"), ("open-without-encoding", "low")]
)
@pytest.mark.parametrize("min_severity", ["high", "critical"])
def test_p4_s5_ac13_class_whose_rule_cant_reach_high_is_an_invalid_policy(  # noqa: PLR0913, PLR0917 - fixtures and parameters
    tmp_path, capsys, monkeypatch, rule, severity, min_severity
):
    pr = money_pr(
        tmp_path, capsys, monkeypatch, config=None, classes=(gate_class(rule, min_severity),)
    )
    pr.push({APP: text(APP_V1)})

    run, provider = pr.review([], mode="shadow")

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []
    assert rule in run.stderr
    assert severity in run.stderr
    assert not (run.out / "shadow.json").exists() or run.shadow["outcome"] == "failed"


@pytest.mark.parametrize(
    "bad",
    {
        "severity_medium": gate_class(MONEY, "medium"),
        "confidence_below_05": gate_class(MONEY, "high", "0.4"),
        "confidence_above_1": gate_class(MONEY, "high", "1.1"),
        "unknown_rule": gate_class("no-such-rule"),
        "unknown_category": gate_class("ai:nonsense"),
        "unknown_key": gate_class(MONEY) + 'colour = "red"\n',
        "no_approved_by": '[[gate.classes]]\nrule = "decimal-money"\napproved_on = 2026-11-01\n',
        "twice": gate_class(MONEY) + gate_class(MONEY),
    }.items(),
    ids=lambda item: item[0],
)
def test_p4_s5_policy_class_outside_the_allowed_range_is_invalid(
    tmp_path, capsys, monkeypatch, bad
):
    _, entry = bad
    pr = money_pr(tmp_path, capsys, monkeypatch, config=None, classes=(entry,))
    pr.push({APP: text(APP_V1)})

    run, provider = pr.review([], mode="shadow")

    assert run.exit_code == USAGE_EXIT, run.stderr
    assert provider.requests == []


def test_p4_s5_policy_class_defaults_are_critical_and_090(tmp_path, capsys, monkeypatch):
    """A secret class with no minimums: `critical`, 0.9, which a secret (critical, 1.0) meets."""
    pr = GatePR(
        tmp_path,
        capsys,
        monkeypatch,
        config=SECRET_CONFIG,
        classes=(gate_class(SECRET_CLASS, None, None),),
    )
    pr.push({KEYS: text(keys_lines())})

    run, _ = pr.review([], mode="shadow")

    assert run.exit_code == 0, run.stderr
    assert gate(run)["decision"] == "block"


# --- AC14: an edited bot inline comment is a tamper ---------------------------------------------


def test_p4_s5_ac14_edited_inline_comment_blocks_tampered_and_keeps_carried(ai_carry):
    pr = ai_carry.pr
    pr.people.edit_comment(ai_carry.a_comment.comment_id)

    run = ai_carry.next_push()

    assert (gate(run)["decision"], gate(run)["reason"]) == ("block", "tampered")
    assert sorted(fingerprints(run)) == sorted([ai_carry.a, ai_carry.b, ai_carry.c])
    assert pr.marker()["tampered"]


def test_p4_s5_ac14_edit_lowering_the_shown_severity_doesnt_launder_it(ai_carry):
    pr = ai_carry.pr
    body = ai_carry.a_comment.body.replace("**critical**", "**low**", 1)
    assert body != ai_carry.a_comment.body
    pr.people.edit_comment(ai_carry.a_comment.comment_id, body)

    run = ai_carry.next_push()

    assert gate(run)["reason"] == "tampered"
    assert ai_carry.a in fingerprints(run)


def test_p4_s5_ac14_the_next_run_takes_the_rebuild_path_and_stays_tampered(ai_carry):
    pr = ai_carry.pr
    pr.people.edit_comment(ai_carry.a_comment.comment_id)
    ai_carry.next_push()
    pr.push({OTHER_FILE: "other = 2\n"})

    run, _ = pr.review([])

    assert_full_review(run)
    assert (gate(run)["decision"], gate(run)["reason"]) == ("block", "tampered")
    assert pr.marker()["tampered"]


def test_p4_s5_ac14_edited_comment_of_a_rule_blocker_is_tampered_too(tmp_path, capsys, monkeypatch):
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(PRICES_V1)})
    pr.review([])
    pr.people.edit_comment(pr.comment_on(PRICES, 3).comment_id)
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert (gate(run)["decision"], gate(run)["reason"]) == ("block", "tampered")


# --- Limits: at most 25 marker entries, AI first; all in the report -----------------------------


def money_lines(count: int) -> list[str]:
    return ["from decimal import Decimal", *(f"price_{n} = {n}.99" for n in range(count))]


def test_p4_s5_limits_more_than_25_blockers_lists_25_ai_first_and_counts_the_rest(
    tmp_path, capsys, monkeypatch
):
    config = repo_config(MONEY, AI_LOGIC)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(MONEY_CLASS, AI_LOGIC_CLASS))
    pr.push({PRICES: text(money_lines(28)), APP: text(APP_V1)})

    run, _ = pr.review([ai(1), ai(EVAL_LINE)])

    assert run.exit_code == 0, run.stderr
    assert len(blockers(run)) == 30, "shadow/v1 lists all"
    assert len(gate_record(run)["fingerprints"]) == 30, "so does the audit record"
    marker = pr.marker()
    assert len(marker["gate"]) == MAX_MARKER_ENTRIES
    assert [c for _, c in marker["gate"][:2]] == [AI_LOGIC, AI_LOGIC], "ai: entries first"
    assert marker["more"] == 5
    assert gate(run)["decision"] == "block"
    assert "30" in pr.visible_summary(), "the summary states the total"


def test_p4_s5_limits_most_severe_first_among_the_listed(tmp_path, capsys, monkeypatch):
    config = repo_config(AI_LOGIC, domain_rule=None)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(AI_LOGIC_CLASS,))
    pr.push({APP: text(APP_V1)})
    first, _ = pr.review([model(1), model(EVAL_LINE, severity="critical")])

    critical = [b["fingerprint"] for b in blockers(first) if b["severity"] == "critical"]
    assert pr.marker()["gate"][0] == (critical[0], AI_LOGIC)


def test_p4_s5_limits_ai_blockers_beyond_25_arent_carried_and_the_summary_says_how_many(
    tmp_path, capsys, monkeypatch
):
    lines = [f"    step_{n} = compute({n})" for n in range(1, 31)]
    config = repo_config(AI_LOGIC, domain_rule=None)
    pr = GatePR(tmp_path, capsys, monkeypatch, config=config, classes=(AI_LOGIC_CLASS,))
    pr.push({APP: text(lines)})
    first, _ = pr.review([model(n) for n in range(1, 31)])
    assert len(blockers(first)) == 30
    assert pr.marker()["more"] == 5
    pr.push({OTHER_FILE: "other = 1\n"})

    run, _ = pr.review([])

    assert len(blockers(run)) == 25
    assert gate(run)["decision"] == "block"
    assert "5" in pr.visible_summary()


def test_p4_s5_limits_a_thousand_blockers_stay_under_60000_and_run_in_bounded_time(
    tmp_path, capsys, monkeypatch
):
    long_dir = "billing/" + "/".join(["d" * 24] * 4)  # over 100 characters, under MAX_PATH
    files = {f"{long_dir}/p{n}.py": text(money_lines(100)) for n in range(10)}
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push(files)

    started = time.monotonic()
    run, _ = pr.review([])
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert run.exit_code == 0, run.stderr
    assert len(blockers(run)) == 1_000
    assert len(pr.summary()) < SUMMARY_LIMIT
    assert len(pr.summary().rsplit("\n", 1)[-1]) <= 2_000, "the marker stays readable"
    assert pr.marker()["more"] == 975


def test_p4_s5_limits_whole_diff_checks_are_linear_on_a_large_diff(tmp_path, capsys, monkeypatch):
    """Close to the default `max_diff_bytes`, every line a money hit."""
    pr = money_pr(tmp_path, capsys, monkeypatch)
    pr.push({PRICES: text(money_lines(20_000))})

    started = time.monotonic()
    run, _ = pr.review([], mode="shadow")
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert run.exit_code == 0, run.stderr
