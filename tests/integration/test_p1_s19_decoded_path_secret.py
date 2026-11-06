"""P1-S19: stop a secret spelt in a quoted path's escapes from reaching stderr or the model.

Written from the spec's criteria and Limits, before the developer's code existed, through the
public interfaces only: `main`, `parse_diff` and `scan_secrets`. No secret appears in this file:
each value `V`, and its escaped form `E(V)`, is assembled at run time (P1-S3 Notes), so CI's
gitleaks scan and P0-S1 AC17 see nothing.
"""

import json
import re
import string
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from diff_support import MINIMAL_DIFF
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from review_bot.diff_parser import parse_diff
from review_bot.errors import DiffNotParsableError
from review_bot.secret_scan import scan_secrets

pytestmark = pytest.mark.integration

ERROR = "review-bot: error: "
WARNING = "review-bot: warning: nothing was reviewed: every changed file was skipped: "
SECRET_EXIT = 16
REDACTED = "[REDACTED]"
HIT = re.compile(r"([a-z-]+) at diff line (\d+)")

UPPER_DIGITS = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits


def chars(alphabet: str, count: int, seed: int = 0) -> str:
    """`count` varied characters from `alphabet`, with no two neighbours alike."""
    return "".join(alphabet[(seed + 7 * index) % len(alphabet)] for index in range(count))


def aws(seed: int = 0, length: int = 16) -> str:
    return "AK" + "IA" + chars(UPPER_DIGITS, length, seed)


def github(seed: int = 0, length: int = 36) -> str:
    return "gh" + "p_" + chars(ALNUM, length, seed)


def literal(seed: int = 0, length: int = 16) -> str:
    """A credential literal holding a digit, with none of the placeholder words."""
    value = chars(ALNUM, length, seed + 5)
    assert any(char.isdigit() for char in value)
    return value


def escaped(text: str) -> str:
    """`E(x)`: every UTF-8 byte of `text` as a three-digit octal escape, as git quotes a path."""
    return "".join(f"\\{byte:03o}" for byte in text.encode("utf-8"))


# The seven P1-S3 kinds, each as a form that may sit in a file name.
KIND_FORMS = {
    "aws-access-key": aws(seed=1),
    "github-token": github(seed=2),
    "anthropic-key": "sk" + "-ant-" + chars(ALNUM + "_-", 24, 3),
    "openai-key": "sk" + "-" + chars(ALNUM, 24, 7),
    "slack-token": "xo" + "xb-" + chars(ALNUM, 14, 4),
    "private-key": "-----BEGIN RSA " + "PRIVATE" + " KEY-----",
    "assigned-credential": f'token = "{literal(seed=6, length=20)}"',
}


# Running the bot.


def hits_in(line: str) -> list[tuple[str, int]]:
    """Each `(kind, diff line)` that the P1-S3 error line names."""
    return [(kind, int(number)) for kind, number in HIT.findall(line)]


def assert_blocked(run: Any, provider: Any, *values: str) -> str:
    """Exit 16, nothing on stdout, no request, no value anywhere; return the one stderr line."""
    assert run.exit_code == SECRET_EXIT, run.stderr
    assert run.stdout == ""
    assert provider.requests == []
    for value in values:
        assert value not in run.stdout
        assert value not in run.stderr
    lines = run.stderr.splitlines()
    assert len(lines) == 1, run.stderr
    assert lines[0].startswith(ERROR + "secret: "), run.stderr
    return lines[0]


def blocked_line(bot: Any, write_diff: Callable[..., Path], text: str, *values: str) -> str:
    provider = bot.mock()

    run = bot.review(write_diff(text), provider=provider)

    return assert_blocked(run, provider, *values)


def assert_all_skipped(run: Any, provider: Any) -> str:
    """P1-S2 AC9: exit 0, no call, the all-skipped warning; return the paths it names."""
    assert run.exit_code == 0, run.stderr
    assert provider.requests == []
    assert run.stdout == "No findings at or above low.\n"
    warnings = [line for line in run.stderr.splitlines() if line.startswith(WARNING)]
    assert len(warnings) == 1, run.stderr
    assert "model_called=false" in run.stderr.splitlines()[-1]
    return warnings[0].removeprefix(WARNING)


# Diff shapes.


