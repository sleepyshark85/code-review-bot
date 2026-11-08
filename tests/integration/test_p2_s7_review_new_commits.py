r"""P2-S7: re-review only what changed since the last reviewed commit.

Written from the spec's criteria, design.md (Comment state, Interfaces, `shadow/v1`) and ADR 0009
while the developer built the slice, before any of its code was read.

Each test is a sequence of pushes on one pull request, as in P2-S5's suite, whose `Session`
(a real git repository in `tmp_path`, one `FakeGitHubClient` for the whole PR, and in-process
`review_bot.pr.cli.main(..., provider=P, github=FAKE)` runs with `--mode comment`) it reuses.
The first run of a PR has no summary, so it reviews the whole PR and leaves `reviewed=A` in the
summary's marker; the pushes after it are reviewed from there.

What the model was asked is read from `MockProvider.requests` (each request's `user_prompt`).
A test that needs a summary the bot didn't write sets the fake's `summary` attribute, which the
fake's `read_summary` is assumed to parse (design.md, Markers).
"""

import importlib
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from test_p2_s2_review_pr_event import event
from test_p2_s5_comment_sync import (
    APP,
    APP_V1,
    BASE_FILE,
    DEFAULT_MAX_OPEN_COMMENTS,
    EVAL,
    EVAL_LINE,
    GITHUB_ERROR_EXIT,
    OTHER_FILE,
    TRANSIENT_EXIT,
    Run,
    Session,
    ai,
    bot_body,
    fingerprint_of,
    inserted,
    marker_of,
    replaced,
    text,
)

from review_bot.diff_parser import parse_diff
from review_bot.errors import ErrorCategory, ProviderError
from review_bot.findings import Finding, ReviewedFinding, Source
from review_bot.providers.mock import MockProvider

pytestmark = pytest.mark.integration

# Generous: it catches a hang or quadratic work, not a slow CI machine.
BOUND_S = 60.0
SUMMARY_MARKER = re.compile(r"<!-- review-bot:v1 summary reviewed=(?P<sha>[0-9a-f]{40}) -->")


@pytest.fixture(autouse=True)
def outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


class PullRequest(Session):
    """P2-S5's session, plus the base branch moving on and history being rewritten."""

    def __init__(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], **options: Any) -> None:
        super().__init__(tmp_path, capsys, **options)
        self.base_tip = self.repo.base

    def payload(self, **changes: Any) -> dict[str, Any]:
        """GitHub's `base.sha` is the base branch's tip when the event fires."""
        return event(self.base_tip, self.head, **({"action": "synchronize"} | changes))

    def advance_main(self, files: dict[str, str | None]) -> None:
        self.repo.git("checkout", "-q", "main")
        self.base_tip = self.repo.commit(files, "main moves on")
        self.repo.git("checkout", "-q", "feature")

    def merge_main(self) -> str:
        self.repo.git("merge", "-q", "--no-ff", "-m", "Merge main", "main")
        self.head = self.repo.git("rev-parse", "HEAD").strip()
        return self.head

    def force_push(self, files: dict[str, str | None]) -> str:
        """Rewrite the branch from the base: the last reviewed commit isn't an ancestor."""
        self.repo.git("reset", "-q", "--hard", self.repo.base)
        return self.push(files)

    def reviewed(self) -> str | None:
        """The summary's `reviewed=` SHA, or None with no summary or no valid marker."""
        summary = self.github.summary()
        if not summary:
            return None
        match = SUMMARY_MARKER.fullmatch(summary.splitlines()[-1])
        return None if match is None else match["sha"]

    def github_recovers(self) -> None:
        """GitHub works again: a new fake holding the same comments and summary.

        The fake's `fail_on` fails every later call to its operation.
        """
        old = self.github.client
        self.github.client = type(old)()
        self.github.client.comments = old.comments
        self.github.client.summary = old.summary

    def set_summary(self, body: str | None) -> None:
        self.github.client.summary = body

    def review(self, findings: Sequence[Finding] = (), **options: Any) -> tuple[Run, MockProvider]:
        provider = options.pop("provider", None) or MockProvider(findings=list(findings))
        return self.run(provider=provider, **options), provider


@pytest.fixture
def pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> PullRequest:
    return PullRequest(tmp_path, capsys)


def prompt(provider: MockProvider) -> str:
    return "\n".join(request.user_prompt for request in provider.requests)


def open_count(count: int) -> str:
    return f"Open bot comments on this pull request: {count}."


