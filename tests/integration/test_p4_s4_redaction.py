r"""P4-S4: redact personal data before any vendor call.

Written from the spec's criteria, Limits and Interfaces, Phase 4 design.md (Redaction, Policy as
code, Interfaces) and ADR 0022, in parallel with the developer and before their code was read.

Every command runs in process through its public entry point, as the earlier suites drive it, with
`MockProvider`, so nothing reaches a model or the network:
- `review-bot`: `review_bot.cli.main([DIFF, "--provider", "mock", ...], provider=MockProvider(...))`
  (the `bot` fixture); context mode through P3-S1's and P3-S4's `Harness` over a real git
  repository.
- `pr shadow` and `pr run`: `review_bot.pr.cli.main([...], provider=...)`; linked issues come from
  P3-S9's local GitHub stub.
- `evals replay`: `review_bot.evals.main(["replay", ...])`.

What the model is sent is read from `MockProvider.requests` (system and user prompt) and
`MockProvider.tool_results`. A pattern case is one added line in a small diff: the line the
prompt holds is `+` and the line as sent.

Readings, each used in one place:
- **Counts.** `report/v1`'s `redaction` maps source to kind to count, as the spec's example. A
  source or kind with nothing masked may be left out or be 0 (`positive`); only the spec's own
  example is compared as written.
- **The audit record** `static`/`redaction` holds the counts as its data, or under one key
  (`record_counts`).
- **"The ledger's byte counts describe the text as returned"**: each masked address changes the
  tool call's `result_bytes`, and the report's `context.result_bytes`, by exactly the difference
  in UTF-8 bytes between the placeholder and the address, against the same call unmasked.
- **A loose case** (an address in a URL, or with `+tag`): the address is gone and a placeholder of
  its kind is there; which neighbouring characters an e-mail's local part takes isn't pinned.
- **Already masked text** that is "left as it is" counts nothing.

Test values: no credential appears in this file. AC10's AWS key is assembled at run time
(`aws_key`), as P3-S17's guard requires; e-mail addresses, phone numbers and IPv4 addresses are
literals, which the secret scanner doesn't report.
"""

import json
import shutil
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from github_stub import GitHubStub
from repo_paths import REPO_ROOT
from test_p1_s4_audit_log import aws_key, read_records
from test_p2_s2_review_pr_event import REPO_NAME, event
from test_p2_s6_comments_on import TOKEN
from test_p3_s1_local_review_with_repo_tools import (
    CHECKOUT,
    CHECKOUT_AT_C,
    GOLDEN,
    ContextRepo,
    make_repo,
    unwrap,
)
from test_p3_s4_finding_evidence import Harness as AnswerHarness
from test_p3_s4_finding_evidence import Review as AnswerReview
from test_p3_s8_pr_review_with_context import PrRunner, make_context_pull
from test_p3_s9_linked_issue_criteria import issue_text

from review_bot.evals import main as evals_main
from review_bot.findings import Finding
from review_bot.providers.mock import MockProvider
from review_bot.secret_scan import scan_secrets
from review_bot.settings import Settings

pytestmark = pytest.mark.integration

USAGE_EXIT = 2
FINDINGS_EXIT = 1
CLEAN_EXIT = 0
SECRET_EXIT = 16
INTERNAL_EXIT = 14
SAMPLE = REPO_ROOT / "Assignment" / "sample_diff.txt"
EVAL_CASES = REPO_ROOT / "tests" / "evals" / "cases"
POLICY_VARIABLE = "REVIEW_BOT_POLICY"
ALL_TOOLS = ("read", "grep", "glob")
# Generous: it catches a hang or super-linear matching, not a slow CI machine.
BOUND_S = 60.0
HOSTILE_LENGTH = 400_000

EMAIL = "[REDACTED:email]"
PHONE = "[REDACTED:phone]"
IPV4 = "[REDACTED:ipv4]"
PLACEHOLDERS = {"email": EMAIL, "phone": PHONE, "ipv4": IPV4}

# The spec's AC1 line.
JANE = "jane.doe@rightship.com"
AC1_LINE = f'contact = "{JANE}"'
AC1_MASKED = f'contact = "{EMAIL}"'

CONTACTS = "app/contacts.py"
ADDED_LINE = 2  # the new-side line of the one added line in `one_line_diff`


# --- The policy file ------------------------------------------------------------------------------


def policy_toml(
    kinds: Sequence[str] | None, *, version: str = "org-1", tools: Sequence[str] = ALL_TOOLS
) -> str:
    """A `policy/v1` file with all three tools and, unless `kinds` is None, `[redaction]`."""
    lines = [f'version = "{version}"', "[tools]", "allowed = " + json.dumps(list(tools))]
    if kinds is not None:
        lines += ["[redaction]", "kinds = " + json.dumps(list(kinds))]
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary working directory, outside CI, with no policy, audit or GitHub variable."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    for name in (
        "CI", "GITHUB_EVENT_NAME", POLICY_VARIABLE, "REVIEW_BOT_MAX_COST_USD",
        "REVIEW_BOT_AUDIT_LOG", "REVIEW_BOT_CONTEXT", "GITHUB_TOKEN", "GITHUB_API_URL",
        "GITHUB_GRAPHQL_URL", "GITHUB_REPOSITORY",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    return work


@pytest.fixture
def write_policy(work_dir: Path) -> Callable[..., Path]:
    def write(content: str, name: str = "p.toml") -> Path:
        path = work_dir / name
        path.write_bytes(content.encode("utf-8"))
        return path

    return write


@pytest.fixture
def policy_for(write_policy: Callable[..., Path]) -> Callable[..., Path]:
    """A policy file masking `kinds`."""
    made = 0

    def make(*kinds: str) -> Path:
        nonlocal made
        made += 1
        return write_policy(policy_toml(kinds), name=f"policy-{made}.toml")

    return make


# --- Diffs ----------------------------------------------------------------------------------------


