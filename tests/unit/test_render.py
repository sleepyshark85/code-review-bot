import json

import pytest

from review_bot.diff_parser import FileDiff, SkipReason
from review_bot.findings import Category, Finding, ReviewedFinding, RuleId, Severity, Source
from review_bot.policy import Decision
from review_bot.prompt import PROMPT_VERSION
from review_bot.providers.base import TokenUsage
from review_bot.render import escape_unprintable, render_findings, render_json
from review_bot.report import PipelineResult
from review_bot.settings import Settings
from review_bot.verify import DropReason, ProposalCheck, VerifyResult

# Assembled, so no key marker is written literally.
KEY_END_MARKER = "-" * 5 + "END RSA " + "PRIVATE KEY" + "-" * 5
NO_USAGE = TokenUsage(input_tokens=0, output_tokens=0)
UNDECODABLE_PATH = b"docs/caf\xff.py".decode("utf-8", "surrogateescape")


def make_finding(
    *,
    file: str = "a.py",
    severity: Severity = Severity.HIGH,
    rationale: str = "Why.",
) -> Finding:
    return Finding(
        file=file,
        line=7,
        severity=severity,
        category=Category.LOGIC,
        rationale=rationale,
        confidence=0.876,
    )


def reviewed(
    finding: Finding | None = None,
    *,
    source: Source = Source.AI,
    rule: RuleId | None = None,
    fingerprint: str = "0123456789abcdef",
) -> ReviewedFinding:
    return ReviewedFinding(finding or make_finding(), source, rule, fingerprint)


def pipeline_result(
    *,
    reported: tuple[ReviewedFinding, ...] = (),
    below_threshold: tuple[ReviewedFinding, ...] = (),
    checks: tuple[ProposalCheck, ...] = (),
    skipped_files: tuple[FileDiff, ...] = (),
    usage: TokenUsage = NO_USAGE,
) -> PipelineResult:
    return PipelineResult(
        VerifyResult(kept=reported + below_threshold, checks=checks),
        Decision(reported=reported, below_threshold=below_threshold),
        usage,
        skipped_files,
        model_called=True,
    )


def render(result: PipelineResult) -> str:
    return render_json(result, Settings(provider="mock"), "run-1")


def test_render_findings_with_none_names_the_threshold() -> None:
    assert render_findings((), Severity.HIGH) == "No findings at or above high."


def test_render_findings_formats_each_finding_on_its_own_line() -> None:
    findings = [make_finding(), make_finding(file="b.py", severity=Severity.LOW)]

    assert render_findings(findings, Severity.LOW) == (
        "a.py:7 [high/logic] Why. (confidence 0.88)\nb.py:7 [low/logic] Why. (confidence 0.88)"
    )


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("x\ny", r"x\ny"),
        ("x\r\ny", r"x\r\ny"),
        ("x\ty", r"x\ty"),
        ("\x1b[31mred", r"\x1b[31mred"),
        ("x\x85y", r"x\x85y"),
        ("x\u2028y", r"x\u2028y"),
        ("x\u2029y", r"x\u2029y"),
        ("x\u202ey", r"x\u202ey"),
    ],
    ids=["newline", "crlf", "tab", "escape", "next-line", "line-sep", "para-sep", "bidi-override"],
)
def test_render_findings_escapes_unprintable_characters(raw: str, escaped: str) -> None:
    rendered_file = render_findings([make_finding(file=raw)], Severity.LOW)
    rendered_rationale = render_findings([make_finding(rationale=raw)], Severity.LOW)

    assert rendered_file.startswith(f"{escaped}:7 ")
    assert f"] {escaped} (confidence" in rendered_rationale
    assert len(rendered_file.splitlines()) == 1
    assert len(rendered_rationale.splitlines()) == 1


def test_escape_unprintable_with_undecodable_path_byte_escapes_it() -> None:
    assert escape_unprintable("bad\udcffname") == r"bad\udcffname"


def test_render_findings_keeps_printable_unicode() -> None:
    rendered = render_findings([make_finding(rationale="a → b é 日本")], Severity.LOW)

    assert "a → b é 日本" in rendered


def test_render_json_writes_every_report_field_in_a_fixed_order() -> None:
    result = pipeline_result(
        reported=(reviewed(source=Source.RULE, rule=RuleId.SWALLOWED_EXCEPTION),),
        usage=TokenUsage(input_tokens=12, output_tokens=3),
    )

    assert json.loads(render(result)) == {
        "schema": "report/v1",
        "run_id": "run-1",
        "provider": "mock",
        "model": Settings().model,
        "prompt_version": PROMPT_VERSION,
        "threshold": "low",
        "findings": [
            {
                "fingerprint": "0123456789abcdef",
                "source": "rule",
                "rule": "swallowed-exception",
                "file": "a.py",
                "line": 7,
                "severity": "high",
                "category": "logic",
                "rationale": "Why.",
                "confidence": 0.876,
                "triage": None,
                "original_rationale": None,
            }
        ],
        "counts": {
            "reported": 1,
            "below_threshold": 0,
            "dropped": 0,
            "deduped": 0,
            "rejected": 0,
            "undecided": 0,
        },
        "skipped_files": [],
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }
    assert list(json.loads(render(result))) == [
        "schema",
        "run_id",
        "provider",
        "model",
        "prompt_version",
        "threshold",
        "findings",
        "counts",
        "skipped_files",
        "usage",
    ]