def changes_since(sha: str) -> str:
    return f"Changes since `{sha[:7]}` reviewed."


def scope(run: Run) -> tuple[str, str | None]:
    return run.shadow["scope"], run.shadow["since"]


# A PR with two files: APP (new, commented on EVAL) and OTHER (new, untouched after A).
OTHER_LINE = "untouched_marker = compute_untouched()"
APP_EDITED = replaced(APP_V1, 5, "def save_all(path, value):")  # 2 lines below EVAL


def reviewed_once(pr: PullRequest, findings: Sequence[Finding] = (ai(EVAL_LINE),)) -> str:
    """The first push and its full review; returns `A`."""
    pr.push({APP: text(APP_V1), OTHER_FILE: f"{OTHER_LINE}\n"})
    run, _ = pr.review(findings)
    assert run.exit_code == 0, run.stderr
    assert len(run.posted) == len(findings), run.calls
    return pr.head


# An existing file whose PR hunks are apart: lines 2 and 20 are the PR's, 17 base lines between.
LIB = "src/lib.py"
LIB_BASE = [line for n in range(1, 11) for line in (f"def step_{n}():", f"    return {n}")]
TOP, BOTTOM, BOTTOM_V2 = (
    "    top = eval(data)",
    "    bottom = eval(data)",
    "    bottom = safe(data)",
)
LIB_A = inserted(inserted(LIB_BASE, 2, TOP), 20, BOTTOM)


@pytest.fixture
def lib_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> PullRequest:
    pr = PullRequest(tmp_path, capsys, base_files={LIB: text(LIB_BASE), BASE_FILE: "base = 1\n"})
    assert (LIB_A[1], LIB_A[19]) == (TOP, BOTTOM)
    pr.push({LIB: text(LIB_A), APP: text(APP_V1)})
    run, _ = pr.review([ai(2, file=LIB)])
    assert run.exit_code == 0, run.stderr
    return pr


# AC1: only the PR diff's hunks with a line changed since A reach the model.


def test_p2_s7_ac1_only_the_hunk_changed_since_a_is_sent_and_scope_is_incremental(lib_pr):
    since = lib_pr.head
    lib_pr.push({LIB: text(replaced(LIB_A, 20, BOTTOM_V2))})

    run, provider = lib_pr.review()

    assert run.exit_code == 0, run.stderr
    sent = prompt(provider)
    assert BOTTOM_V2 in sent
    assert TOP not in sent, "a PR hunk with no line changed since A"
    assert "value = eval(data)" not in sent, "a file not changed since A"
    assert scope(run) == ("incremental", since)
    assert lib_pr.reviewed() == lib_pr.head


def test_p2_s7_ac1_base_branch_changes_merged_in_arent_sent(lib_pr):
    lib_pr.advance_main({BASE_FILE: "base = 2  # from main\n"})
    lib_pr.merge_main()
    lib_pr.push({LIB: text(replaced(LIB_A, 20, BOTTOM_V2))})

    run, provider = lib_pr.review()

    assert run.exit_code == 0, run.stderr
    sent = prompt(provider)
    assert BOTTOM_V2 in sent
    assert "from main" not in sent
    assert run.shadow["scope"] == "incremental"


def test_p2_s7_ac1_reviewed_marker_advances_with_each_push(pr):
    first = reviewed_once(pr)
    assert pr.reviewed() == first
    second = pr.push({APP: text(APP_EDITED)})
    assert pr.review([])[0].exit_code == 0
    assert pr.reviewed() == second
    third = pr.push({OTHER_FILE: f"{OTHER_LINE}\nmore = 1\n"})

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("incremental", second)
    assert pr.reviewed() == third


# AC2: a merge of the base branch alone: no model call, and the marker advances.


def test_p2_s7_ac2_merge_from_main_alone_calls_no_model_and_changes_no_comment(pr):
    since = reviewed_once(pr)
    pr.advance_main({BASE_FILE: "base = 2  # from main\n"})
    head = pr.merge_main()

    run, provider = pr.review([ai(EVAL_LINE, "low")])

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("reviewed", "no_new_changes")
    assert run.writes == ["write_summary"], "nothing posted, moved or resolved"
    assert len(pr.open_comments()) == 1
    assert pr.reviewed() == head
    assert (
        "**Review bot:** `reviewed` (`no_new_changes`). No changes to review since "
        f"`{since[:7]}`, so nothing was sent to the model. {open_count(1)}"
    ) in (pr.github.summary() or "")