def one_line_diff(text: str, path: str = CONTACTS) -> str:
    """A modified file whose one added line, new-side line 2, is `text`."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        f"+{text}\n"
        " VALUE = 1\n"
    )


def lines_diff(lines: Sequence[str], path: str = CONTACTS) -> str:
    """A new file holding `lines`, every one added, from new-side line 1."""
    body = "".join(f"+{line}\n" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..3333333\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}"
    )


@pytest.fixture
def write_diff(tmp_path: Path) -> Callable[..., Path]:
    made = 0

    def write(content: str) -> Path:
        nonlocal made
        made += 1
        path = tmp_path / f"change-{made}.diff"
        path.write_bytes(content.encode("utf-8"))
        return path

    return write


# --- Running review-bot ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Done:
    exit_code: int
    stdout: str
    stderr: str
    provider: MockProvider
    audit_log: Path

    @property
    def report(self) -> dict[str, Any]:
        document = json.loads(self.stdout)
        assert isinstance(document, dict), self.stdout
        assert document["schema"] == "report/v1"
        return document

    @property
    def request(self) -> Any:
        assert len(self.provider.requests) == 1, self.provider.requests
        return self.provider.requests[0]

    @property
    def prompt_lines(self) -> list[str]:
        return str(self.request.user_prompt).split("\n")

    @property
    def sent(self) -> str:
        """Everything the provider was sent: both prompts and every tool result."""
        parts = [self.request.system_prompt, self.request.user_prompt]
        return "\n".join([*map(str, parts), *map(str, self.provider.tool_results)])

    @property
    def log_text(self) -> str:
        return self.audit_log.read_text(encoding="utf-8") if self.audit_log.exists() else ""

    def records(self, stage: str, event_name: str) -> list[dict[str, Any]]:
        return [
            record["data"]
            for record in read_records(self.audit_log)
            if (record["stage"], record["event"]) == (stage, event_name)
        ]


@dataclass
class Bot:
    """`review-bot DIFF --provider mock --audit-log LOG FLAGS`, a fresh log for each run."""

    bot: Any
    tmp_path: Path
    runs: int = 0

    def run(
        self,
        diff: Path,
        *flags: str,
        findings: Sequence[Finding] = (),
        output: str = "json",
    ) -> Done:
        self.runs += 1
        audit_log = self.tmp_path / f"audit-{self.runs}.jsonl"
        provider = MockProvider(findings=list(findings))
        run = self.bot.run(
            str(diff),
            "--provider",
            "mock",
            "--format",
            output,
            "--audit-log",
            str(audit_log),
            *flags,
            provider=provider,
        )
        return Done(run.exit_code, run.stdout, run.stderr, provider, audit_log)


@pytest.fixture
def review(bot: Any, tmp_path: Path) -> Bot:
    return Bot(bot, tmp_path)


def contacts_finding(line: int = ADDED_LINE, rationale: str | None = None) -> Finding:
    return Finding(
        file=CONTACTS,
        line=line,
        severity="medium",
        category="security",
        rationale=rationale or "A personal contact is committed to the source; move it to config.",
        confidence=0.8,
    )


def positive(counts: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """`redaction` without any source or kind whose count is 0."""
    assert isinstance(counts, dict), counts
    kept: dict[str, dict[str, int]] = {}
    for source, kinds in counts.items():
        assert isinstance(kinds, dict), counts
        for kind, count in kinds.items():
            assert isinstance(count, int) and count >= 0, counts
            if count:
                kept.setdefault(source, {})[kind] = count
    return kept


def record_counts(data: Mapping[str, Any]) -> dict[str, Any]:
    """The counts an audit `static`/`redaction` record holds: its data, or one key's value."""
    if set(data) <= {"diff", "tools", "issues"}:
        return dict(data)
    nested = [value for value in data.values() if isinstance(value, dict)]
    assert len(nested) == 1, f"no single counts object in {data}"
    return dict(nested[0])


# --- The pattern catalogue ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Case:
    kind: str
    text: str  # the added line
    sent: str | None  # the line as sent, or None for a loose case
    gone: tuple[str, ...] = ()  # in a loose case: what mustn't be sent
    count: int = 1


def masked(kind: str, text: str, sent: str, count: int = 1) -> Case:
    return Case(kind, text, sent, count=count)


def loose(kind: str, text: str, *gone: str, count: int = 1) -> Case:
    return Case(kind, text, None, gone, count)


def kept(kind: str, text: str) -> Case:
    return Case(kind, text, text, count=0)


MASKED = {
    # AC1, and an address at each kind of boundary.
    "email-ac1": masked("email", AC1_LINE, AC1_MASKED),
    "email-whole-line": masked("email", JANE, EMAIL),
    "email-at-line-start": masked("email", f"{JANE} is the owner", f"{EMAIL} is the owner"),
    "email-at-line-end": masked("email", f"# owner: {JANE}", f"# owner: {EMAIL}"),
    "email-in-angle-brackets": masked(
        "email", "# Jane Doe <jane@rightship.com>", f"# Jane Doe <{EMAIL}>"
    ),
    "email-in-parentheses": masked("email", "# (jane@rightship.com)", f"# ({EMAIL})"),
    "email-before-a-full-stop": masked(
        "email", "# Write to jane@rightship.com.", f"# Write to {EMAIL}."
    ),
    "email-before-a-comma": masked("email", "to = jane@rightship.com, ops", f"to = {EMAIL}, ops"),
    "email-before-a-semicolon": masked("email", "to=jane@rightship.com;", f"to={EMAIL};"),
    "email-in-single-quotes": masked("email", "x = 'jane@rightship.com'", f"x = '{EMAIL}'"),
    "email-capitals": masked("email", 'x = "JANE.DOE@RIGHTSHIP.COM"', f'x = "{EMAIL}"'),
    "email-subdomain": masked("email", 'x = "ops@mail.rightship.com"', f'x = "{EMAIL}"'),
    "email-two-on-a-line": masked(
        "email",
        'to = ["jane@rightship.com", "ops@rightship.io"]',
        f'to = ["{EMAIL}", "{EMAIL}"]',
        count=2,
    ),
    "email-in-a-mailto-url": loose(
        "email", 'url = "mailto:jane@rightship.com?subject=Hi"', "jane@rightship.com"
    ),
    "email-in-a-url-query": loose(
        "email",
        'url = "https://rightship.com/u?to=jane@rightship.com&x=1"',
        "jane@rightship.com",
    ),
    "email-with-plus-tag": loose("email", 'x = "jane+billing@rightship.com"', "@rightship.com"),
    # Documentation look-alikes that aren't reserved names.
    "email-notexample-com": masked("email", 'x = "jane@notexample.com"', f'x = "{EMAIL}"'),
    "email-example-com-au": masked("email", 'x = "jane@example.com.au"', f'x = "{EMAIL}"'),
    "email-examples-com": masked("email", 'x = "jane@examples.com"', f'x = "{EMAIL}"'),
    "email-test-com": masked("email", 'x = "jane@test.com"', f'x = "{EMAIL}"'),
    "email-invalid-org": masked("email", 'x = "jane@invalid.org"', f'x = "{EMAIL}"'),
    # AC3: E.164, with single spaces or hyphens between groups, and its boundaries.
    "phone-ac3-spaces": masked("phone", "+61 2 9999 0000", PHONE),
    "phone-ac3-plain": masked("phone", 'tel = "+12025550143"', f'tel = "{PHONE}"'),
    "phone-ac3-hyphens": masked("phone", 'tel = "+1-202-555-0143"', f'tel = "{PHONE}"'),
    "phone-at-line-start": masked("phone", "+12025550143", PHONE),
    "phone-mid-sentence": masked("phone", "# call +61 2 9999 0000 now", f"# call {PHONE} now"),
    "phone-before-a-comma": masked("phone", 'x = ["+12025550143", 1]', f'x = ["{PHONE}", 1]'),
    "phone-in-parentheses": masked("phone", "# (+1-202-555-0143)", f"# ({PHONE})"),
    "phone-tel-url": masked("phone", 'href = "tel:+12025550143"', f'href = "tel:{PHONE}"'),
    "phone-8-digits": masked("phone", 'x = "+12345678"', f'x = "{PHONE}"'),
    "phone-15-digits": masked("phone", 'x = "+123456789012345"', f'x = "{PHONE}"'),
    "phone-two-on-a-line": masked(
        "phone", "a, b = '+12025550143', '+61 2 9999 0000'", f"a, b = '{PHONE}', '{PHONE}'", 2
    ),
    # AC4: IPv4 addresses outside the documentation ranges, and their boundaries.
    "ipv4-ac4-private": masked("ipv4", 'host = "10.0.0.12"', f'host = "{IPV4}"'),
    "ipv4-ac4-public": masked("ipv4", 'dns = "8.8.8.8"', f'dns = "{IPV4}"'),
    "ipv4-whole-line": masked("ipv4", "8.8.8.8", IPV4),
    "ipv4-in-a-url-with-a-port": masked(
        "ipv4", 'url = "http://10.0.0.12:8080/health"', f'url = "http://{IPV4}:8080/health"'
    ),
    "ipv4-before-a-semicolon": masked("ipv4", "ip=8.8.8.8;", f"ip={IPV4};"),
    "ipv4-in-parentheses": masked("ipv4", "# (10.0.0.12)", f"# ({IPV4})"),
    "ipv4-all-255": masked("ipv4", 'mask = "255.255.255.255"', f'mask = "{IPV4}"'),
    "ipv4-next-to-192-0-2": masked("ipv4", 'x = "192.0.3.5"', f'x = "{IPV4}"'),
    "ipv4-next-to-198-51-100": masked("ipv4", 'x = "198.51.101.7"', f'x = "{IPV4}"'),
    "ipv4-next-to-203-0-113": masked("ipv4", 'x = "203.0.114.9"', f'x = "{IPV4}"'),
    "ipv4-two-on-a-line": masked(
        "ipv4", "hosts = ['10.0.0.12', '8.8.4.4']", f"hosts = ['{IPV4}', '{IPV4}']", 2
    ),
}