def new_file(escaped_path: str, body: str = "+x\n") -> str:
    """Issue #24's shape: a new file whose `diff --git` and `+++` paths are quoted."""
    count = body.count("\n")
    return (
        f'diff --git "a/{escaped_path}" "b/{escaped_path}"\n'
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f'+++ "b/{escaped_path}"\n'
        f"@@ -0,0 +1,{count} @@\n{body}"
    )


def plain_new_file(path: str, body: str = "+x\n") -> str:
    """The same shape with an unquoted path."""
    count = body.count("\n")
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{count} @@\n{body}"
    )


def plus_path_only(quoted: str, plain: str = "docs/x.md") -> str:
    """A new file whose only quoted string is the `+++` path, `quoted`, on diff line 4."""
    return (
        f"diff --git a/{plain} b/{plain}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ {quoted}\n"
        "@@ -0,0 +1 @@\n+x\n"
    )


PLUS_LINE = 4


# AC1: issue #24's repro.


def test_p1_s19_ac1_issue_24_repro_exits_16_naming_three_hits(bot, write_diff):
    value = aws(seed=10)
    text = new_file(f"docs/{escaped(value)}.md")
    assert value not in text

    line = blocked_line(bot, write_diff, text, value, escaped(value))

    assert line == (
        "review-bot: error: secret: 3 possible secrets in the diff (aws-access-key at diff line "
        "1; aws-access-key at diff line 1; aws-access-key at diff line 5), so it was not sent to "
        "the model. Remove them and rotate any that are real, then run again."
    )


def test_p1_s19_ac1_quoted_repro_line_equals_the_plain_paths_line(bot, write_diff):
    value = aws(seed=10)
    plain = blocked_line(bot, write_diff, plain_new_file(f"docs/{value}.md"), value)

    quoted = blocked_line(bot, write_diff, new_file(f"docs/{escaped(value)}.md"), value)

    assert quoted == plain


# AC2: wherever the quoted path sits in the header lines, and reviewed or skipped.


def git_header(first: str, rest: str) -> str:
    return f"diff --git {first}\n{rest}"


HUNK = "@@ -0,0 +1 @@\n+x\n"


def placed(where: str, quoted_path: str, plain_path: str) -> tuple[str, int]:
    """A diff whose one quoted string, `"<prefix>quoted_path"`, sits at `where`, and its line."""
    p = plain_path
    q = quoted_path
    shapes = {
        "diff_git_old": (git_header(f'"a/{q}" b/{p}', f"--- a/{p}\n+++ b/{p}\n{HUNK}"), 1),
        "diff_git_new": (git_header(f'a/{p} "b/{q}"', f"--- a/{p}\n+++ b/{p}\n{HUNK}"), 1),
        "diff_cc": (
            f'diff --cc "{q}"\nindex 1111111,2222222..3333333\n--- a/{p}\n+++ b/{p}\n'
            "@@@ -1,1 -1,1 +1,2 @@@\n  x\n++y\n",
            1,
        ),
        "minus_minus_minus": (git_header(f"a/{p} b/{p}", f'--- "a/{q}"\n+++ b/{p}\n{HUNK}'), 2),
        "plus_plus_plus": (git_header(f"a/{p} b/{p}", f'--- a/{p}\n+++ "b/{q}"\n{HUNK}'), 3),
        "rename_from": (
            git_header(
                f"a/{p} b/{p}", f'similarity index 100%\nrename from "{q}"\nrename to {p}\n'
            ),
            3,
        ),
        "rename_to": (
            git_header(
                f"a/{p} b/{p}", f'similarity index 100%\nrename from {p}\nrename to "{q}"\n'
            ),
            4,
        ),
        "copy_from": (
            git_header(f"a/{p} b/{p}", f'similarity index 100%\ncopy from "{q}"\ncopy to {p}\n'),
            3,
        ),
        "copy_to": (
            git_header(f"a/{p} b/{p}", f'similarity index 100%\ncopy from {p}\ncopy to "{q}"\n'),
            4,
        ),
        "binary_old": (
            git_header(f"a/{p} b/{p}", f'Binary files "a/{q}" and b/{p} differ\n'),
            2,
        ),
        "binary_new": (
            git_header(f"a/{p} b/{p}", f'Binary files a/{p} and "b/{q}" differ\n'),
            2,
        ),
        "plain_unified_diff": (f'--- a/{p}\n+++ "b/{q}"\n{HUNK}', 2),
    }
    return shapes[where]