# AC3: a re-run on the reviewed head is skipped.


def test_p2_s7_ac3_rerun_on_the_reviewed_head_is_skipped_already_reviewed(pr):
    reviewed_once(pr)
    summary_before = pr.github.summary()

    run, provider = pr.review([ai(EVAL_LINE, "low")])

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("skipped", "already_reviewed")
    assert run.writes == []
    assert pr.github.summary() == summary_before


# AC4: a force-push, or a reviewed SHA the clone doesn't have, reviews the whole PR.


def test_p2_s7_ac4_force_push_reviews_the_whole_pr(pr):
    reviewed_once(pr)
    head = pr.force_push({APP: text(APP_EDITED), OTHER_FILE: f"{OTHER_LINE}\n"})

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("full", None)
    assert OTHER_LINE in prompt(provider), "a file the rewrite didn't change is reviewed too"
    assert pr.reviewed() == head


def test_p2_s7_ac4_reviewed_sha_not_in_the_clone_reviews_the_whole_pr(pr):
    reviewed_once(pr)
    pr.push({APP: text(APP_EDITED)})
    pr.set_summary(f"Review summary.\n\n<!-- review-bot:v1 summary reviewed={'0f' * 20} -->")

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("full", None)
    assert OTHER_LINE in prompt(provider)


# AC5: no summary, or no valid marker, reviews the whole PR.

A_SHA_39 = "a" * 39
BAD_SUMMARIES = {
    "no_summary": None,
    "no_marker": "Review summary.\n\nNo findings at or above `medium`.",
    "s5_none_marker": "Review summary.\n\n<!-- review-bot:v1 summary reviewed=none -->",
    "sha_39_hex": f"Summary.\n\n<!-- review-bot:v1 summary reviewed={A_SHA_39} -->",
    "sha_41_hex": f"Summary.\n\n<!-- review-bot:v1 summary reviewed={A_SHA_39}bb -->",
    "sha_not_hex": f"Summary.\n\n<!-- review-bot:v1 summary reviewed={A_SHA_39}g -->",
}


@pytest.mark.parametrize("case", BAD_SUMMARIES)
def test_p2_s7_ac5_summary_without_a_valid_marker_reviews_the_whole_pr(pr, case):
    reviewed_once(pr)
    pr.push({APP: text(APP_EDITED)})
    pr.set_summary(BAD_SUMMARIES[case])

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("full", None)
    assert OTHER_LINE in prompt(provider)


def test_p2_s7_ac5_first_run_with_no_summary_is_full(pr):
    pr.push({APP: text(APP_V1)})

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("full", None)


def test_p2_s7_ac5_valid_sha_with_text_after_the_marker_isnt_read(pr):
    """The marker is read by exact pattern: here a real ancestor, but not the marker line."""
    since = reviewed_once(pr)
    pr.push({APP: text(APP_EDITED)})
    pr.set_summary(f"S.\n\n<!-- review-bot:v1 summary reviewed={since} --> and more")

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("full", None)


# AC6: an own comment not found again is resolved only if its own line changed since A.


def test_p2_s7_ac6_comment_whose_own_line_changed_since_a_is_resolved(pr):
    since = reviewed_once(pr)
    comment = pr.comment_at(APP, EVAL_LINE)
    pr.push({APP: text(replaced(APP_V1, EVAL_LINE, "    value = safe_eval(data)"))})
    pr.github.outdate(comment.comment_id)

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert run.shadow["scope"] == "incremental"
    assert run.resolved_threads == [comment.thread_id]
    summary = pr.github.summary() or ""
    assert changes_since(since) in summary
    assert open_count(0) in summary, "1 open before the run, 1 resolved"


@pytest.mark.parametrize("github_view", ["followed", "outdated"])
def test_p2_s7_ac6_unchanged_line_inside_a_hunk_changed_since_a_is_kept(pr, github_view):
    since = reviewed_once(pr)
    comment = pr.comment_at(APP, EVAL_LINE)
    pr.push({APP: text(APP_EDITED)})
    if github_view == "outdated":
        pr.github.outdate(comment.comment_id)

    run, provider = pr.review([])  # the model saw EVAL and found nothing there this time

    assert run.exit_code == 0, run.stderr
    assert EVAL in prompt(provider)
    assert run.resolved_threads == []
    assert run.writes == ["write_summary"]
    assert (
        f"**Review bot:** `reviewed`. {changes_since(since)} "
        f"No new findings at or above {run.shadow['report']['threshold']}. {open_count(1)}"
    ) in (pr.github.summary() or "")