KEPT = {
    # AC2: the reserved documentation names.
    "email-ac2-example-com": kept("email", 'x = "user@example.com"'),
    "email-ac2-mail-example-org": kept("email", 'x = "ops@mail.example.org"'),
    "email-ac2-test": kept("email", 'x = "a@b.test"'),
    "email-ac2-invalid": kept("email", 'x = "x@y.invalid"'),
    "email-example-net": kept("email", 'x = "dev@example.net"'),
    "email-dot-example": kept("email", 'x = "dev@host.example"'),
    "email-docs-example-com": kept("email", 'x = "help@docs.example.com"'),
    # Not addresses.
    "email-decorator": kept("email", "@property"),
    "email-route-decorator": kept("email", '@app.route("/users")'),
    "email-no-local-part": kept("email", 'x = "@rightship.com"'),
    "email-no-domain": kept("email", 'x = "jane@"'),
    "email-spaced-at": kept("email", "x = jane @ rightship.com"),
    "email-matrix-multiply": kept("email", "y = a @ b"),
    "email-only-ats": kept("email", 'x = "@@@"'),
    # AC3: not E.164.
    "phone-ac3-no-plus": kept("phone", 'x = "12025550143"'),
    "phone-ac3-too-short": kept("phone", 'x = "+123"'),
    "phone-ac3-16-digits": kept("phone", 'x = "+1234567890123456"'),
    "phone-20-digits": kept("phone", 'x = "+12345678901234567890"'),
    "phone-7-digits": kept("phone", 'x = "+1234567"'),
    "phone-addition": kept("phone", "total = a + 12"),
    "phone-double-space": kept("phone", 'x = "+61  2  9999  0000"'),
    "phone-plus-plus": kept("phone", "i++"),
    # AC4: documentation ranges, and what isn't an address.
    "ipv4-ac4-192-0-2": kept("ipv4", 'x = "192.0.2.5"'),
    "ipv4-ac4-198-51-100": kept("ipv4", 'x = "198.51.100.7"'),
    "ipv4-ac4-203-0-113": kept("ipv4", 'x = "203.0.113.9"'),
    "ipv4-192-0-2-edges": kept("ipv4", 'x = ["192.0.2.0", "192.0.2.255"]'),
    "ipv4-ac4-octet-999": kept("ipv4", 'x = "999.1.1.1"'),
    "ipv4-octet-256": kept("ipv4", 'x = "256.1.1.1"'),
    "ipv4-last-octet-300": kept("ipv4", 'x = "1.1.1.300"'),
    "ipv4-ac4-five-parts": kept("ipv4", 'x = "1.2.3.4.5"'),
    "ipv4-ac4-version": kept("ipv4", 'x = "v1.2.3"'),
    "ipv4-three-parts": kept("ipv4", 'version = "1.2.3"'),
    "ipv4-float": kept("ipv4", "rate = 1.25"),
}


def run_case(review: Bot, write_diff: Callable[..., Path], policy: Path, case: Case) -> Done:
    done = review.run(write_diff(one_line_diff(case.text)), "--policy", str(policy))
    assert done.exit_code in (CLEAN_EXIT, FINDINGS_EXIT), done.stderr
    return done


def assert_case_sent(done: Done, case: Case) -> None:
    lines = done.prompt_lines
    if case.sent is not None:
        assert "+" + case.sent in lines, (case.text, [x for x in lines if x.startswith("+")])
    else:
        added = [line for line in lines if line.startswith("+") and not line.startswith("+++")]
        assert len(added) == 1, added
        assert PLACEHOLDERS[case.kind] in added[0], added
        for text in case.gone:
            assert text not in done.sent, (text, added)


# --- AC1: an e-mail address is masked in place, and findings keep their anchors -----------------


def test_p4_s4_ac1_spec_example_prompt_holds_the_placeholder_not_the_address(
    review, write_diff, policy_for
):
    done = review.run(write_diff(one_line_diff(AC1_LINE)), "--policy", str(policy_for("email")))

    assert done.exit_code == CLEAN_EXIT, done.stderr
    assert "+" + AC1_MASKED in done.prompt_lines
    assert JANE not in done.sent
    assert "rightship.com" not in done.sent


def test_p4_s4_ac1_only_the_address_changes_in_the_prompt(review, write_diff, policy_for):
    """Masking is in place: the prompt is the unmasked one with the address replaced."""
    diff = write_diff(one_line_diff(AC1_LINE))

    plain = review.run(diff)
    chosen = review.run(diff, "--policy", str(policy_for("email")))

    assert chosen.request.system_prompt == plain.request.system_prompt
    plain_lines, chosen_lines = plain.prompt_lines, chosen.prompt_lines
    assert len(chosen_lines) == len(plain_lines)
    changed = [
        (before, after) for before, after in zip(plain_lines, chosen_lines, strict=True)
        if before != after and not before.startswith("<<<")
    ]  # fmt: skip
    assert changed == [("+" + AC1_LINE, "+" + AC1_MASKED)]


def test_p4_s4_ac1_finding_on_the_masked_line_keeps_its_fingerprint(review, write_diff, policy_for):
    diff = write_diff(one_line_diff(AC1_LINE))
    finding = contacts_finding()

    plain = review.run(diff, findings=[finding])
    chosen = review.run(diff, "--policy", str(policy_for("email")), findings=[finding])

    assert plain.exit_code == chosen.exit_code == FINDINGS_EXIT, chosen.stderr
    [before] = [f for f in plain.report["findings"] if f["source"] == "ai"]
    [after] = [f for f in chosen.report["findings"] if f["source"] == "ai"]
    assert (after["file"], after["line"]) == (CONTACTS, ADDED_LINE)
    assert after["fingerprint"] == before["fingerprint"]
    volatile = ("run_id", "usage", "policy", "redaction")
    assert {k: v for k, v in chosen.report.items() if k not in volatile} == {
        k: v for k, v in plain.report.items() if k not in volatile
    }