def test_render_json_with_an_untagged_ai_finding_writes_a_null_rule() -> None:
    [finding] = json.loads(render(pipeline_result(reported=(reviewed(),))))["findings"]

    assert finding["source"] == "ai"
    assert finding["rule"] is None


def test_render_json_lists_only_reported_findings_and_counts_the_rest() -> None:
    proposed = make_finding()
    result = pipeline_result(
        reported=(reviewed(fingerprint="aaaaaaaaaaaaaaaa"),),
        below_threshold=(reviewed(fingerprint="bbbbbbbbbbbbbbbb"),),
        checks=(
            ProposalCheck(proposed, None, drop_reason=DropReason.UNKNOWN_FILE),
            ProposalCheck(proposed, "a.py", duplicate_of="aaaaaaaaaaaaaaaa"),
            ProposalCheck(proposed, "a.py", duplicate_of="aaaaaaaaaaaaaaaa"),
        ),
    )

    report = json.loads(render(result))

    assert [finding["fingerprint"] for finding in report["findings"]] == ["aaaaaaaaaaaaaaaa"]
    assert report["counts"] == {
        "reported": 1,
        "below_threshold": 1,
        "dropped": 1,
        "deduped": 2,
        "rejected": 0,
        "undecided": 0,
    }


def test_render_json_with_no_findings_writes_an_empty_list() -> None:
    report = json.loads(render(pipeline_result()))

    assert report["findings"] == []
    assert report["counts"]["reported"] == 0


def test_render_json_lists_skipped_files_naming_a_deleted_one_by_its_old_path() -> None:
    skipped = (
        FileDiff("README.md", "README.md", (), SkipReason.DOCS),
        FileDiff(None, "uv.lock", (), SkipReason.LOCK),
    )

    report = json.loads(render(pipeline_result(skipped_files=skipped)))

    assert report["skipped_files"] == [
        {"file": "README.md", "reason": "docs"},
        {"file": "uv.lock", "reason": "lock"},
    ]


def test_render_json_quotes_a_path_that_is_not_utf8_and_flags_it() -> None:
    finding = Finding.model_construct(**{**make_finding().model_dump(), "file": UNDECODABLE_PATH})
    skipped = (FileDiff(UNDECODABLE_PATH, None, (), SkipReason.BINARY),)

    report = json.loads(
        render(pipeline_result(reported=(reviewed(finding),), skipped_files=skipped))
    )

    assert report["findings"][0]["file"] == r'"docs/caf\377.py"'
    assert report["findings"][0]["path_quoted"] is True
    assert report["skipped_files"] == [
        {"file": r'"docs/caf\377.py"', "path_quoted": True, "reason": "binary"}
    ]


def test_render_json_with_a_valid_utf8_path_has_no_path_quoted_key() -> None:
    [finding] = json.loads(render(pipeline_result(reported=(reviewed(),))))["findings"]

    assert "path_quoted" not in finding


def test_render_json_with_hostile_text_is_one_ascii_line_that_round_trips() -> None:
    hostile = 'x\x1b[31m\ny\u2028\u2192 "q"'
    finding = make_finding(file=f"src/{hostile}.py", rationale=hostile)

    rendered = render(pipeline_result(reported=(reviewed(finding),)))

    assert rendered.isascii()
    assert len(rendered.splitlines()) == 1
    [parsed] = json.loads(rendered)["findings"]
    assert parsed["file"] == f"src/{hostile}.py"
    assert parsed["rationale"] == hostile


def test_render_json_of_two_equal_results_built_apart_is_identical() -> None:
    def build() -> PipelineResult:
        skipped = (FileDiff("README.md", None, (), SkipReason.DOCS),)
        return pipeline_result(reported=(reviewed(make_finding()),), skipped_files=skipped)

    assert render(build()) == render(build())


def test_render_json_and_text_write_a_path_holding_a_key_marker_alike() -> None:
    path = f"x{KEY_END_MARKER}.py"
    finding = make_finding(file=path)

    [parsed] = json.loads(render(pipeline_result(reported=(reviewed(finding),))))["findings"]

    assert parsed["file"] == path
    assert render_findings([finding], Severity.LOW).startswith(f"{path}:7 ")