def test_p2_s7_ac6_unchanged_line_in_a_file_added_since_a_is_kept(pr):
    """An own comment on a file A didn't have, its line still there: S5's rule keeps it."""
    reviewed_once(pr)
    added = "src/added.py"
    pr.push({added: text(["def added(data):", EVAL, "    return value"])})
    seeded = pr.github.seed(
        path=added,
        line=2,
        body=bot_body(fingerprint=fingerprint_of(EVAL, file=added), anchor_text=EVAL),
    )
    pr.push({added: text(["def added(data):", EVAL, "    return value or None"])})

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == []
    assert pr.comment(seeded).resolved_by is None


def test_p2_s7_ac6_edited_line_in_a_file_added_since_a_is_resolved(pr):
    """The comment was posted by a run whose summary write then failed, so `reviewed` is A."""
    since = reviewed_once(pr)
    added = "src/added.py"
    added_lines = ["def added(data):", EVAL, "    return value"]
    pr.push({added: text(added_lines)})
    pr.github.fail("write_summary")
    failed, _ = pr.review([ai(2, file=added)])
    assert failed.exit_code == GITHUB_ERROR_EXIT, failed.stderr
    assert pr.reviewed() == since
    pr.github_recovers()
    comment = pr.comment_at(added, 2)
    pr.push({added: text(replaced(added_lines, 2, "    value = safe_eval(data)"))})
    pr.github.outdate(comment.comment_id)

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("incremental", since)
    assert run.resolved_threads == [comment.thread_id]


# AC7: an ordinal shift from the incremental diff pairs by key and nearest line.

TWIN = "    twin = eval(data)"
LIB_TWINS = inserted(inserted(LIB_BASE, 2, TWIN), 20, TWIN)


def test_p2_s7_ac7_twin_found_at_ordinal_0_pairs_with_its_ordinal_1_comment(tmp_path, capsys):
    pr = PullRequest(tmp_path, capsys, base_files={LIB: text(LIB_BASE)})
    pr.push({LIB: text(LIB_TWINS)})
    first, _ = pr.review([ai(2, file=LIB), ai(20, file=LIB)])
    assert len(first.posted) == 2, first.calls
    lower = pr.comment_at(LIB, 20)
    assert marker_of(lower.body)["fingerprint"] == fingerprint_of(TWIN, 1, file=LIB)
    pr.push({LIB: text(replaced(LIB_TWINS, 21, "def step_ten():"))})  # just below the twin

    run, provider = pr.review([ai(20, file=LIB)])

    assert run.exit_code == 0, run.stderr
    assert prompt(provider).count(TWIN) == 1, "only the lower twin's hunk is sent"
    assert run.posted == []
    assert run.resolved_threads == []
    assert run.writes == ["write_summary"]
    kept = run.entry("keep")
    assert (kept["line"], kept["fingerprint"]) == (20, marker_of(lower.body)["fingerprint"])
    assert len(pr.open_comments()) == 2


# AC8: a rename-only push: no model call, keys follow the rename, nothing resolved or moved.

OLD_PATH, NEW_PATH = "src/old_name.py", "src/new_name.py"
RENAME_BASE = [line for n in range(1, 6) for line in (f"def helper_{n}(data):", f"    return {n}")]
RENAME_HEAD = inserted(RENAME_BASE, 2, EVAL)


def test_p2_s7_ac8_rename_only_push_calls_no_model_and_keeps_every_comment(tmp_path, capsys):
    pr = PullRequest(tmp_path, capsys, base_files={OLD_PATH: text(RENAME_BASE)})
    pr.push({OLD_PATH: text(RENAME_HEAD)})
    assert len(pr.review([ai(2, file=OLD_PATH)])[0].posted) == 1
    comment = pr.comment_at(OLD_PATH, 2)
    head = pr.push({}, renames={OLD_PATH: NEW_PATH})
    pr.github.outdate(comment.comment_id)

    run, provider = pr.review([ai(2, file=NEW_PATH)])

    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    assert (run.shadow["outcome"], run.shadow["reason"]) == ("reviewed", "no_new_changes")
    assert run.writes == ["write_summary"], "none resolved, none moved"
    assert pr.comment(comment.comment_id).resolved_by is None
    assert pr.reviewed() == head


