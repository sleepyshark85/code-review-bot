import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from review_bot.errors import EventError
from review_bot.pr.event import (
    MAX_EVENT_BYTES,
    MAX_PULLS,
    MergedPull,
    PullRequestRef,
    read_event,
    read_pulls,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
REPO = "owner/repo"
CANARY = "canary-7f3e"
_DELETE = object()


def event(**changes: Any) -> dict[str, Any]:
    """A same-repo ``opened`` event; each change replaces the value at a dotted path."""
    document: dict[str, Any] = {
        "action": "opened",
        "number": 7,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 7,
            "title": f"Title {CANARY}",
            "body": f"Body {CANARY}\x07",
            "draft": False,
            "updated_at": "2026-11-08T10:00:00Z",
            "base": {"sha": BASE_SHA, "repo": {"full_name": "owner/repo"}},
            "head": {"sha": HEAD_SHA, "repo": {"full_name": "owner/repo"}},
        },
    }
    for path, value in changes.items():
        *parents, last = path.split("__")
        target = document
        for key in parents:
            target = target[key]
        if value is _DELETE:
            del target[last]
        else:
            target[last] = value
    return document


def write(tmp_path: Path, document: object) -> Path:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_read_event_keeps_only_the_fields_a_run_needs(tmp_path: Path) -> None:
    pr = read_event(write(tmp_path, event()))

    assert pr == PullRequestRef(
        repo="owner/repo",
        number=7,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        is_fork=False,
        is_draft=False,
        event_at=datetime(2026, 11, 8, 10, 0, tzinfo=UTC),
        action="opened",
    )
    assert CANARY not in repr(pr)


def test_read_event_lowercases_an_uppercase_sha(tmp_path: Path) -> None:
    pr = read_event(write(tmp_path, event(pull_request__head__sha="B" * 40)))

    assert pr.head_sha == HEAD_SHA


@pytest.mark.parametrize(
    "head_repo",
    [None, {"full_name": "someone/repo"}, {}, "owner/repo"],
    ids=["null", "other", "no-name", "not-an-object"],
)
def test_read_event_with_any_head_repo_but_this_one_is_a_fork(
    tmp_path: Path, head_repo: object
) -> None:
    pr = read_event(write(tmp_path, event(pull_request__head__repo=head_repo)))

    assert pr.is_fork


def test_read_event_reads_draft_and_treats_a_missing_one_as_false(tmp_path: Path) -> None:
    assert read_event(write(tmp_path, event(pull_request__draft=True))).is_draft
    assert not read_event(write(tmp_path, event(pull_request__draft=_DELETE))).is_draft


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"pull_request": _DELETE}, "lacks pull_request."),
        ({"pull_request": []}, "pull_request isn't an object"),
        ({"pull_request__head": _DELETE}, "lacks pull_request.head."),
        ({"pull_request__head__sha": "b" * 39}, "pull_request.head.sha isn't a 40-hex-digit SHA"),
        ({"pull_request__base__sha": "g" * 40}, "pull_request.base.sha isn't a 40-hex-digit SHA"),
        ({"repository__full_name": "owner"}, "repository.full_name isn't owner/name"),
        ({"pull_request__number": 0}, "pull_request.number isn't a positive integer"),
        ({"pull_request__number": True}, "pull_request.number isn't a positive integer"),
        ({"pull_request__number": "7"}, "pull_request.number isn't a positive integer"),
        ({"pull_request__draft": "no"}, "pull_request.draft isn't true or false"),
        ({"pull_request__updated_at": "yesterday"}, "pull_request.updated_at isn't an ISO 8601"),
        ({"pull_request__updated_at": "2026-11-08T10:00:00"}, "pull_request.updated_at isn't"),
        ({"action": "Opened!"}, "action isn't an action name"),
    ],
)
def test_read_event_with_a_bad_field_names_it_but_not_its_value(
    tmp_path: Path, changes: dict[str, Any], named: str
) -> None:
    with pytest.raises(EventError, match=r"^The event file --event .*GITHUB_EVENT_PATH\.$") as info:
        read_event(write(tmp_path, event(**changes)))

    assert named in str(info.value)
    assert "\n" not in str(info.value)


