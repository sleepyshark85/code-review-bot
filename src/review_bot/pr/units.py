"""Would-be comments: the units shadow precision is judged and counted in (ADR 0011).

Pure. A would-be comment is a (repo, PR, key, index) group of ``create`` entries across that
PR's shadow reports, where the key is the path, rule part and anchor (Phase 2 design, How the
gate is evidenced). Reports are untrusted, so ``ShadowFile`` parses the ``shadow/v1`` fields used
here, and any text from them must be escaped before it is shown.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from review_bot.findings import Severity, Source
from review_bot.pr.feedback import Hex16, RepoName, Text, Unit
from review_bot.pr.outcome import Outcome
from review_bot.pr.plan import Action

__all__ = ["ShadowEntry", "ShadowFile", "WouldBeComment", "would_be_comments"]


class _ReportPart(BaseModel):
    # Other keys are ignored, because later slices add keys to ``shadow/v1`` and never remove any.
    model_config = ConfigDict(frozen=True)


class ShadowEntry(_ReportPart):
    """One entry of a shadow report's ``comments``."""

    action: str
    fingerprint: Hex16
    rule_part: Text
    anchor: Hex16
    source: Source
    severity: Severity
    file: Text
    line: Annotated[int, Field(ge=1)]
    body: str


class _PullRequest(_ReportPart):
    repo: RepoName
    number: Annotated[int, Field(ge=1)]


class _Review(_ReportPart):
    """The fields of the embedded ``report/v1`` object that a label keeps."""

    model: Text
    prompt_version: Text


class ShadowFile(_ReportPart):
    """The fields of a ``shadow/v1`` report that would-be comments need."""

    # Aliased because ``schema`` is a ``BaseModel`` attribute.
    schema_: Literal["shadow/v1"] = Field(alias="schema")
    run_id: Text
    mode: Literal["shadow", "comment"]
    origin: Literal["local", "event", "backfill"]
    outcome: Outcome
    pr: _PullRequest | None
    finished_at: AwareDatetime
    report: _Review | None
    comments: tuple[ShadowEntry, ...]

    @model_validator(mode="after")
    def _check_review(self) -> Self:
        if self.outcome is Outcome.REVIEWED and self.report is None:
            raise ValueError("a reviewed report must hold its report/v1 object")
        return self


@dataclass(frozen=True, slots=True)
class WouldBeComment:
    """One would-be comment, as the latest report with it shows it.

    ``run_id``, ``model`` and ``prompt_version`` are that report's run; ``report_count`` is how
    many reports had it.
    """

    unit: Unit
    entry: ShadowEntry
    run_id: str
    model: str
    prompt_version: str
    report_count: int


def would_be_comments(reports: Iterable[ShadowFile]) -> tuple[WouldBeComment, ...]:
    """Group the reports' ``create`` entries into would-be comments.

    Only ``shadow`` reports with outcome ``reviewed`` and a pull request count, whatever their
    origin. Within one report, a key's entries are indexed in line order. Each would-be comment
    takes its entry from the report with the latest ``finished_at``; between reports finished at
    the same moment, the later one given wins. A report given twice, by ``run_id``, counts once.
    They come in order of repo, PR, file and line.
    """
    latest: dict[Unit, tuple[ShadowEntry, str, _Review]] = {}
    counts: Counter[Unit] = Counter()
    seen_runs: set[str] = set()
    for report in sorted(reports, key=lambda report: report.finished_at):
        # An artifact downloaded twice is one run, not two reports agreeing.
        if report.run_id in seen_runs:
            continue
        seen_runs.add(report.run_id)
        pr, review = report.pr, report.report
        if report.mode != "shadow" or report.outcome is not Outcome.REVIEWED:
            continue
        if pr is None or review is None:
            continue
        for unit, entry in _indexed(pr, report.comments):
            counts[unit] += 1
            latest[unit] = (entry, report.run_id, review)
    comments = [
        WouldBeComment(unit, entry, run_id, review.model, review.prompt_version, counts[unit])
        for unit, (entry, run_id, review) in latest.items()
    ]
    return tuple(sorted(comments, key=_comment_order))


def _indexed(
    pr: _PullRequest, entries: Iterable[ShadowEntry]
) -> Iterator[tuple[Unit, ShadowEntry]]:
    """Each ``create`` entry of one report with its unit, numbering a key's entries by line."""
    by_key: defaultdict[tuple[str, str, str], list[ShadowEntry]] = defaultdict(list)
    for entry in entries:
        if entry.action == Action.CREATE:
            by_key[entry.file, entry.rule_part, entry.anchor].append(entry)
    for (file, rule_part, anchor), group in by_key.items():
        for index, entry in enumerate(sorted(group, key=lambda entry: entry.line)):
            yield Unit(pr.repo, pr.number, file, rule_part, anchor, index), entry


def _comment_order(comment: WouldBeComment) -> tuple[str, int, str, int, str, str, int]:
    unit = comment.unit
    return (
        unit.repo,
        unit.pr,
        unit.file,
        comment.entry.line,
        unit.rule_part,
        unit.anchor,
        unit.index,
    )
