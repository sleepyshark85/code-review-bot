"""The ``pull_request`` event payload, parsed into the few fields one run needs.

Only SHAs, numbers, names, flags and a time are kept. No free text from the event, such as the
pull request's title or body, goes any further (Phase 2 design, Safety on the PR path).
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

__all__ = ["MAX_EVENT_BYTES", "PullRequestRef", "read_event"]

# GitHub's own payloads are far smaller; a larger file is refused before it is parsed.
MAX_EVENT_BYTES: Final = 10_000_000
_SHA: Final = re.compile(r"[0-9a-fA-F]{40}")
_REPO: Final = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
_ACTION: Final = re.compile(r"[a-z_]{1,50}")
_ADVICE: Final = "Pass the pull_request event file GitHub Actions writes to GITHUB_EVENT_PATH."


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    """The pull request one event is about.

    ``repo`` is the event's repository, ``owner/name``, and the SHAs are 40 lowercase hex digits.
    ``is_fork`` is true unless the head repository is that repository itself. ``event_at``
    is the pull request's ``updated_at``, and ``action`` the event's action, such as ``opened``.
    """

    repo: str
    number: int
    base_sha: str
    head_sha: str
    is_fork: bool
    is_draft: bool
    event_at: datetime
    action: str


def read_event(path: Path) -> PullRequestRef:
    """Read the event file at ``path``, as GitHub Actions writes it to ``GITHUB_EVENT_PATH``.

    Raises:
        EventError: If the file can't be read, is over 10 MB, isn't a JSON object, or a field
            the run needs is missing or invalid; the message names the field, never its value.
    """
    shown_path = redact(str(path))
    try:
        with path.open("rb") as event_file:
            raw = event_file.read(MAX_EVENT_BYTES + 1)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise EventError(
            f"Can't read the event file --event {shown_path}: {reason}. {_ADVICE}"
        ) from exc
    if len(raw) > MAX_EVENT_BYTES:
        raise EventError(
            f"The event file --event {shown_path} is over {MAX_EVENT_BYTES} bytes. {_ADVICE}"
        )
    # Nesting deeper than the parser can recurse makes the event as unusable as bad syntax.
    try:
        # Not strict, so a control character GitHub left unescaped in the title or body, which
        # is never kept, can't make the whole event unreadable.
        event: object = json.loads(raw, strict=False)
    except (ValueError, RecursionError) as exc:
        raise EventError(f"The event file --event {shown_path} isn't JSON. {_ADVICE}") from exc
    if not isinstance(event, Mapping):
        raise EventError(f"The event file --event {shown_path} isn't a JSON object. {_ADVICE}")
    return _pull_request_ref(_Fields(event, shown_path))


class _Fields:
    """Reads typed fields from the event by their dotted path, raising ``EventError`` naming one.

    The field's value is never shown: it is untrusted, and may be large.
    """

    def __init__(self, event: Mapping[str, object], shown_path: str) -> None:
        self._event = event
        self._shown_path = shown_path

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
        current: object = self._event
        walked: list[str] = []
        for key in path.split("."):
            if not isinstance(current, Mapping):
                raise self._invalid(".".join(walked), "an object")
            walked.append(key)
            if key not in current:
                raise EventError(
                    f"The event file --event {self._shown_path} lacks {'.'.join(walked)}. {_ADVICE}"
                )
            current = current[key]
        return current

    def _invalid(self, path: str, expected: str) -> EventError:
        return EventError(
            f"The event file --event {self._shown_path}: {path} isn't {expected}. {_ADVICE}"
        )


def _pull_request_ref(fields: _Fields) -> PullRequestRef:
    repo = fields.matching("repository.full_name", _REPO, "owner/name")
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
