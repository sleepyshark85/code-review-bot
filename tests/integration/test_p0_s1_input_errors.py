"""P0-S1 AC7 to AC11: input errors stop before the provider, and the limit is exact."""

import builtins
import io
import os
import pathlib
import re
from pathlib import Path

import pytest
from diff_support import MINIMAL_DIFF, section

pytestmark = pytest.mark.integration

DEFAULT_LIMIT_BYTES = 400_000


def assert_stopped_before_the_provider(run, provider, *, exit_code):
    """In AC7 to AC10: no provider call, and none of what only a successful review prints."""
    assert run.exit_code == exit_code, run.stderr
    assert provider.requests == []
    assert run.stdout == ""
    stderr = run.stderr.lower()
    assert "canned" not in stderr
    assert "mockprovider" not in stderr
    assert "provider=mock" not in stderr
    assert "input_tokens=" not in stderr


def contains_number(text: str, number: int) -> bool:
    return re.search(rf"(?<![\d,]){number}(?![\d,])", text) is not None


@pytest.fixture
def deny_reads(monkeypatch):
    """Make every way of opening one path raise `PermissionError`; nothing else is affected."""

    def deny(target: Path) -> None:
        def refusing(original):
            def wrapper(file, *args, **kwargs):
                if isinstance(file, str | os.PathLike) and Path(file) == target:
                    raise PermissionError(13, "Permission denied", str(target))
                return original(file, *args, **kwargs)

            return wrapper

        for owner, name in (
            (builtins, "open"),
            (io, "open"),
            (os, "open"),
            (pathlib.Path, "open"),
            (pathlib.Path, "read_text"),
            (pathlib.Path, "read_bytes"),
        ):
            monkeypatch.setattr(owner, name, refusing(getattr(owner, name)))

    return deny


@pytest.mark.parametrize(
    "name", ["missing.diff", "with space/missing file.diff"], ids=["plain", "with_spaces"]
)
def test_p0_s1_ac7_missing_path_exits_3(bot, tmp_path, name):
    path = tmp_path / name
    provider = bot.mock()

    run = bot.review(path, provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=3)
    assert str(path) in run.stderr


def test_p0_s1_ac8_directory_exits_4(bot, tmp_path):
    provider = bot.mock()

    run = bot.review(tmp_path, provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=4)
    assert str(tmp_path) in run.stderr


def test_p0_s1_ac8_utf16_from_powershell_redirection_exits_4(bot, write_diff):
    path = write_diff(bytes([0xFF, 0xFE, 0x41, 0x00]))
    provider = bot.mock()

    run = bot.review(path, provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=4)
    assert str(path) in run.stderr


@pytest.mark.parametrize(
    "content",
    [b"caf\xc3", b"+valid line\n\x80\n", b"\xff"],
    ids=["truncated_sequence", "invalid_byte_after_valid_text", "single_invalid_byte"],
)
def test_p0_s1_ac8_other_invalid_utf8_exits_4(bot, write_diff, content):
    path = write_diff(content)
    provider = bot.mock()

    run = bot.review(path, provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=4)
    assert str(path) in run.stderr


def test_p0_s1_ac8_permission_error_on_read_exits_4(bot, write_diff, deny_reads):
    path = write_diff(MINIMAL_DIFF)
    provider = bot.mock()
    deny_reads(path)
    with pytest.raises(PermissionError):
        path.read_bytes()

    run = bot.review(path, provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=4)
    assert str(path) in run.stderr


@pytest.mark.parametrize(
    "content",
    [b"", b" \n\t\n", b"\n", b" ", b"\r\n \r\n"],
    ids=["zero_bytes", "spec_whitespace", "single_newline", "single_space", "crlf_whitespace"],
)
def test_p0_s1_ac9_empty_or_whitespace_only_exits_5(bot, write_diff, content):
    path = write_diff(content)
    provider = bot.mock()

    run = bot.review(path, provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=5)
    assert str(path) in run.stderr


def test_p0_s1_ac10_one_byte_over_the_flag_limit_exits_6_and_states_both_sizes(
    bot, write_diff, tmp_path
):
    path = write_diff(b"x" * 11)
    provider = bot.mock()

    run = bot.review(path, "--max-diff-bytes", "10", provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=6)
    # The temporary path can hold digits of its own, which mustn't satisfy these two checks.
    message = run.stderr.replace(str(path), "").replace(str(tmp_path), "")
    assert contains_number(message, 11), run.stderr
    assert contains_number(message, 10), run.stderr


def test_p0_s1_ac10_one_byte_over_the_default_limit_exits_6(bot, write_diff):
    provider = bot.mock()

    run = bot.review(write_diff(b"x" * (DEFAULT_LIMIT_BYTES + 1)), provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=6)


def test_p0_s1_ac10_the_limit_counts_bytes_not_characters(bot, write_diff):
    # Six characters and twelve bytes: under the limit if characters were counted.
    provider = bot.mock()

    run = bot.review(write_diff("é" * 6), "--max-diff-bytes", "10", provider=provider)

    assert_stopped_before_the_provider(run, provider, exit_code=6)


def test_p0_s1_ac10_exactly_the_default_limit_is_accepted(bot, write_diff):
    # Since P1-S2 the file must be a real diff, so one added line pads it to the limit.
    header = section("app.py", "")
    content = header + "+" + "x" * (DEFAULT_LIMIT_BYTES - len(header) - 2) + "\n"
    assert len(content.encode("utf-8")) == DEFAULT_LIMIT_BYTES
    provider = bot.mock(findings=[])

    run = bot.review(write_diff(content), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert content in provider.requests[0].user_prompt


# AC11 as changed by P1-S2: "a diff exactly `--max-diff-bytes` bytes long", so each limit is the
# diff's own size.
@pytest.mark.parametrize(
    "content",
    [
        section("app.py", "+abcdefghij\n"),
        section("app.py", "+ééééé\n"),
        section("app.py", "+abc\n+def\n").replace("\n", "\r\n"),
    ],
    ids=["ascii", "multi_byte", "crlf_line_endings"],
)
def test_p0_s1_ac11_file_at_the_limit_reaches_the_provider_whole(bot, write_diff, content):
    path = write_diff(content)
    limit = len(content.encode("utf-8"))
    assert path.stat().st_size == limit
    provider = bot.mock(findings=[])

    run = bot.review(path, "--max-diff-bytes", str(limit), provider=provider)

    assert run.exit_code == 0, run.stderr
    assert content in provider.requests[0].user_prompt