def test_p4_s4_ac1_findings_after_the_masked_line_keep_their_lines(review, write_diff, policy_for):
    """A line holding two addresses shrinks, but the next line's finding stays on its line."""
    lines = ['to = ["jane@rightship.com", "ops@rightship.io"]', "retries = 3", "timeout = 9"]
    diff = write_diff(lines_diff(lines))
    findings = [contacts_finding(line=n, rationale=f"Line {n} is wrong.") for n in (1, 2, 3)]

    plain = review.run(diff, findings=findings)
    chosen = review.run(diff, "--policy", str(policy_for("email")), findings=findings)

    def anchors(done: Done) -> list[tuple[int, str]]:
        return sorted((f["line"], f["fingerprint"]) for f in done.report["findings"])

    assert [line for line, _ in anchors(chosen)] == [1, 2, 3]
    assert anchors(chosen) == anchors(plain)


@pytest.mark.parametrize("where", ["context", "removed"])
def test_p4_s4_ac1_context_and_removed_lines_are_masked_too(review, write_diff, policy_for, where):
    """The whole reviewable diff is sent, so every line of it is masked (design, Redaction)."""
    marker = " " if where == "context" else "-"
    new_count = 2 if where == "context" else 1
    diff = (
        f"diff --git a/{CONTACTS} b/{CONTACTS}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{CONTACTS}\n"
        f"+++ b/{CONTACTS}\n"
        f"@@ -1,2 +1,{new_count} @@\n"
        f"{marker}{AC1_LINE}\n"
        "-VALUE = 1\n"
        "+VALUE = 2\n"
    )

    done = review.run(write_diff(diff), "--policy", str(policy_for("email")))

    assert done.exit_code == CLEAN_EXIT, done.stderr
    assert marker + AC1_MASKED in done.prompt_lines
    assert JANE not in done.sent


# --- AC2, AC3, AC4: the catalogue -----------------------------------------------------------------


@pytest.mark.parametrize("case", MASKED.values(), ids=MASKED.keys())
def test_p4_s4_ac2_ac3_ac4_value_is_masked_in_place_by_kind(review, write_diff, policy_for, case):
    done = run_case(review, write_diff, policy_for(case.kind), case)

    assert_case_sent(done, case)
    assert positive(done.report["redaction"]) == {"diff": {case.kind: case.count}}


@pytest.mark.parametrize("case", KEPT.values(), ids=KEPT.keys())
def test_p4_s4_ac2_ac3_ac4_value_is_sent_as_written(review, write_diff, policy_for, case):
    done = run_case(review, write_diff, policy_for(case.kind), case)

    assert_case_sent(done, case)
    assert positive(done.report["redaction"]) == {}


def test_p4_s4_ac3_phone_at_the_start_of_the_added_line_follows_the_diffs_plus(
    review, write_diff, policy_for
):
    """The diff line is `++12025550143`: the content's `+` starts the number."""
    done = review.run(
        write_diff(one_line_diff("+12025550143")), "--policy", str(policy_for("phone"))
    )

    assert "+" + PHONE in done.prompt_lines
    assert "12025550143" not in done.sent


def test_p4_s4_ac1_address_in_a_hunk_headers_section_text_is_masked(review, write_diff, policy_for):
    """Git copies the enclosing line after `@@ ... @@`; it is diff text sent like any other."""
    diff = one_line_diff("x = 1").replace(
        "@@ -1,2 +1,3 @@\n", f"@@ -1,2 +1,3 @@ def notify(to={JANE}):\n"
    )

    done = review.run(write_diff(diff), "--policy", str(policy_for("email")))

    assert done.exit_code == CLEAN_EXIT, done.stderr
    assert JANE not in done.sent
    assert positive(done.report["redaction"]) == {"diff": {"email": 1}}


def test_p4_s4_ac1_address_on_a_crlf_line_is_masked_and_the_cr_kept(review, write_diff, policy_for):
    diff = one_line_diff(AC1_LINE + "\r")

    done = review.run(write_diff(diff), "--policy", str(policy_for("email")))

    assert JANE not in done.sent
    assert "+" + AC1_MASKED + "\r" in done.prompt_lines


@pytest.mark.parametrize(
    ("kind", "others"),
    [("email", ("phone", "ipv4")), ("phone", ("email", "ipv4")), ("ipv4", ("email", "phone"))],
)
def test_p4_s4_ac2_ac3_ac4_only_the_policys_kinds_are_masked(
    review, write_diff, policy_for, kind, others
):
    line = f'x = ["{JANE}", "+12025550143", "10.0.0.12"]'
    values = {"email": JANE, "phone": "+12025550143", "ipv4": "10.0.0.12"}

    done = review.run(write_diff(one_line_diff(line)), "--policy", str(policy_for(kind)))

    expected = line.replace(values[kind], PLACEHOLDERS[kind])
    assert "+" + expected in done.prompt_lines
    for other in others:
        assert values[other] in done.sent
    assert positive(done.report["redaction"]) == {"diff": {kind: 1}}


def test_p4_s4_ac2_ac3_ac4_all_three_kinds_on_one_line(review, write_diff, policy_for):
    line = f'x = ["{JANE}", "+12025550143", "10.0.0.12", "user@example.com", "192.0.2.5"]'

    done = review.run(
        write_diff(one_line_diff(line)), "--policy", str(policy_for("email", "phone", "ipv4"))
    )

    assert f'+x = ["{EMAIL}", "{PHONE}", "{IPV4}", "user@example.com", "192.0.2.5"]' in (
        done.prompt_lines
    )
    assert positive(done.report["redaction"]) == {"diff": {"email": 1, "phone": 1, "ipv4": 1}}


@pytest.mark.parametrize("kind", ["email", "phone", "ipv4"])
def test_p4_s4_limits_text_already_masked_is_left_as_it_is(review, write_diff, policy_for, kind):
    line = f'x = "{PLACEHOLDERS[kind]}"  # {EMAIL} {PHONE} {IPV4}'

    done = review.run(
        write_diff(one_line_diff(line)), "--policy", str(policy_for("email", "phone", "ipv4"))
    )

    assert "+" + line in done.prompt_lines
    assert positive(done.report["redaction"]) == {}


@pytest.mark.parametrize(
    "kinds",
    [["name"], ["Email"], ["ipv6"], ["email", "postal"], [""], [1]],
    ids=["name", "capitalised", "ipv6", "one-unknown-of-two", "empty-string", "number"],
)
def test_p4_s4_ac_policy_unknown_kind_exits_2_before_the_diff_naming_the_file(
    review, write_policy, work_dir, kinds
):
    path = write_policy(
        'version = "org-1"\n[redaction]\nkinds = ' + json.dumps(kinds) + "\n", name="bad.toml"
    )

    done = review.run(work_dir / "absent.diff", "--policy", str(path))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "bad.toml" in done.stderr
    named = [str(kind) for kind in kinds if kind not in ("email",)] + ["kinds"]
    assert any(text and text in done.stderr for text in named), done.stderr
    assert done.provider.requests == []


@pytest.mark.parametrize(
    "section",
    ['[redaction]\nkinds = "email"\n', '[redaction]\nkinds = ["email"]\nfoo = 1\n'],
    ids=["not-a-list", "unknown-key"],
)
def test_p4_s4_ac_policy_malformed_redaction_section_exits_2(
    review, write_policy, work_dir, section
):
    path = write_policy('version = "org-1"\n' + section, name="bad.toml")

    done = review.run(work_dir / "absent.diff", "--policy", str(path))

    assert done.exit_code == USAGE_EXIT, done.stderr
    assert "bad.toml" in done.stderr
    assert done.provider.requests == []


@pytest.mark.parametrize("kinds", [["email"], ["email", "phone", "ipv4"], []])
def test_p4_s4_ac_policy_known_kinds_are_accepted(review, write_policy, kinds):
    path = write_policy(policy_toml(kinds))

    done = review.run(SAMPLE, "--policy", str(path))

    assert done.exit_code == FINDINGS_EXIT, done.stderr  # the sample's rule findings