def test_read_event_that_isnt_json_says_so(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_bytes(b"{not json")

    with pytest.raises(EventError, match="isn't JSON"):
        read_event(path)


def test_read_event_nested_too_deeply_to_parse_says_it_isnt_json(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_text("[" * 100_000, encoding="utf-8")

    with pytest.raises(EventError, match="isn't JSON"):
        read_event(path)


def test_read_event_that_is_a_json_list_says_it_isnt_an_object(tmp_path: Path) -> None:
    with pytest.raises(EventError, match="isn't a JSON object"):
        read_event(write(tmp_path, [event()]))


def test_read_event_with_raw_control_characters_in_free_text_still_reads(tmp_path: Path) -> None:
    path = write(tmp_path, event())
    path.write_text(path.read_text(encoding="utf-8").replace("\\u0007", "\x07"), encoding="utf-8")

    assert read_event(path).number == 7


def test_read_event_over_10_mb_is_refused_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "event.json"
    path.write_bytes(b" " * (MAX_EVENT_BYTES + 1))

    with pytest.raises(EventError, match=f"over {MAX_EVENT_BYTES} bytes"):
        read_event(path)


def test_read_event_that_is_missing_names_the_flag(tmp_path: Path) -> None:
    with pytest.raises(EventError, match="Can't read the event file --event"):
        read_event(tmp_path / "missing.json")


def pull(**changes: Any) -> dict[str, Any]:
    """One entry as ``gh pr list --state merged --json`` writes it, with ``changes`` applied."""
    entry: dict[str, Any] = {
        "number": 12,
        "baseRefOid": BASE_SHA.upper(),
        "headRefOid": HEAD_SHA,
        "mergedAt": "2026-11-07T08:09:10Z",
        "isCrossRepository": False,
    }
    for key, value in changes.items():
        if value is _DELETE:
            del entry[key]
        else:
            entry[key] = value
    return entry


def test_read_pulls_keeps_each_merged_pull_request_in_file_order(tmp_path: Path) -> None:
    pulls = read_pulls(write(tmp_path, [pull(), pull(number=3, isCrossRepository=True)]), repo=REPO)

    assert pulls == (
        MergedPull(12, BASE_SHA, HEAD_SHA, datetime(2026, 11, 7, 8, 9, 10, tzinfo=UTC), False),
        MergedPull(3, BASE_SHA, HEAD_SHA, datetime(2026, 11, 7, 8, 9, 10, tzinfo=UTC), True),
    )


def test_merged_pull_ref_is_the_pull_request_as_its_merge_left_it(tmp_path: Path) -> None:
    (merged,) = read_pulls(write(tmp_path, [pull(isCrossRepository=True)]), repo=REPO)

    assert merged.ref("owner/repo") == PullRequestRef(
        repo="owner/repo",
        number=12,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        is_fork=True,
        is_draft=False,
        event_at=merged.merged_at,
        action="closed",
    )


@pytest.mark.parametrize(
    ("entry", "named"),
    [
        (pull(number=_DELETE), "entry 2 lacks number"),
        (pull(number=0), "entry 2: number isn't a positive integer"),
        (pull(baseRefOid="abc"), "entry 2: baseRefOid isn't a 40-hex-digit SHA"),
        (pull(headRefOid="g" * 40), "entry 2: headRefOid isn't a 40-hex-digit SHA"),
        (pull(mergedAt="2026-11-07T08:09:10"), "entry 2: mergedAt isn't an ISO 8601"),
        (pull(isCrossRepository=_DELETE), "entry 2 lacks isCrossRepository"),
        (pull(isCrossRepository="no"), "entry 2: isCrossRepository isn't true or false"),
        (pull(), "entry 2 repeats the number of an earlier entry"),
        ("12", "entry 2 isn't a JSON object"),
    ],
)
def test_read_pulls_with_a_bad_entry_names_it_and_the_field_but_not_the_value(
    tmp_path: Path, entry: object, named: str
) -> None:
    with pytest.raises(
        EventError, match=r"^The pulls file --pulls .*isCrossRepository,body\.$"
    ) as info:
        read_pulls(write(tmp_path, [pull(), entry]), repo=REPO)

    assert named in str(info.value)
    assert "abc" not in str(info.value)


def test_read_pulls_that_isnt_a_list_says_so(tmp_path: Path) -> None:
    with pytest.raises(EventError, match="isn't a JSON list"):
        read_pulls(write(tmp_path, {"number": 12}), repo=REPO)


def test_read_pulls_over_500_entries_is_refused(tmp_path: Path) -> None:
    entries = [pull(number=number) for number in range(1, MAX_PULLS + 2)]

    with pytest.raises(EventError, match=f"more than {MAX_PULLS} pull requests"):
        read_pulls(write(tmp_path, entries), repo=REPO)


def test_read_pulls_at_500_entries_is_read(tmp_path: Path) -> None:
    entries = [pull(number=number) for number in range(1, MAX_PULLS + 1)]

    assert len(read_pulls(write(tmp_path, entries), repo=REPO)) == MAX_PULLS


def test_read_pulls_over_10_mb_is_refused_before_parsing(tmp_path: Path) -> None:
    path = tmp_path / "pulls.json"
    path.write_bytes(b"[" + b" " * MAX_EVENT_BYTES + b"]")

    with pytest.raises(EventError, match=f"--pulls .* is over {MAX_EVENT_BYTES} bytes"):
        read_pulls(path, repo=REPO)


def test_read_pulls_that_isnt_json_or_is_missing_names_the_flag(tmp_path: Path) -> None:
    path = tmp_path / "pulls.json"
    with pytest.raises(EventError, match=r"^Can't read the pulls file --pulls "):
        read_pulls(path, repo=REPO)
    path.write_text("[{", encoding="utf-8")
    with pytest.raises(EventError, match=r"--pulls .* isn't JSON"):
        read_pulls(path, repo=REPO)


# Linked issues (P3-S9).


def test_read_event_keeps_the_issues_its_body_closes_but_not_the_body(tmp_path: Path) -> None:
    body = f"Fixes #12, closes: #7 and RESOLVED owner/repo#9 {CANARY}"

    pr = read_event(write(tmp_path, event(pull_request__body=body)))

    assert pr.linked_issues == (12, 7, 9)
    assert CANARY not in repr(pr)


@pytest.mark.parametrize("body", [None, _DELETE])
def test_read_event_with_a_null_or_missing_body_links_no_issue(
    tmp_path: Path, body: object
) -> None:
    assert read_event(write(tmp_path, event(pull_request__body=body))).linked_issues == ()


def test_read_event_with_a_body_that_isnt_text_names_the_field(tmp_path: Path) -> None:
    with pytest.raises(EventError, match=r"pull_request\.body isn't text or null"):
        read_event(write(tmp_path, event(pull_request__body=12)))


def test_read_pulls_keeps_the_issues_each_body_closes_in_its_repository(tmp_path: Path) -> None:
    entries = [
        pull(body="Fixes #3 and fixes other/repo#4"),
        pull(number=13, body=None),
        pull(number=14),
    ]

    pulls = read_pulls(write(tmp_path, entries), repo=REPO)

    assert [merged.linked_issues for merged in pulls] == [(3,), (), ()]
    assert pulls[0].ref(REPO).linked_issues == (3,)
