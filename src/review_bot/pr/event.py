"""The pull requests a run is about: from a ``pull_request`` event, or a pulls file of merged ones.

Only SHAs, numbers, names, flags and times are kept. No free text, such as a pull request's
title or body, goes any further (Phase 2 design, Safety on the PR path).
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from review_bot.errors import EventError
from review_bot.secret_scan import redact

__all__ = [
    "MAX_EVENT_BYTES",
    "MAX_PULLS",
    "REPO_NAME",
    "MergedPull",
    "PullRequestRef",
    "read_event",
    "read_pulls",
]

# GitHub's own payloads are far smaller; a larger event or pulls file is refused before it is
# parsed.
MAX_EVENT_BYTES: Final = 10_000_000
MAX_PULLS: Final = 500
_SHA: Final = re.compile(r"[0-9a-fA-F]{40}")
# A repository's ``owner/name``, as GitHub names it.
REPO_NAME: Final = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_ACTION: Final = re.compile(r"[a-z_]{1,50}")
_ADVICE: Final = "Pass the pull_request event file GitHub Actions writes to GITHUB_EVENT_PATH."
_PULLS_ADVICE: Final = (
    "Write it with gh pr list --state merged --json "
    "number,baseRefOid,headRefOid,mergedAt,isCrossRepository."
)
# The action of a merged pull request's last event.
_MERGED_ACTION: Final = "closed"


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    """The pull request one event is about.

    ``repo`` is the event's repository, ``owner/name``, and the SHAs are 40 lowercase hex digits.
    ``is_fork`` is true unless the head repository is that repository itself. ``event_at``
    is the pull request's ``updated_at``, and ``action`` the event's action, such as ``opened``;
    for a merged pull request from the pulls file, they are its ``mergedAt`` and ``closed``.
    """

    repo: str
    number: int
    base_sha: str
    head_sha: str
    is_fork: bool
    is_draft: bool
    event_at: datetime
    action: str


@dataclass(frozen=True, slots=True)
class MergedPull:
    """One merged pull request from the pulls file, which doesn't name its repository.

    The SHAs are 40 lowercase hex digits, and ``merged_at`` names its time zone.
    """

    number: int
    base_sha: str
    head_sha: str
    merged_at: datetime
    is_cross_repository: bool

    def ref(self, repo: str) -> PullRequestRef:
        """The pull request in ``repo``, as its merge left it."""
        return PullRequestRef(
            repo=repo,
            number=self.number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
            is_fork=self.is_cross_repository,
            is_draft=False,
            event_at=self.merged_at,
            action=_MERGED_ACTION,
        )


def read_event(path: Path) -> PullRequestRef:
    """Read the event file at ``path``, as GitHub Actions writes it to ``GITHUB_EVENT_PATH``.

    Raises:
        EventError: If the file can't be read, is over 10 MB, isn't a JSON object, or a field
            the run needs is missing or invalid; the message names the field, never its value.
    """
    name = f"event file --event {redact(str(path))}"
    event = _read_json(path, name, _ADVICE)
    if not isinstance(event, Mapping):
        raise EventError(f"The {name} isn't a JSON object. {_ADVICE}")
    return _pull_request_ref(_Fields(event, f"The {name}", _ADVICE))


def read_pulls(path: Path) -> tuple[MergedPull, ...]:
    """Read the pulls file at ``path``, as ``gh pr list --state merged --json`` writes it.

    Raises:
        EventError: If the file can't be read, is over 10 MB, isn't a JSON list of at most 500
            objects, lists a number twice, or an entry lacks a field or has an invalid one; the
            message names the entry and the field, never the value.
    """
    name = f"pulls file --pulls {redact(str(path))}"
    entries = _read_json(path, name, _PULLS_ADVICE)
    if not isinstance(entries, list):
        raise EventError(f"The {name} isn't a JSON list. {_PULLS_ADVICE}")
    if len(entries) > MAX_PULLS:
        raise EventError(f"The {name} lists more than {MAX_PULLS} pull requests. {_PULLS_ADVICE}")
    pulls: list[MergedPull] = []
    numbers: set[int] = set()
    for position, entry in enumerate(entries, start=1):
        where = f"The {name}, entry {position}"
        if not isinstance(entry, Mapping):
            raise EventError(f"{where} isn't a JSON object. {_PULLS_ADVICE}")
        pull = _merged_pull(_Fields(entry, where, _PULLS_ADVICE))
        # Each pull request's report has its own directory, so a repeat would overwrite one.
        if pull.number in numbers:
            raise EventError(f"{where} repeats the number of an earlier entry. {_PULLS_ADVICE}")
        numbers.add(pull.number)
        pulls.append(pull)
    return tuple(pulls)


def _read_json(path: Path, name: str, advice: str) -> object:
    """Parse the JSON file at ``path``, which messages call ``the {name}``.

    Raises:
        EventError: If it can't be read, is over 10 MB, or isn't JSON.
    """
    try:
        with path.open("rb") as json_file:
            raw = json_file.read(MAX_EVENT_BYTES + 1)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise EventError(f"Can't read the {name}: {reason}. {advice}") from exc
    if len(raw) > MAX_EVENT_BYTES:
        raise EventError(f"The {name} is over {MAX_EVENT_BYTES} bytes. {advice}")
    # Nesting deeper than the parser can recurse makes the file as unusable as bad syntax.
    try:
        # Not strict, so a control character GitHub left unescaped in an event's title or body,
        # which is never kept, can't make the whole file unreadable.
        return json.loads(raw, strict=False)
    except (ValueError, RecursionError) as exc:
        raise EventError(f"The {name} isn't JSON. {advice}") from exc


class _Fields:
    """Reads typed fields from a JSON object by their dotted path, raising ``EventError``.

    ``where`` starts each message, naming the file and the object, and ``advice`` ends it. The
    field's value is never shown: it is untrusted, and may be large.
    """

    def __init__(self, document: Mapping[str, object], where: str, advice: str) -> None:
        self._document = document
        self._where = where
        self._advice = advice

    def find(self, path: str) -> Mapping[str, object]:
        """The object at ``path``."""
        value = self._value(path)
        if not isinstance(value, Mapping):
            raise self._invalid(path, "an object")
        return value

    def matching(self, path: str, pattern: re.Pattern[str], expected: str) -> str:
        """The string at ``path``, which must match ``pattern`` whole."""
        value = self._value(path)
        if not (isinstance(value, str) and pattern.fullmatch(value)):
            raise self._invalid(path, expected)
        return value

    def positive_integer(self, path: str) -> int:
        """The integer at ``path``, which must be at least 1."""
        value = self._value(path)
        # ``type``, not ``isinstance``, because a bool is an int.
        if type(value) is not int or value < 1:
            raise self._invalid(path, "a positive integer")
        return value

    def boolean(self, path: str) -> bool:
        """The boolean at ``path``."""
        value = self._value(path)
        if not isinstance(value, bool):
            raise self._invalid(path, "true or false")
        return value

    def flag(self, path: str) -> bool:
        """The boolean at ``path``; false if its object lacks it."""
        parent, _, key = path.rpartition(".")
        value = self.find(parent).get(key, False)
        if not isinstance(value, bool):
            raise self._invalid(path, "true or false")
        return value

    def timestamp(self, path: str) -> datetime:
        """The ISO 8601 time at ``path``, which must name its time zone."""
        value = self._value(path)
        try:
            moment = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            moment = None
        if moment is None or moment.tzinfo is None:
            raise self._invalid(path, "an ISO 8601 time with a time zone")
        return moment

    def _value(self, path: str) -> object:
        """The value at ``path``.

        Raises:
            EventError: If it, or an object on the way, is missing.
        """
        current: object = self._document
        walked: list[str] = []
        for key in path.split("."):
            if not isinstance(current, Mapping):
                raise self._invalid(".".join(walked), "an object")
            walked.append(key)
            if key not in current:
                raise EventError(f"{self._where} lacks {'.'.join(walked)}. {self._advice}")
            current = current[key]
        return current

    def _invalid(self, path: str, expected: str) -> EventError:
        return EventError(f"{self._where}: {path} isn't {expected}. {self._advice}")


def _pull_request_ref(fields: _Fields) -> PullRequestRef:
    repo = fields.matching("repository.full_name", REPO_NAME, "owner/name")
    head_repo = fields.find("pull_request.head").get("repo")
    # A deleted fork's head repository is null, so anything but this repository is a fork.
    is_fork = not (isinstance(head_repo, Mapping) and head_repo.get("full_name") == repo)
    return PullRequestRef(
        repo=repo,
        number=fields.positive_integer("pull_request.number"),
        base_sha=fields.matching("pull_request.base.sha", _SHA, "a 40-hex-digit SHA").lower(),
        head_sha=fields.matching("pull_request.head.sha", _SHA, "a 40-hex-digit SHA").lower(),
        is_fork=is_fork,
        is_draft=fields.flag("pull_request.draft"),
        event_at=fields.timestamp("pull_request.updated_at"),
        action=fields.matching("action", _ACTION, "an action name such as opened"),
    )


def _merged_pull(fields: _Fields) -> MergedPull:
    return MergedPull(
        number=fields.positive_integer("number"),
        base_sha=fields.matching("baseRefOid", _SHA, "a 40-hex-digit SHA").lower(),
        head_sha=fields.matching("headRefOid", _SHA, "a 40-hex-digit SHA").lower(),
        merged_at=fields.timestamp("mergedAt"),
        is_cross_repository=fields.boolean("isCrossRepository"),
    )