# AC9: a finding on a line the incremental diff has but the PR diff doesn't is withheld.


def test_p2_s7_ac9_finding_on_a_line_only_the_changed_diff_has_is_withheld_not_commentable():
    """Through `plan_comments`: end to end the reviewed diff is a subset of the PR diff, so
    verify drops such a finding before the plan. Here `changed` holds a base-branch line merged
    in since A, which the PR diff doesn't."""
    finding = ReviewedFinding(
        finding=ai(1, file=BASE_FILE), source=Source.AI, rule=None, fingerprint="c3" * 8
    )
    pr_diff = (
        f"diff --git a/{APP} b/{APP}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{APP}\n@@ -0,0 +1,1 @@\n+{APP_V1[0]}\n"
    )
    changed = (
        f"diff --git a/{BASE_FILE} b/{BASE_FILE}\n--- a/{BASE_FILE}\n+++ b/{BASE_FILE}\n"
        "@@ -1 +1 @@\n-base = 1\n+base = 2  # from main\n"
    )
    plan = importlib.import_module("review_bot.pr.plan").plan_comments(
        [finding],
        [],
        pr_diff=parse_diff(pr_diff),
        changed=parse_diff(changed),
        max_open_comments=DEFAULT_MAX_OPEN_COMMENTS,
    )

    assert plan.comments == ()
    assert [(w.reviewed.finding.file, str(w.reason)) for w in plan.withheld] == [
        (BASE_FILE, "not_commentable")
    ]


# AC10: a failed run leaves `reviewed=A`, so the next push reviews from A again.


def test_p2_s7_ac10_provider_failure_keeps_reviewed_at_a(pr):
    since = reviewed_once(pr)
    pr.push({APP: text(APP_EDITED)})
    failing = MockProvider(error=ProviderError(ErrorCategory.TRANSIENT, "Try again."))

    failed, _ = pr.review(provider=failing)
    assert failed.exit_code == TRANSIENT_EXIT, failed.stderr
    assert pr.reviewed() == since
    pr.push({OTHER_FILE: f"{OTHER_LINE}\nmore = 1\n"})

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("incremental", since)
    assert "def save_all(path, value):" in prompt(provider), "the failed push's change again"


def test_p2_s7_ac10_github_failure_keeps_reviewed_at_a(pr):
    since = reviewed_once(pr)
    pr.push({APP: text(APP_EDITED), OTHER_FILE: f"{OTHER_LINE}\nsecond = eval(more)\n"})
    pr.github.fail("post_review")

    failed, _ = pr.review([ai(2, file=OTHER_FILE)])
    assert failed.exit_code == GITHUB_ERROR_EXIT, failed.stderr
    assert pr.reviewed() == since
    pr.github_recovers()

    run, _ = pr.review([ai(2, file=OTHER_FILE)])  # a re-run of the same head

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("incremental", since)
    assert [(path, line) for path, line, _, _ in run.posted] == [(OTHER_FILE, 2)]
    summary = pr.github.summary() or ""
    assert changes_since(since) in summary
    assert "0 withheld." in summary
    assert open_count(2) in summary, "1 open before the run, 1 created"


# AC11: shadow mode always reviews the whole PR and uses no client.


def test_p2_s7_ac11_shadow_mode_is_full_every_time_and_uses_no_client(pr):
    reviewed_once(pr)
    pr.push({APP: text(APP_EDITED)})

    for _ in range(2):
        run, provider = pr.review([], mode="shadow", with_client=True)

        assert run.exit_code == 0, run.stderr
        assert scope(run) == ("full", None)
        assert run.calls == []
        assert OTHER_LINE in prompt(provider)
        assert "Open bot comments" not in run.shadow["summary_body"]


# AC12: a file added in the PR, then renamed after A with one line edited elsewhere in it.

A_PY, B_PY = "src/a.py", "src/b.py"
A_LINES = [*APP_V1, *[f"    step_{n} = {n}" for n in range(1, 7)]]  # EVAL on line 3
B_LINES = replaced(A_LINES, 5, "def save_all(path, value):")  # within the hunk's context