PLACES = (
    "diff_git_old",
    "diff_git_new",
    "diff_cc",
    "minus_minus_minus",
    "plus_plus_plus",
    "rename_from",
    "rename_to",
    "copy_from",
    "copy_to",
    "binary_old",
    "binary_new",
    "plain_unified_diff",
)
# A reviewed file and a skipped one: (directory, extension) of both the quoted and plain paths.
FILE_KINDS = {"reviewed": ("src", ".py"), "skipped": ("docs", ".md")}


@pytest.mark.parametrize("file_kind", FILE_KINDS.values(), ids=FILE_KINDS.keys())
@pytest.mark.parametrize("where", PLACES)
def test_p1_s19_ac2_quoted_path_anywhere_in_the_header_exits_16_naming_its_line(
    bot, write_diff, where, file_kind
):
    directory, extension = file_kind
    if where.startswith("binary"):
        extension = ".bin"
    value = aws(seed=20)
    text, line_number = placed(
        where, f"{directory}/{escaped(value)}{extension}", f"{directory}/x{extension}"
    )
    assert value not in text

    line = blocked_line(bot, write_diff, text, value, escaped(value))

    hits = hits_in(line)
    assert hits, line
    assert {number for _, number in hits} == {line_number}, line
    assert {kind for kind, _ in hits} == {"aws-access-key"}, line


def test_p1_s19_ac2_reviewed_file_src_path_exits_16(bot, write_diff):
    value = aws(seed=21)

    line = blocked_line(bot, write_diff, new_file(f"src/{escaped(value)}.py"), value)

    assert {number for _, number in hits_in(line)} == {1, 5}, line


# AC2, beyond its list: other header lines, line endings and later files.


def beyond_ac2(where: str, q: str) -> tuple[str, int]:
    """A diff holding the quoted string `"q"`, first at `where`, and that first diff line."""
    shapes = {
        "diff_combined": (
            f'diff --combined "{q}"\n--- a/docs/x.md\n+++ b/docs/x.md\n'
            "@@@ -1,1 -1,1 +1,2 @@@\n  x\n++y\n",
            1,
        ),
        # A header line git never writes: still between the file start and its first `@@`.
        "unknown_header_line": (
            f'diff --git a/docs/x.md b/docs/x.md\nnote "{q}"\n--- a/docs/x.md\n'
            f"+++ b/docs/x.md\n{HUNK}",
            2,
        ),
        "second_file": (
            MINIMAL_DIFF + new_file(q.removeprefix("b/")),
            len(MINIMAL_DIFF.splitlines()) + 1,
        ),
        "crlf_endings": (
            f'diff --git a/docs/x.md b/docs/x.md\r\n--- a/docs/x.md\r\n+++ "{q}"\r\n'
            "@@ -0,0 +1 @@\r\n+x\r\n",
            3,
        ),
    }
    return shapes[where]


@pytest.mark.parametrize(
    "where", ["diff_combined", "unknown_header_line", "second_file", "crlf_endings"]
)
def test_p1_s19_ac2_beyond_quoted_path_exits_16_naming_its_first_line(bot, write_diff, where):
    value = aws(seed=22)
    text, first_line = beyond_ac2(where, f"b/docs/{escaped(value)}.md")
    assert value not in text

    line = blocked_line(bot, write_diff, text, value)

    hits = hits_in(line)
    assert hits, line
    assert min(number for _, number in hits) == first_line, line


# AC3: each of the seven kinds, every byte escaped, in a skipped file's quoted path.


@pytest.mark.parametrize(("kind", "form"), KIND_FORMS.items(), ids=KIND_FORMS.keys())
def test_p1_s19_ac3_fully_escaped_form_of_each_kind_exits_16_naming_it(bot, write_diff, kind, form):
    text = plus_path_only(f'"b/docs/{escaped(form)}.md"')

    line = blocked_line(bot, write_diff, text, form, escaped(form))

    assert (kind, PLUS_LINE) in hits_in(line), line


# AC4: partial escapes, an invalid byte and an escaped line feed before V.


def first_byte_escaped(value: str) -> str:
    return f'"b/docs/\\{ord(value[0]):03o}{value[1:]}.md"'