# --- AC5: tool results and linked issues ----------------------------------------------------------

TEAM = "team/contacts.py"
TEAM_TEXT = (
    '"""Who looks after billing."""\n'
    f'OWNER = "{JANE}"\n'
    'BACKUP = "ops@rightship.io"\n'
    'DOCS = "user@example.com"\n'
    "PAGER = 3\n"
)
READ_TEAM = ("read", {"path": TEAM, "start_line": 1, "line_count": 5})
GREP_OWNER = ("grep", {"literal": "OWNER"})
GREP_ADDRESS = ("grep", {"literal": JANE})
# The bytes each masked address saves in a result.
SAVED = {
    JANE: len(JANE.encode()) - len(EMAIL.encode()),
    "ops@rightship.io": len(b"ops@rightship.io") - len(EMAIL.encode()),
}


@dataclass
class ContextBot:
    """`review-bot SAMPLE --provider mock --context-repo REPO --format json` with tool calls."""

    review: Bot

    def run(
        self, repo: ContextRepo, tool_calls: Sequence[tuple[str, dict[str, object]]], *flags: str
    ) -> Done:
        self.review.runs += 1
        audit_log = self.review.tmp_path / f"context-audit-{self.review.runs}.jsonl"
        provider = MockProvider(findings=[], tool_calls=list(tool_calls))
        run = self.review.bot.run(
            str(SAMPLE), "--provider", "mock", "--format", "json", "--audit-log", str(audit_log),
            "--context-repo", str(repo.path), *flags, provider=provider,
        )  # fmt: skip
        return Done(run.exit_code, run.stdout, run.stderr, provider, audit_log)


@pytest.fixture
def context_bot(review: Bot) -> ContextBot:
    return ContextBot(review)


@pytest.fixture
def team_repo(tmp_path: Path) -> ContextRepo:
    return make_repo(tmp_path / "team-repo", {TEAM: TEAM_TEXT})


def bodies(done: Done) -> list[str]:
    return [unwrap(result) for result in done.provider.tool_results]


def test_p4_s4_ac5_read_returns_the_file_masked(context_bot, team_repo, policy_for):
    done = context_bot.run(team_repo, [READ_TEAM], "--policy", str(policy_for("email")))

    assert done.exit_code == FINDINGS_EXIT, done.stderr  # the sample's rule findings
    [body] = bodies(done)
    assert f'2\tOWNER = "{EMAIL}"' in body.split("\n"), body
    assert f'3\tBACKUP = "{EMAIL}"' in body.split("\n"), body
    assert '4\tDOCS = "user@example.com"' in body.split("\n"), body
    assert JANE not in done.sent
    assert "ops@rightship.io" not in done.sent


def test_p4_s4_ac5_grep_returns_matching_lines_masked(context_bot, team_repo, policy_for):
    done = context_bot.run(
        team_repo, [GREP_OWNER, GREP_ADDRESS], "--policy", str(policy_for("email"))
    )

    owner, address = bodies(done)
    assert f'{TEAM}:2:OWNER = "{EMAIL}"' in owner.split("\n"), owner
    assert JANE not in address, address
    assert JANE not in done.sent


def test_p4_s4_ac5_ledger_spans_and_bytes_describe_the_masked_result(
    context_bot, team_repo, policy_for
):
    plain = context_bot.run(team_repo, [READ_TEAM, GREP_OWNER])
    chosen = context_bot.run(
        team_repo, [READ_TEAM, GREP_OWNER], "--policy", str(policy_for("email"))
    )

    plain_calls = plain.records("context", "tool_call")
    chosen_calls = chosen.records("context", "tool_call")
    assert [c["spans"] for c in chosen_calls] == [c["spans"] for c in plain_calls]
    saved = [SAVED[JANE] + SAVED["ops@rightship.io"], SAVED[JANE]]
    assert [c["result_bytes"] for c in chosen_calls] == [
        c["result_bytes"] - s for c, s in zip(plain_calls, saved, strict=True)
    ]
    assert chosen.report["context"]["result_bytes"] == sum(c["result_bytes"] for c in chosen_calls)
    for call, result in zip(chosen_calls, chosen.provider.tool_results, strict=True):
        body = unwrap(result)
        assert call["result_bytes"] in (len(body.encode()), len(str(result).encode())), call


def test_p4_s4_ac5_read_lines_keep_their_numbers(context_bot, team_repo, policy_for):
    plain = context_bot.run(team_repo, [READ_TEAM])
    chosen = context_bot.run(team_repo, [READ_TEAM], "--policy", str(policy_for("email")))

    def numbers(done: Done) -> list[str]:
        return [line.split("\t", 1)[0] for line in bodies(done)[0].split("\n") if "\t" in line]

    assert numbers(chosen) == numbers(plain) == ["1", "2", "3", "4", "5"]


def test_p4_s4_ac5_report_counts_the_tool_source(context_bot, team_repo, policy_for):
    done = context_bot.run(team_repo, [READ_TEAM], "--policy", str(policy_for("email")))

    assert positive(done.report["redaction"]) == {"tools": {"email": 2}}


def test_p4_s4_ac5_without_a_policy_the_tool_result_is_unchanged(context_bot, team_repo):
    done = context_bot.run(team_repo, [READ_TEAM])

    [body] = bodies(done)
    assert f'2\tOWNER = "{JANE}"' in body.split("\n"), body
    assert done.report["redaction"] == {}


# Linked issues, through `pr run --context repo` and P3-S9's GitHub stub.

ISSUE_TITLE = f"Billing contact {JANE} is wrong"
ISSUE_BODY = "Call +61 2 9999 0000 or write to ops@rightship.io.\nDocs: user@example.com"


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[GitHubStub]:
    with GitHubStub() as server:
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
        monkeypatch.setenv("GITHUB_API_URL", server.url)
        yield server