@pytest.fixture
def renamed_after_a(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> PullRequest:
    pr = PullRequest(tmp_path, capsys)
    pr.push({A_PY: text(A_LINES), OTHER_FILE: f"{OTHER_LINE}\n"})
    assert len(pr.review([ai(EVAL_LINE, file=A_PY)])[0].posted) == 1
    pr.push({B_PY: text(B_LINES)}, renames={A_PY: B_PY})
    assert f"rename to {B_PY}" in pr.repo.git("diff", "-M", f"HEAD~1..{pr.head}")
    pr.github.outdate(pr.comment_at(A_PY, EVAL_LINE).comment_id)
    return pr


def test_p2_s7_ac12_only_the_edit_is_reviewed_and_the_comment_isnt_resolved(renamed_after_a):
    pr = renamed_after_a
    (comment,) = pr.open_comments()

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    sent = prompt(provider)
    assert "def save_all(path, value):" in sent
    assert OTHER_LINE not in sent
    assert run.resolved_threads == []
    assert run.posted == []
    assert pr.comment(comment.comment_id).resolved_by is None


def test_p2_s7_ac12_pairing_finding_moves_it_to_b_with_its_fingerprint(renamed_after_a):
    pr = renamed_after_a
    (old,) = pr.open_comments()

    run, _ = pr.review([ai(EVAL_LINE, file=B_PY)])

    assert run.exit_code == 0, run.stderr
    assert run.resolved_threads == [old.thread_id]
    assert run.posted == [(B_PY, EVAL_LINE, "RIGHT", old.body)]
    moved = run.entry("move")
    assert (moved["file"], moved["fingerprint"]) == (B_PY, marker_of(old.body)["fingerprint"])
    assert not [e for e in run.entries if e["action"] == "create"]
    (now_open,) = pr.open_comments()
    assert now_open.path == B_PY


def test_p2_s7_ac6_after_a_rename_an_edit_to_the_commented_line_resolves_it(renamed_after_a):
    """AC12's comment stays on a.py with no pairing; then its line is edited in b.py."""
    pr = renamed_after_a
    (comment,) = pr.open_comments()
    assert pr.review([])[0].resolved_threads == []
    since = pr.head
    pr.push({B_PY: text(replaced(B_LINES, EVAL_LINE, "    value = safe_eval(data)"))})

    run, _ = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert scope(run) == ("incremental", since)
    assert run.resolved_threads == [comment.thread_id]


# An unusable diff since A falls back to a full review (spec Notes, review round 1).


def test_p2_s7_unusable_diff_since_a_over_the_byte_cap_falls_back_to_full(pr, monkeypatch):
    reviewed_once(pr)
    pr.advance_main({"src/vendored.py": "".join(f"row_{n} = {n}\n" for n in range(20_000))})
    pr.merge_main()
    pr.push({APP: text(APP_EDITED)})
    pr_bytes = len(pr.repo.git("diff", f"{pr.base_tip}...{pr.head}").encode())
    assert pr_bytes < 5_000
    monkeypatch.setenv("REVIEW_BOT_MAX_DIFF_BYTES", "5000")

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert scope(run) == ("full", None)
    assert OTHER_LINE in prompt(provider)
    assert pr.reviewed() == pr.head


def test_p2_s7_unusable_diff_since_a_not_utf8_falls_back_to_full(pr):
    reviewed_once(pr)
    pr.repo.git("checkout", "-q", "main")
    (pr.repo.path / "src" / "latin1.py").write_bytes(b"name = 'caf\xe9'\n")
    pr.base_tip = pr.repo.commit({}, "main adds a Latin-1 file")
    pr.repo.git("checkout", "-q", "feature")
    pr.merge_main()
    pr.push({APP: text(APP_EDITED)})

    run, provider = pr.review([])

    assert run.exit_code == 0, run.stderr
    assert run.shadow["outcome"] == "reviewed"
    assert scope(run) == ("full", None)
    assert OTHER_LINE in prompt(provider)


# Limits: the marker is read by exact pattern, quickly, from a hostile summary.


def test_p2_s7_limits_huge_summary_of_marker_text_is_read_quickly_and_ignored(pr):
    since = reviewed_once(pr)
    pr.push({APP: text(APP_EDITED)})
    hostile = (f"<!-- review-bot:v1 summary reviewed={since}" + " " * 9 + "reviewed=") * 50_000
    pr.set_summary(hostile + "\n<!-- review-bot:v1 summary reviewed=" + "a" * 1_000_000 + " -->")

    started = time.monotonic()
    run, _ = pr.review([])
    elapsed = time.monotonic() - started

    assert run.exit_code == 0, run.stderr
    assert elapsed < BOUND_S, f"took {elapsed:.1f}s"
    assert scope(run) == ("full", None)