AC4_PATHS = {
    "only_first_byte_escaped": first_byte_escaped,
    "invalid_utf8_byte_before": lambda v: f'"b/docs/\\377{escaped(v)}.md"',
    "escaped_line_feed_before": lambda v: f'"b/docs/x\\n{escaped(v)}.md"',
}


@pytest.mark.parametrize("build", AC4_PATHS.values(), ids=AC4_PATHS.keys())
def test_p1_s19_ac4_hit_names_the_line_holding_the_quoted_path(bot, write_diff, build):
    value = aws(seed=30)
    text = plus_path_only(build(value))
    assert value not in text

    line = blocked_line(bot, write_diff, text, value)

    assert hits_in(line) == [("aws-access-key", PLUS_LINE)], line


# AC5: a quoted path decoding to no secret behaves as before.

# Each name as written in the quoted path, and as it decodes.
CLEAN_DECODES = {
    "accented": ("caf\\303\\251", "café"),
    "aws_near_miss": (escaped(aws(seed=40, length=15)), aws(seed=40, length=15)),
}


@pytest.mark.parametrize("case", CLEAN_DECODES.values(), ids=CLEAN_DECODES.keys())
def test_p1_s19_ac5_clean_decoded_path_in_a_skipped_file_is_p1_s2_ac9(bot, write_diff, case):
    name, decoded = case
    provider = bot.mock()

    run = bot.review(write_diff(new_file(f"docs/{name}.md")), provider=provider)

    # The decoded path, unredacted: `redact` changes nothing without a secret.
    assert assert_all_skipped(run, provider) == f"docs/{decoded}.md (1 in total)"


@pytest.mark.parametrize("case", CLEAN_DECODES.values(), ids=CLEAN_DECODES.keys())
def test_p1_s19_ac5_clean_decoded_path_in_a_reviewed_file_is_reviewed(bot, write_diff, case):
    name, _ = case
    request = bot.request_for(write_diff(new_file(f"src/{name}.py", "+total = 1\n")))

    assert "+total = 1" in request.user_prompt


# AC6: the decoded paths are scanned only when the raw text is clean.


def raw_hit_and_quoted_path(raw_value: str | None, path_value: str) -> str:
    app_line = f'+key = "{raw_value}"\n' if raw_value else "+key = 1\n"
    app = f"diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -0,0 +1,1 @@\n{app_line}"
    return app + new_file(f"docs/{escaped(path_value)}.md")


def test_p1_s19_ac6_raw_hit_alone_is_named_when_a_path_also_holds_one(bot, write_diff):
    raw_value, path_value = aws(seed=50), aws(seed=51)
    text = raw_hit_and_quoted_path(raw_value, path_value)

    line = blocked_line(bot, write_diff, text, raw_value, path_value)

    assert "1 possible secret in the diff" in line, line
    assert hits_in(line) == [("aws-access-key", 5)], line


def test_p1_s19_ac6_without_the_raw_hit_the_paths_hits_are_named(bot, write_diff):
    path_value = aws(seed=51)
    text = raw_hit_and_quoted_path(None, path_value)

    line = blocked_line(bot, write_diff, text, path_value)

    # The docs file starts on line 6: its `diff --git` holds two strings and its `+++` one.
    assert hits_in(line) == [("aws-access-key", 6), ("aws-access-key", 6), ("aws-access-key", 10)]


# AC7: the audit log of a decoded-path hit, as for a raw hit.


def read_records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def event_names(records: list[dict[str, Any]]) -> list[str]:
    return [f"{record['stage']}.{record['event']}" for record in records]


def scalars(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in scalars(nested)]
    if isinstance(value, list):
        return [item for nested in value for item in scalars(nested)]
    return [value]


def logged_run(bot: Any, write_diff: Callable[..., Path], text: str, log: Path) -> Any:
    provider = bot.mock()
    run = bot.review(write_diff(text), "--audit-log", str(log), provider=provider)
    assert run.exit_code == SECRET_EXIT, run.stderr
    assert provider.requests == []
    return read_records(log)


