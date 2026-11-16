"""The eval gate's report: a line per suite, then a final verdict, as text or ``eval-gate/v1``."""

import json
from typing import Final

from review_bot.evals.gate import GateResult, SuiteResult
from review_bot.render import escape_unprintable

__all__ = ["GATE_SCHEMA", "render_gate_json", "render_gate_text"]

GATE_SCHEMA: Final = "eval-gate/v1"


def render_gate_text(result: GateResult) -> str:
    """The report, ending with the verdict.

    It holds each prompt pin that fails, each suite's line with its failing cases under it, and
    each model the suites don't cover.
    """
    lines = [escape_unprintable(problem) for problem in result.prompt_problems]
    for suite in result.suites:
        lines.append(_suite_line(suite))
        lines += [
            f"  case {escape_unprintable(case.case_id)}: {escape_unprintable(problem)}"
            for case in suite.cases
            for problem in case.problems
        ]
    lines += [escape_unprintable(problem) for problem in result.coverage_problems]
    lines.append(f"verdict: {_verdict(passed=result.passed)}")
    return "\n".join(lines)


def render_gate_json(result: GateResult) -> str:
    """The ``eval-gate/v1`` document, as one line of ASCII JSON."""
    document = {
        "schema": GATE_SCHEMA,
        "prompt_problems": list(result.prompt_problems),
        "suites": [_suite_document(suite) for suite in result.suites],
        "coverage_problems": list(result.coverage_problems),
        "verdict": _verdict(passed=result.passed),
    }
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def _suite_line(result: SuiteResult) -> str:
    return (
        f"suite {escape_unprintable(result.suite.name)}: cases {len(result.cases)}, fresh "
        f"{result.fresh}, passed {result.passed_cases}, accepted misses "
        f"{result.accepted_misses}, {_verdict(passed=result.passed)}"
    )


def _suite_document(result: SuiteResult) -> dict[str, object]:
    suite = result.suite
    return {
        "provider": suite.provider,
        "model": suite.model,
        "mode": suite.mode,
        "cases": len(result.cases),
        "fresh": result.fresh,
        "passed": result.passed_cases,
        "accepted_misses": result.accepted_misses,
        "verdict": _verdict(passed=result.passed),
        "failures": [
            {"case": case.case_id, "problems": list(case.problems)}
            for case in result.cases
            if not case.passed
        ],
    }


def _verdict(*, passed: bool) -> str:
    return "pass" if passed else "fail"