@pytest.fixture
def pr(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> PrRunner:
    return PrRunner(capsys, tmp_path)


def run_with_issue(
    pr: PrRunner, tmp_path: Path, stub: GitHubStub, *flags: str
) -> tuple[Any, MockProvider]:
    pull, _ = make_context_pull(tmp_path / "clone", {CHECKOUT: CHECKOUT_AT_C})
    stub.add_linked_issue(12, title=ISSUE_TITLE, body=ISSUE_BODY)
    payload = event(pull.base, pull.head)
    payload["pull_request"]["body"] = "Closes #12"
    for holder in (payload["repository"], payload["pull_request"]["base"]["repo"]):
        holder["full_name"] = REPO_NAME
    provider = MockProvider(findings=[])
    run = pr.run(payload, pull.repo.path, "--context", "repo", *flags, provider=provider)
    return run, provider


def test_p4_s4_ac5_linked_issue_title_and_body_are_masked_before_sending(
    pr, tmp_path, stub, policy_for
):
    run, provider = run_with_issue(
        pr, tmp_path, stub, "--policy", str(policy_for("email", "phone"))
    )

    assert run.exit_code == 0, run.stderr
    [request] = provider.requests
    text = issue_text(str(request.user_prompt), 12)
    assert f"Billing contact {EMAIL} is wrong" in text, text
    assert f"Call {PHONE} or write to {EMAIL}." in text, text
    assert "user@example.com" in text
    for value in (JANE, "ops@rightship.io", "+61 2 9999 0000", "9999 0000"):
        assert value not in str(request.user_prompt), value
        assert value not in str(request.system_prompt), value
    assert positive(run.report["redaction"]) == {"issues": {"email": 2, "phone": 1}}


def test_p4_s4_ac5_without_a_policy_the_issue_is_sent_as_written(pr, tmp_path, stub):
    run, provider = run_with_issue(pr, tmp_path, stub)

    [request] = provider.requests
    assert JANE in issue_text(str(request.user_prompt), 12)
    assert run.report["redaction"] == {}


# --- AC6: counts in the report and the audit log, and no masked value in any output ---------------

AC6_LINES = [
    f'OWNER = "{JANE}"',
    'BACKUP = "ops@rightship.io"',
    'PAGER = "+61 2 9999 0000"',
    'GATEWAY = "10.0.0.12"',
    'DOCS = "user@example.com"',
]
AC6_VALUES = (JANE, "ops@rightship.io", "+61 2 9999 0000", "9999 0000", "10.0.0.12")
AC6_COUNTS = {"diff": {"email": 2, "phone": 1, "ipv4": 1}}


def ac6_findings() -> list[Finding]:
    """A finding on each masked line, so every output has something to say about them."""
    return [contacts_finding(line=n, rationale=f"Line {n} holds personal data.") for n in (1, 3, 4)]


def assert_no_masked_value(*outputs: str) -> None:
    for output in outputs:
        for value in AC6_VALUES:
            assert value not in output, (value, output[:400])


def test_p4_s4_ac6_spec_example_counts_by_source_and_kind(review, write_diff, policy_for):
    done = review.run(write_diff(one_line_diff(AC1_LINE)), "--policy", str(policy_for("email")))

    assert done.report["redaction"] == {"diff": {"email": 1}}


def test_p4_s4_ac6_report_redaction_is_appended_after_policy(review, write_diff, policy_for):
    done = review.run(write_diff(one_line_diff(AC1_LINE)), "--policy", str(policy_for("email")))

    keys = list(done.report)
    assert keys[-2:] == ["policy", "redaction"], keys


def test_p4_s4_ac6_counts_every_kind(review, write_diff, policy_for):
    done = review.run(
        write_diff(lines_diff(AC6_LINES)), "--policy", str(policy_for("email", "phone", "ipv4"))
    )

    assert positive(done.report["redaction"]) == AC6_COUNTS


def test_p4_s4_ac6_one_static_redaction_audit_record_with_the_same_counts(
    review, write_diff, policy_for
):
    done = review.run(
        write_diff(lines_diff(AC6_LINES)), "--policy", str(policy_for("email", "phone", "ipv4"))
    )

    [data] = done.records("static", "redaction")
    assert positive(record_counts(data)) == positive(done.report["redaction"]) == AC6_COUNTS


def test_p4_s4_ac6_counts_add_up_across_diff_tools_and_issues(pr, tmp_path, stub, write_policy):
    """A PR whose diff, a read file and a linked issue each hold an address."""
    pull, _ = make_context_pull(tmp_path / "clone", {CHECKOUT: CHECKOUT_AT_C, TEAM: TEAM_TEXT})
    stub.add_linked_issue(12, title=ISSUE_TITLE, body=ISSUE_BODY)
    payload = event(pull.base, pull.head)
    payload["pull_request"]["body"] = "Closes #12"
    provider = MockProvider(findings=[], tool_calls=[("read", {"path": TEAM, "line_count": 5})])
    path = write_policy(policy_toml(["email"]))

    run = pr.run(
        payload, pull.repo.path, "--context", "repo", "--policy", str(path), provider=provider
    )

    assert run.exit_code == 0, run.stderr
    expected = {"diff": {"email": 2}, "tools": {"email": 2}, "issues": {"email": 2}}
    assert positive(run.report["redaction"]) == expected
    [data] = [
        record["data"]
        for record in read_records(run.out / "audit.jsonl")
        if (record["stage"], record["event"]) == ("static", "redaction")
    ]
    assert positive(record_counts(data)) == expected
    assert JANE not in "\n".join(
        [str(request.user_prompt) for request in provider.requests]
        + [str(result) for result in provider.tool_results]
    )


@pytest.mark.parametrize("output", ["json", "text", "markdown"])
def test_p4_s4_ac6_no_masked_value_in_stdout_stderr_or_the_audit_log(
    review, write_diff, policy_for, output
):
    done = review.run(
        write_diff(lines_diff(AC6_LINES)),
        "--policy",
        str(policy_for("email", "phone", "ipv4")),
        findings=ac6_findings(),
        output=output,
    )

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert_no_masked_value(done.stdout, done.stderr, done.log_text)


def test_p4_s4_ac6_pr_shadow_writes_no_masked_value(capsys, tmp_path, write_diff, policy_for):
    from review_bot.pr import cli as pr_cli  # noqa: PLC0415 - as the earlier suites import it

    out = tmp_path / "shadow-out"
    diff = write_diff(lines_diff(AC6_LINES))
    provider = MockProvider(findings=ac6_findings())
    policy = policy_for("email", "phone", "ipv4")
    capsys.readouterr()

    exit_code = pr_cli.main(
        ["shadow", str(diff), "--out", str(out), "--provider", "mock", "--policy", str(policy)],
        provider=provider,
    )

    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    written = {path.name: path.read_text(encoding="utf-8") for path in out.iterdir()}
    assert {"shadow.json", "shadow.md", "audit.jsonl"} <= set(written), sorted(written)
    assert_no_masked_value(captured.out, captured.err, *written.values())
    document = json.loads(written["shadow.json"])
    assert positive(document["report"]["redaction"]) == AC6_COUNTS
    assert len(document["comments"]) >= 1, "the findings on masked lines are still reported"


def test_p4_s4_ac6_context_review_writes_no_value_from_a_tool_result(
    context_bot, team_repo, policy_for
):
    done = context_bot.run(
        team_repo, [READ_TEAM, GREP_ADDRESS], "--policy", str(policy_for("email"))
    )

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    for text in (done.stdout, done.stderr, done.log_text):
        assert JANE not in text
        assert "ops@rightship.io" not in text


def test_p4_s4_ac6_counts_only_never_the_values_in_the_audit_record(review, write_diff, policy_for):
    done = review.run(
        write_diff(lines_diff(AC6_LINES)), "--policy", str(policy_for("email", "phone", "ipv4"))
    )

    [data] = done.records("static", "redaction")
    assert_no_masked_value(json.dumps(data))
    assert "rightship" not in json.dumps(data)


# --- AC7: the built-in policy masks nothing -------------------------------------------------------


def test_p4_s4_ac7_builtin_report_redaction_is_empty(review):
    done = review.run(SAMPLE)

    assert done.exit_code == FINDINGS_EXIT, done.stderr
    assert done.report["redaction"] == {}


def test_p4_s4_ac7_builtin_sample_prompts_are_phase_3s_goldens(review):
    done = review.run(SAMPLE)

    system = (GOLDEN / "p1_v1_system_prompt.golden.txt").read_bytes().decode("utf-8")
    user = (GOLDEN / "p1_v1_sample_user_prompt.golden.txt").read_bytes().decode("utf-8")
    assert done.request.system_prompt == system
    assert done.request.user_prompt + "\n" == user


def test_p4_s4_ac7_builtin_sends_personal_data_as_written(review, write_diff):
    done = review.run(write_diff(lines_diff(AC6_LINES)))

    for line in AC6_LINES:
        assert "+" + line in done.prompt_lines
    assert "[REDACTED:" not in done.sent
    assert done.report["redaction"] == {}


@pytest.mark.parametrize(
    "content",
    [policy_toml(None), policy_toml([])],
    ids=["no-redaction-section", "empty-kinds"],
)
def test_p4_s4_ac7_policy_without_kinds_sends_the_same_bytes_as_builtin(
    review, write_diff, write_policy, content
):
    diff = write_diff(lines_diff(AC6_LINES))

    plain = review.run(diff)
    chosen = review.run(diff, "--policy", str(write_policy(content)))

    assert (chosen.request.system_prompt, chosen.request.user_prompt) == (
        plain.request.system_prompt,
        plain.request.user_prompt,
    )
    assert chosen.report["redaction"] == {}


def test_p4_s4_ac7_builtin_context_review_sends_the_same_bytes_as_no_kinds(
    context_bot, team_repo, write_policy
):
    plain = context_bot.run(team_repo, [READ_TEAM, GREP_OWNER])
    chosen = context_bot.run(
        team_repo, [READ_TEAM, GREP_OWNER], "--policy", str(write_policy(policy_toml(None)))
    )

    assert chosen.sent == plain.sent
    assert JANE in plain.sent


def test_p4_s4_ac7_policy_with_kinds_and_no_match_sends_the_same_bytes(
    review, write_diff, policy_for
):
    """The sample has `@@` hunk headers, `@`-free code and dotted numbers, but nothing to mask."""
    plain = review.run(SAMPLE)
    chosen = review.run(SAMPLE, "--policy", str(policy_for("email", "phone", "ipv4")))

    assert (chosen.request.system_prompt, chosen.request.user_prompt) == (
        plain.request.system_prompt,
        plain.request.user_prompt,
    )
    assert positive(chosen.report["redaction"]) == {}


# --- AC8: a suggestion holding a placeholder is dropped `redacted` --------------------------------

FIX_LINE = 14  # the sample's added line, as P3-S12's suite anchors on it
INVOICE = "billing/invoice.py"


def suggestion_answer(suggestion: str) -> str:
    finding = {
        "file": INVOICE,
        "line": FIX_LINE,
        "severity": "high",
        "category": "logic",
        "rationale": "Tax is added here and again at checkout, so it is charged twice.",
        "confidence": 0.8,
        "suggestion": suggestion,
    }
    return json.dumps({"findings": [finding]})


@pytest.fixture
def answer_harness(bot: Any, tmp_path: Path) -> AnswerHarness:
    return AnswerHarness(bot, tmp_path)


@pytest.fixture
def plain_repo(tmp_path: Path) -> ContextRepo:
    return make_repo(tmp_path / "plain-repo")


def ai_entry(review: AnswerReview) -> dict[str, Any]:
    assert review.exit_code == FINDINGS_EXIT, review.stderr
    entry = review.ai_entry
    assert (entry["file"], entry["line"]) == (INVOICE, FIX_LINE), entry
    return entry


REDACTED_SUGGESTIONS = {
    "email": f'        return notify("{EMAIL}")',
    "ipv4": f'        host = "{IPV4}"',
    "phone": f"        # call {PHONE}",
    "open-bracket-only": '        marker = "[REDACTED:"',
}


@pytest.mark.parametrize(
    "suggestion", REDACTED_SUGGESTIONS.values(), ids=REDACTED_SUGGESTIONS.keys()
)
def test_p4_s4_ac8_context_suggestion_with_a_placeholder_is_dropped_redacted(
    answer_harness, plain_repo, policy_for, suggestion
):
    review = answer_harness.run(
        suggestion_answer(suggestion),
        "--context-repo",
        str(plain_repo.path),
        "--policy",
        str(policy_for("email", "phone", "ipv4")),
    )

    entry = ai_entry(review)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == "redacted"


def test_p4_s4_ac8_verify_record_carries_redacted(answer_harness, plain_repo, policy_for):
    review = answer_harness.run(
        suggestion_answer(REDACTED_SUGGESTIONS["email"]),
        "--context-repo",
        str(plain_repo.path),
        "--policy",
        str(policy_for("email")),
    )

    [record] = [
        data
        for data in review.records("verify", "checked")
        if data.get("file") == INVOICE and data.get("line") == FIX_LINE
    ]
    assert record["suggestion_dropped"] == "redacted", record


def test_p4_s4_ac8_diff_mode_keeps_not_requested(answer_harness, policy_for):
    review = answer_harness.run(
        suggestion_answer(REDACTED_SUGGESTIONS["email"]), "--policy", str(policy_for("email"))
    )

    entry = ai_entry(review)
    assert entry["suggestion"] is None
    assert entry["suggestion_dropped"] == "not_requested"


def test_p4_s4_ac8_control_suggestion_without_a_placeholder_is_kept(
    answer_harness, plain_repo, policy_for
):
    suggestion = "        return subtotal  # REDACTED is a word, not a placeholder"

    review = answer_harness.run(
        suggestion_answer(suggestion),
        "--context-repo",
        str(plain_repo.path),
        "--policy",
        str(policy_for("email")),
    )

    entry = ai_entry(review)
    assert entry["suggestion"] == suggestion
    assert entry["suggestion_dropped"] is None


def test_p4_s4_interfaces_suggestion_drop_reason_redacted():
    from review_bot.suggestion import SuggestionDropReason  # noqa: PLC0415 - the slice's seam

    assert SuggestionDropReason.REDACTED == "redacted"


def test_p4_s4_interfaces_redaction_kind_values():
    from review_bot.governance.redaction import RedactionKind  # noqa: PLC0415 - the slice's seam

    assert sorted(kind.value for kind in RedactionKind) == ["email", "ipv4", "phone"]


# --- AC9: evals replay rebuilds the prompts with the same masking ---------------------------------

MAIL_DIFF = lines_diff(['"""Who to contact."""', f'OWNER = "{JANE}"'])


@dataclass(frozen=True, slots=True)
class Replay:
    exit_code: int
    output: str


def run_replay(capsys: pytest.CaptureFixture[str], *args: str | Path) -> Replay:
    capsys.readouterr()
    try:
        exit_code = evals_main(["replay", *map(str, args)])
    except SystemExit as exc:  # argparse reports usage errors this way
        exit_code = exc.code if isinstance(exc.code, int) else USAGE_EXIT
    captured = capsys.readouterr()
    return Replay(exit_code, captured.out + captured.err)


def fresh_recording(case_dir: Path, case_id: str) -> Path:
    """The clean case's zero-findings control, with today's fields under no policy."""
    from review_bot.evals.replay import freshness_fields  # noqa: PLC0415 - P1-S14's reading

    control = EVAL_CASES / "clean" / "recordings" / "synthetic" / "zero_findings.json"
    data = json.loads(control.read_text(encoding="utf-8"))
    data |= {
        "case_id": case_id,
        "model": Settings.model_fields["model"].default,
        **freshness_fields(case_dir / "case.json"),
    }
    path = case_dir / "recordings" / "synthetic" / "fresh.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def clean_case(root: Path, case_id: str, diff: str | None) -> Path:
    """The committed clean case, or a copy of it reviewing `diff` instead."""
    case_dir = root / case_id
    case_dir.mkdir(parents=True)
    case = json.loads((EVAL_CASES / "clean" / "case.json").read_text(encoding="utf-8"))
    case["id"] = case_id
    if diff is None:
        shutil.copyfile(EVAL_CASES / "clean" / case["diff"], case_dir / case["diff"])
    else:
        case["diff"] = "change.diff"
        (case_dir / "change.diff").write_bytes(diff.encode("utf-8"))
    (case_dir / "case.json").write_text(json.dumps(case, indent=2), encoding="utf-8")
    return case_dir


def test_p4_s4_ac9_case_with_no_match_stays_fresh_under_the_policy(capsys, tmp_path, policy_for):
    root = tmp_path / "cases"
    recording = fresh_recording(clean_case(root, "clean", None), "clean")
    policy = policy_for("email", "phone", "ipv4")

    plain = run_replay(capsys, "--cases", root, "--recording", recording)
    chosen = run_replay(capsys, "--cases", root, "--recording", recording, "--policy", policy)

    assert plain.exit_code == CLEAN_EXIT, plain.output
    assert chosen.exit_code == CLEAN_EXIT, chosen.output
    assert "prompt_sha256" not in chosen.output
    assert "stale" not in chosen.output.lower()


def test_p4_s4_ac9_committed_sample_case_stays_fresh_under_the_policy(capsys, tmp_path, policy_for):
    root = tmp_path / "cases"
    shutil.copytree(EVAL_CASES / "sample", root / "sample")

    plain = run_replay(capsys, "--cases", root)
    chosen = run_replay(capsys, "--cases", root, "--policy", policy_for("email", "phone", "ipv4"))

    assert chosen.exit_code == plain.exit_code, chosen.output
    assert chosen.output == plain.output


def test_p4_s4_ac9_case_with_a_match_is_stale_naming_prompt_sha256(capsys, tmp_path, policy_for):
    root = tmp_path / "cases"
    recording = fresh_recording(clean_case(root, "mail", MAIL_DIFF), "mail")

    plain = run_replay(capsys, "--cases", root, "--recording", recording)
    chosen = run_replay(
        capsys, "--cases", root, "--recording", recording, "--policy", policy_for("email")
    )

    assert plain.exit_code == CLEAN_EXIT, plain.output
    assert chosen.exit_code == FINDINGS_EXIT, chosen.output
    assert "prompt_sha256" in chosen.output
    for field in ("case_sha256", "diff_sha256"):
        assert field not in chosen.output, chosen.output


def test_p4_s4_ac9_a_kind_the_case_doesnt_hold_keeps_it_fresh(capsys, tmp_path, policy_for):
    """The mail case holds an address but no phone number: a phone-only policy changes nothing."""
    root = tmp_path / "cases"
    recording = fresh_recording(clean_case(root, "mail", MAIL_DIFF), "mail")

    chosen = run_replay(
        capsys, "--cases", root, "--recording", recording, "--policy", policy_for("phone")
    )

    assert chosen.exit_code == CLEAN_EXIT, chosen.output
    assert "prompt_sha256" not in chosen.output


def test_p4_s4_ac9_freshness_under_the_policy_is_the_masked_prompts_hash(tmp_path, policy_for):
    """The fields a recording made under the policy would carry differ from the unmasked ones."""
    from review_bot.evals.replay import freshness_fields  # noqa: PLC0415 - P1-S14's reading
    from review_bot.governance.policy import parse_policy  # noqa: PLC0415 - design's Interfaces

    case_dir = clean_case(tmp_path / "cases", "mail", MAIL_DIFF)
    path = policy_for("email")
    policy = parse_policy(path.read_bytes(), source=str(path))

    plain = freshness_fields(case_dir / "case.json")
    chosen = freshness_fields(case_dir / "case.json", policy=policy)

    assert chosen["prompt_sha256"] != plain["prompt_sha256"]
    assert {k: v for k, v in chosen.items() if k != "prompt_sha256"} == {
        k: v for k, v in plain.items() if k != "prompt_sha256"
    }


# --- AC10: a secret still stops the review --------------------------------------------------------


@pytest.mark.parametrize(
    "kinds", [None, ["email"], ["email", "phone", "ipv4"]], ids=["builtin", "email", "all"]
)
def test_p4_s4_ac10_secret_in_the_diff_exits_16_whatever_the_policy(
    review, write_diff, write_policy, kinds
):
    key = aws_key(11)
    diff = write_diff(lines_diff([AC1_LINE, f'KEY = "{key}"  # owner {JANE}']))
    flags = () if kinds is None else ("--policy", str(write_policy(policy_toml(kinds))))

    done = review.run(diff, *flags)

    assert done.exit_code == SECRET_EXIT, done.stderr
    assert done.provider.requests == []
    assert key not in done.stdout + done.stderr + done.log_text


def test_p4_s4_ac10_secret_next_to_an_address_is_not_masked_away(review, write_diff, policy_for):
    """A key glued to an address is still a key: masking never runs before the secret scan."""
    key = aws_key(12)
    diff = write_diff(one_line_diff(f'x = "{JANE}:{key}"'))

    done = review.run(diff, "--policy", str(policy_for("email", "phone", "ipv4")))

    assert done.exit_code == SECRET_EXIT, done.stderr
    assert done.provider.requests == []


# --- Limits: hostile lines are handled in linear time --------------------------------------------

HOSTILE = {
    "at-signs": "@" * HOSTILE_LENGTH,
    "plus-signs": "+" * HOSTILE_LENGTH,
    "full-stops": "." * HOSTILE_LENGTH,
    "letter-full-stop": "a." * (HOSTILE_LENGTH // 2),
    "letter-at": "a@" * (HOSTILE_LENGTH // 2),
    "digit-full-stop": "1." * (HOSTILE_LENGTH // 2),
    "plus-digit-space": "+1 " * (HOSTILE_LENGTH // 3),
    "digit-hyphen": "+" + "1-" * (HOSTILE_LENGTH // 2),
    "local-part-no-domain": "a" * HOSTILE_LENGTH + "@",
    "domain-no-tld": "a@" + "b" * HOSTILE_LENGTH,
}


@pytest.mark.parametrize("line", HOSTILE.values(), ids=HOSTILE.keys())
def test_p4_s4_limits_hostile_line_is_masked_in_bounded_time(line):
    from review_bot.governance.redaction import (  # noqa: PLC0415 - the slice's seam
        RedactionKind,
        mask_personal_data,
    )

    started = time.monotonic()
    result = mask_personal_data(line, frozenset(RedactionKind))
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"{elapsed:.1f}s for a {len(line):,}-character line"
    assert "\n" not in result.text


@pytest.mark.parametrize("char", ["@", "+", "."], ids=["at-signs", "plus-signs", "full-stops"])
def test_p4_s4_limits_hostile_diff_line_is_reviewed_in_bounded_time(
    review, write_diff, policy_for, char
):
    diff = write_diff(one_line_diff(char * HOSTILE_LENGTH))

    started = time.monotonic()
    done = review.run(
        diff, "--policy", str(policy_for("email", "phone", "ipv4")), "--max-diff-bytes", "1000000"
    )
    elapsed = time.monotonic() - started

    assert elapsed < BOUND_S, f"{elapsed:.1f}s"
    assert done.exit_code in (CLEAN_EXIT, FINDINGS_EXIT), done.stderr
    assert positive(done.report["redaction"]) == {}


# --- This file holds no secret (P3-S17's guard) ---------------------------------------------------


def test_p4_s4_this_file_holds_nothing_the_secret_scanner_reports():
    text = Path(__file__).read_text(encoding="utf-8")

    assert scan_secrets(text) == ()