def test_p1_s19_ac7_records_match_a_raw_hit_and_hold_no_value(bot, write_diff, tmp_path):
    value = aws(seed=10)
    quoted_log, plain_log = tmp_path / "quoted.jsonl", tmp_path / "plain.jsonl"

    records = logged_run(bot, write_diff, new_file(f"docs/{escaped(value)}.md"), quoted_log)
    plain = logged_run(bot, write_diff, plain_new_file(f"docs/{value}.md"), plain_log)

    names = event_names(records)
    assert names == ["run.start", "static.secrets", "agent.skipped", "run.end"], names
    assert "parse.parsed" not in names
    [secrets] = [r["data"] for r in records if event_names([r]) == ["static.secrets"]]
    [plain_secrets] = [r["data"] for r in plain if event_names([r]) == ["static.secrets"]]
    assert secrets == plain_secrets
    assert "aws-access-key" in scalars(secrets)
    assert {1, 5} <= set(scalars(secrets))
    assert "secret_detected" in scalars(records[2]["data"])
    assert SECRET_EXIT in scalars(records[3]["data"])
    raw_log = quoted_log.read_text(encoding="ascii")
    assert value not in raw_log
    assert escaped(value) not in raw_log
    assert json.dumps(escaped(value))[1:-1] not in raw_log


# AC8: each path the AC9 warning names passes `redact`.


def test_p1_s19_ac8_end_marker_path_is_redacted_from_its_start_in_the_warning(bot, write_diff):
    end = "-----END RSA " + "PRIVATE" + " KEY-----"
    provider = bot.mock()

    run = bot.review(write_diff(plain_new_file(f"docs/{end}.md")), provider=provider)

    named = assert_all_skipped(run, provider)
    assert named.startswith(f"{REDACTED}.md"), named
    assert "PRIVATE" not in run.stderr
    assert "docs/" not in named, named


# AC9: a path holding V on the command line is redacted in the error line.


def command_line_failure(code: int, tmp_path: Path, value: str) -> tuple[list[str], Path]:
    """The argv giving exit `code`, and the path holding `value` that it names."""
    path = tmp_path / f"{value}.diff"
    if code == 2:
        good = tmp_path / "x.diff"
        good.write_text(MINIMAL_DIFF, encoding="utf-8")
        return [str(good), str(path), "--provider", "mock"], path
    if code == 4:
        path.mkdir()
    elif code == 5:
        path.write_bytes(b"")
    elif code == 6:
        path.write_text(MINIMAL_DIFF, encoding="utf-8")
        return [str(path), "--provider", "mock", "--max-diff-bytes", "8"], path
    elif code == 15:
        path.write_text("just some prose\n", encoding="utf-8")
    return [str(path), "--provider", "mock"], path


@pytest.mark.parametrize("code", [2, 3, 4, 5, 6, 15])
def test_p1_s19_ac9_command_line_path_holding_v_is_redacted_in_the_error_line(bot, tmp_path, code):
    value = aws(seed=60)
    argv, _ = command_line_failure(code, tmp_path, value)
    provider = bot.mock()

    run = bot.run(*argv, provider=provider)

    assert run.exit_code == code, run.stderr
    assert provider.requests == []
    assert value not in run.stderr
    assert value not in run.stdout
    errors = [line for line in run.stderr.splitlines() if line.startswith(ERROR)]
    assert len(errors) == 1, run.stderr
    assert f"{REDACTED}.diff" in errors[0], errors[0]


# AC10: quoted strings that pair differently from the parser's pick of the new path.


def test_p1_s19_ac10_parsers_pick_of_the_new_path_is_scanned(bot, write_diff):
    value = aws(seed=70)
    text = (
        f'diff --git "x" "y\\" "b/src/{escaped(value)}.py"\n'
        "new file mode 100644\n"
        "@@ -0,0 +1,1 @@\n+x\n"
    )

    line = blocked_line(bot, write_diff, text, value)

    assert {number for _, number in hits_in(line)} == {1}, line


# AC11: property over generated diffs.

SECRET_VALUES = st.sampled_from([aws(seed=80), github(seed=81), aws(seed=82)])


@st.composite
def encoded(draw: st.DrawFn, value: str) -> str:
    """`value` as written in a quoted path: plain, every byte escaped, or some bytes escaped."""
    mode = draw(st.sampled_from(["plain", "all", "some"]))
    if mode == "plain":
        return value
    if mode == "all":
        return escaped(value)
    flags = draw(st.lists(st.booleans(), min_size=len(value), max_size=len(value)))
    return "".join(escaped(c) if flag else c for c, flag in zip(value, flags, strict=True))


