"""Unit tests for how a review's diff paths are named and written."""

import pytest

from review_bot.diff_parser import FileDiff, SkipReason
from review_bot.report import path_fields, section_path

# Assembled, so no key marker is written literally.
KEY_END_MARKER = "-" * 5 + "END RSA " + "PRIVATE KEY" + "-" * 5


@pytest.mark.parametrize(
    ("path", "old_path", "named"),
    [("new.md", "old.md", "new.md"), (None, "old.md", "old.md"), (None, None, "/dev/null")],
    ids=["new-path", "deleted", "no-path"],
)
def test_section_path_names_a_file_by_its_new_then_old_path(
    path: str | None, old_path: str | None, named: str
) -> None:
    assert section_path(FileDiff(path, old_path, (), SkipReason.DOCS)) == named


def test_path_fields_with_a_valid_utf8_path_writes_it_plain_with_no_flag() -> None:
    assert path_fields("docs/café.md") == {"file": "docs/café.md"}


def test_path_fields_with_a_byte_that_is_not_utf8_writes_gits_quoted_form() -> None:
    path = b"docs/caf\xff.md".decode("utf-8", "surrogateescape")

    assert path_fields(path) == {"file": r'"docs/caf\377.md"', "path_quoted": True}


def test_path_fields_leaves_a_key_marker_in_a_path_unredacted() -> None:
    path = f"x{KEY_END_MARKER}.py"

    assert path_fields(path) == {"file": path}