AROUND = st.sampled_from(
    ["", "docs/", "src/", "-", "x/", "\\377", "x\\n", "\\t", "\\q", "\\8", "\\400", "a b/", '\\"']
)
ENDINGS = st.sampled_from(["", ".md", ".py", "\\377", "\\\\", "\\", '\\"'])


@st.composite
def header_path(draw: st.DrawFn) -> str:
    """A path token as it may sit in a header line: quoted, unclosed or bare, maybe malformed."""
    body = draw(AROUND) + draw(encoded(draw(SECRET_VALUES))) + draw(ENDINGS)
    side = draw(st.sampled_from(["a/", "b/", ""]))
    quoting = draw(st.sampled_from(["closed", "unclosed", "bare"]))
    if quoting == "closed":
        return f'"{side}{body}"'
    if quoting == "unclosed":
        return f'"{side}{body}'
    return f"{side}{body}"


@st.composite
def generated_diff(draw: st.DrawFn) -> str:
    plain = draw(st.sampled_from(["docs/x.md", "src/x.py", "x.bin"]))
    token = draw(header_path())
    shapes = [
        f"diff --git {token} b/{plain}\n--- a/{plain}\n+++ b/{plain}\n{HUNK}",
        f"diff --git a/{plain} {token}\nnew file mode 100644\n{HUNK}",
        f"diff --git a/{plain} b/{plain}\n--- /dev/null\n+++ {token}\n{HUNK}",
        f"diff --git a/{plain} b/{plain}\n--- {token}\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
        f"diff --git a/{plain} b/{plain}\nsimilarity index 100%\nrename from {plain}\n"
        f"rename to {token}\n",
        f"diff --git a/{plain} b/{plain}\nsimilarity index 90%\ncopy from {token}\n"
        f"copy to {plain}\n",
        f"diff --git a/{plain} b/{plain}\nBinary files {token} and b/{plain} differ\n",
        f"diff --cc {token}\n--- a/{plain}\n+++ b/{plain}\n@@@ -1,1 -1,1 +1,2 @@@\n  x\n++y\n",
        f"--- a/{plain}\n+++ {token}\n{HUNK}",
    ]
    text = draw(st.sampled_from(shapes))
    return text + draw(st.sampled_from(["", MINIMAL_DIFF]))


def yielded_paths(text: str) -> list[str]:
    files = parse_diff(text).files
    return [path for file in files for path in (file.path, file.old_path) if path is not None]


def has_flagged_path(text: str) -> bool:
    """True if a `path` or `old_path` that `parse_diff` yields holds a detected form."""
    try:
        return any(scan_secrets(path) for path in yielded_paths(text))
    except DiffNotParsableError:
        return False


@settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(text=generated_diff())
def test_p1_s19_ac11_property_yielded_path_with_a_detected_form_always_exits_16(
    bot, write_diff, text
):
    # The property is an implication, so an example whose paths hold no detected form passes.
    # Returning, not `assume`, keeps Hypothesis from rejecting the generator as too filtered.
    if not has_flagged_path(text):
        return
    provider = bot.mock()

    run = bot.review(write_diff(text), provider=provider)

    assert run.exit_code == SECRET_EXIT, (text, yielded_paths(text), run.stderr)
    assert provider.requests == []


def test_p1_s19_ac11_generator_yields_paths_with_detected_forms():
    """Guard against a vacuous property: many generated diffs must reach the assertion."""
    examples: list[str] = []

    @settings(max_examples=200, deadline=None, derandomize=True)
    @given(text=generated_diff())
    def collect(text: str) -> None:
        examples.append(text)

    collect()

    assert sum(has_flagged_path(text) for text in examples) >= 20


# AC12: an unquoted parsed path that the raw-text scan misses mid-line.

AC12_TARGETS = {
    "hyphen_before_aws": lambda: f"-{aws(seed=90)}.md",
    "yaml_password_form": lambda: f'password: "{literal(seed=91)}".md',
}


@pytest.mark.parametrize("target", AC12_TARGETS.values(), ids=AC12_TARGETS.keys())
def test_p1_s19_ac12_unquoted_rename_to_path_exits_16_naming_its_line(bot, write_diff, target):
    path = target()
    text = f"diff --git a/x.md b/x.md\nsimilarity index 100%\nrename from x.md\nrename to {path}\n"
    assert not scan_secrets(text), "the raw-text scan must miss it for this criterion"

    line = blocked_line(bot, write_diff, text)

    assert 4 in {number for _, number in hits_in(line)}, line


# Limits: malformed quoting and escapes outside the octal range keep their characters.

KEPT_ESCAPES = {
    # Each decodes to a detected form only if the escape keeps its character.
    "backslash_q_gives_q": f'"b/docs/{github(seed=95, length=35)}\\q.md"',
    "backslash_8_gives_8": f'"b/docs/{aws(seed=96, length=15)}\\8.md"',
    "backslash_400_gives_400": f'"b/docs/{aws(seed=97, length=13)}\\400.md"',
}


@pytest.mark.parametrize("quoted", KEPT_ESCAPES.values(), ids=KEPT_ESCAPES.keys())
def test_p1_s19_limits_unknown_escape_keeps_its_character(bot, write_diff, quoted):
    text = plus_path_only(quoted)
    assert not scan_secrets(text)

    line = blocked_line(bot, write_diff, text)

    assert {number for _, number in hits_in(line)} == {PLUS_LINE}, line


MALFORMED = {
    "no_closing_quote": lambda v: f'"b/docs/{escaped(v)}.md',
    "ends_in_a_lone_backslash": lambda v: f'"b/docs/{escaped(v)}\\',
}


@pytest.mark.parametrize("build", MALFORMED.values(), ids=MALFORMED.keys())
def test_p1_s19_limits_malformed_quoting_doesnt_fail_the_run(bot, write_diff, build):
    value = aws(seed=98)
    provider = bot.mock()

    run = bot.review(write_diff(plus_path_only(build(value))), provider=provider)

    assert run.exit_code in (0, 1, SECRET_EXIT), run.stderr
    assert value not in run.stderr


# Limits: cost on hostile input. Linear work finishes in well under a second; a quadratic pass
# over a 400 KB line would take minutes, so the bound has wide headroom for a slow runner.

TIME_BOUND = 20.0
LIMIT = 400_000


def timed(bot: Any, path: Path, *flags: str) -> tuple[Any, float]:
    provider = bot.mock()
    start = time.perf_counter()
    run = bot.review(path, *flags, provider=provider)
    return run, time.perf_counter() - start


def one_long_header_line(fill: str, tail: str = "") -> str:
    """A skipped docs file whose `diff --git` line is near the 400 KB size limit."""
    rest = "--- /dev/null\n+++ b/docs/x.md\n@@ -0,0 +1 @@\n+x\n"
    head = 'diff --git "a/docs/'
    room = LIMIT - len(head) - len(tail) - len(rest) - 32
    filler = fill * (room // len(fill))
    return f"{head}{filler}{tail} b/docs/x.md\n{rest}"


HOSTILE_LINES = {
    "octal_escapes": lambda: one_long_header_line("\\101", '.md"'),
    "escaped_quotes_no_close": lambda: one_long_header_line('\\"'),
    "short_quoted_strings": lambda: one_long_header_line('" "'),
}


@pytest.mark.parametrize("build", HOSTILE_LINES.values(), ids=HOSTILE_LINES.keys())
def test_p1_s19_limits_400_kb_hostile_header_line_is_linear(bot, write_diff, build):
    text = build()
    assert LIMIT - 100 < len(text.encode()) <= LIMIT

    run, seconds = timed(bot, write_diff(text))

    assert run.exit_code in (0, 1), run.stderr
    assert seconds < TIME_BOUND, f"{seconds:.1f}s"


def test_p1_s19_limits_400_kb_octal_line_ending_in_v_exits_16(bot, write_diff):
    value = aws(seed=99)
    text = one_long_header_line("\\101", f"/{escaped(value)}.md" + '"')

    run, seconds = timed(bot, write_diff(text))

    assert run.exit_code == SECRET_EXIT, run.stderr
    assert value not in run.stderr
    assert seconds < TIME_BOUND, f"{seconds:.1f}s"


def test_p1_s19_limits_10000_files_with_quoted_paths_is_linear(bot, write_diff):
    text = "".join(new_file(f"docs/caf\\303\\251{n}.md") for n in range(10_000))
    size = len(text.encode())

    run, seconds = timed(bot, write_diff(text), "--max-diff-bytes", str(size))

    assert run.exit_code == 0, run.stderr
    assert "skipped_files=10000" in run.stderr.splitlines()[-1]
    assert seconds < TIME_BOUND * 3, f"{seconds:.1f}s"
