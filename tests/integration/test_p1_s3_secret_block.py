"""P1-S3: stop a diff with a detected secret from reaching the model.

Written from the spec's criteria and Interfaces, before the developer's code existed. No secret
appears in this file: every value is assembled at run time from pieces (spec Notes), so CI's
gitleaks scan and P0-S1 AC17 see nothing. `secret_scan` is imported per test, so a missing
module fails its own tests instead of the whole file.
"""

import hashlib
import importlib
import string
from types import ModuleType

import pytest
from diff_support import section

pytestmark = pytest.mark.integration

ERROR = "review-bot: error: "
SECRET_EXIT = 16
REDACTED = "[REDACTED]"

UPPER_DIGITS = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits


def chars(alphabet: str, count: int, seed: int = 0) -> str:
    """`count` varied characters from `alphabet`, with no two neighbours alike."""
    return "".join(alphabet[(seed + 7 * index) % len(alphabet)] for index in range(count))


def aws(seed: int = 0, length: int = 16) -> str:
    return "AK" + "IA" + chars(UPPER_DIGITS, length, seed)


def github(prefix: str = "p_", seed: int = 0) -> str:
    return "gh" + prefix + chars(ALNUM, 36, seed)


def anthropic(seed: int = 0) -> str:
    return "sk" + "-ant-" + chars(ALNUM + "_-", 24, seed)


def openai(seed: int = 0) -> str:
    return "sk" + "-" + chars(ALNUM, 24, seed + 3)


def slack(seed: int = 0) -> str:
    return "xo" + "xb-" + chars(ALNUM + "-", 14, seed)


def credential(seed: int = 0) -> str:
    """A 20-character literal holding a digit, with none of the placeholder words."""
    return chars(ALNUM, 20, seed + 5)


def pem_lines(seed: int = 0, *, pgp: bool = False) -> tuple[str, str, str]:
    """A private-key block's BEGIN line, one body line and its END line."""
    label = "PGP " + "PRIVATE" + " KEY BLOCK" if pgp else "RSA " + "PRIVATE" + " KEY"
    return (
        "-----BEGIN " + label + "-----",
        chars(ALNUM + "+/", 48, seed),
        "-----END " + label + "-----",
    )


def secret_scan() -> ModuleType:
    return importlib.import_module("review_bot.secret_scan")


def line_of(text: str, needle: str) -> int:
    """The 1-based line of the diff file holding `needle`."""
    return text[: text.index(needle)].count("\n") + 1


def kind_of(hit) -> str:
    return str(getattr(hit.kind, "value", hit.kind))


def assert_blocked(run, provider, *values: str) -> str:
    """Exit 16, nothing on stdout, no request, no value anywhere; return the one stderr line."""
    assert run.exit_code == SECRET_EXIT, run.stderr
    assert run.stdout == ""
    assert provider.requests == []
    for value in values:
        assert value not in run.stdout
        assert value not in run.stderr
    lines = run.stderr.splitlines()
    assert len(lines) == 1, run.stderr
    assert lines[0].startswith(ERROR), run.stderr
    return lines[0]


def blocked_line(bot, write_diff, text: str, *values: str, flags: tuple[str, ...] = ()) -> str:
    path = write_diff(text)
    provider = bot.mock()

    run = bot.review(path, *flags, provider=provider)

    line = assert_blocked(run, provider, *values)
    assert str(path) not in line
    return line


def assert_reviewed(bot, write_diff, text: str, line: str) -> None:
    """The diff was sent: exit 1 from the mock's canned finding, and `line` reached the prompt."""
    request = bot.request_for(write_diff(text))
    assert line in request.user_prompt


def added(line: str, path: str = "app.py") -> str:
    return section(path, f"+{line}\n")


# AC1: the seven kinds, each on an added line.

KINDS = {
    "aws-access-key": aws(),
    "github-token": github(),
    "anthropic-key": anthropic(),
    "openai-key": openai(),
    "slack-token": slack(),
}


@pytest.mark.parametrize(("kind", "value"), KINDS.items(), ids=KINDS.keys())
def test_p1_s3_ac1_vendor_kind_on_an_added_line_exits_16_naming_kind_and_line(
    bot, write_diff, kind, value
):
    text = added(f'value = "{value}"')

    line = blocked_line(bot, write_diff, text, value)

    assert f"{kind} at diff line {line_of(text, value)}" in line


def test_p1_s3_ac1_private_key_on_added_lines_exits_16_naming_kind_and_line(bot, write_diff):
    begin, body, end = pem_lines()
    text = section("app.py", f"+{begin}\n+{body}\n+{end}\n")

    line = blocked_line(bot, write_diff, text, begin, body, end)

    assert f"private-key at diff line {line_of(text, begin)}" in line


def test_p1_s3_ac1_assigned_credential_on_an_added_line_exits_16_naming_kind_and_line(
    bot, write_diff
):
    value = credential()
    text = added(f'password = "{value}"')

    line = blocked_line(bot, write_diff, text, value)

    assert f"assigned-credential at diff line {line_of(text, value)}" in line


ASSIGNMENT_FORMS = {
    "json": '"api_key": "{}"',
    "typed": 'token: str = "{}"',
    "yaml": 'password: "{}"',
    "yaml_trailing_comma": 'password: "{}",',
}


@pytest.mark.parametrize("form", ASSIGNMENT_FORMS.values(), ids=ASSIGNMENT_FORMS.keys())
def test_p1_s3_ac1_assignment_form_with_a_digit_is_an_assigned_credential(bot, write_diff, form):
    value = credential(seed=11)
    assert any(char.isdigit() for char in value)
    text = added(form.format(value))

    line = blocked_line(bot, write_diff, text, value)

    assert f"assigned-credential at diff line {line_of(text, value)}" in line


def other_quote_inside(other: str) -> str:
    """A 20-character literal holding a digit and the quote character it isn't opened with."""
    return chars(ALNUM, 6, 40) + other + chars(ALNUM, 13, 41)


OTHER_QUOTE_FORMS = {"double_holding_single": ('"', "'"), "single_holding_double": ("'", '"')}


@pytest.mark.parametrize(("opening", "other"), OTHER_QUOTE_FORMS.values(), ids=OTHER_QUOTE_FORMS)
def test_p1_s3_ac1_literal_holding_the_other_quote_is_an_assigned_credential(
    bot, write_diff, opening, other
):
    value = other_quote_inside(other)
    assert any(char.isdigit() for char in value)
    text = added(f"password = {opening}{value}{opening}")

    line = blocked_line(bot, write_diff, text, value)

    assert f"assigned-credential at diff line {line_of(text, value)}" in line


def test_p1_s3_ac1_pgp_private_key_block_is_a_private_key(bot, write_diff):
    begin, body, end = pem_lines(seed=12, pgp=True)
    text = section("app.py", f"+{begin}\n+{body}\n+{end}\n")

    line = blocked_line(bot, write_diff, text, begin, body, end)

    assert f"private-key at diff line {line_of(text, begin)}" in line


def test_p1_s3_ac1_private_key_line_has_no_prefix_boundary(bot, write_diff):
    begin, body, _ = pem_lines(seed=13)
    text = added(f"key=x{begin}{body}")

    line = blocked_line(bot, write_diff, text, begin, body)

    assert f"private-key at diff line {line_of(text, begin)}" in line


UPPERCASE_PLACEHOLDERS = {"XXX": "XXX", "Example": "Exam" + "ple", "YOUR": "YO" + "UR"}


@pytest.mark.parametrize("word", UPPERCASE_PLACEHOLDERS.values(), ids=UPPERCASE_PLACEHOLDERS)
def test_p1_s3_ac1_placeholder_word_not_in_lowercase_is_still_detected(bot, write_diff, word):
    value = chars(ALNUM, 8, 50) + word + chars(ALNUM, 8, 51)
    text = added(f'token = "{value}"')

    line = blocked_line(bot, write_diff, text, value)

    assert f"assigned-credential at diff line {line_of(text, value)}" in line


def test_p1_s3_ac1_interfaces_single_hit_line_is_exact(bot, write_diff):
    key = aws(seed=10)
    lines = ["diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py", "@@ -0,0 +1,8 @@"]
    lines += ["+x = 1"] * (11 - len(lines))
    lines.append(f'+value = "{key}"')
    text = "\n".join(lines) + "\n"
    assert line_of(text, key) == 12

    line = blocked_line(bot, write_diff, text, key)

    assert line == (
        "review-bot: error: secret: 1 possible secret in the diff (aws-access-key at diff line "
        "12), so it was not sent to the model. Remove it and rotate it if it is real, then run "
        "again."
    )


# AC1, beyond its list: the other forms in the Interfaces table.

MORE_FORMS = {
    "gho": ("github-token", github("o_", seed=1)),
    "ghu": ("github-token", github("u_", seed=2)),
    "ghs": ("github-token", github("s_", seed=3)),
    "ghr": ("github-token", github("r_", seed=4)),
    "github_pat": ("github-token", "github" + "_pat_" + chars(ALNUM + "_", 22, 5)),
    "xoxa": ("slack-token", "xo" + "xa-" + chars(ALNUM, 10, 6)),
    "xoxp": ("slack-token", "xo" + "xp-" + chars(ALNUM, 10, 7)),
    "xoxr": ("slack-token", "xo" + "xr-" + chars(ALNUM, 10, 8)),
    "xoxs": ("slack-token", "xo" + "xs-" + chars(ALNUM, 10, 9)),
}


@pytest.mark.parametrize(("kind", "value"), MORE_FORMS.values(), ids=MORE_FORMS.keys())
def test_p1_s3_ac1_interfaces_form_is_detected_as_its_kind(bot, write_diff, kind, value):
    text = added(f'value = "{value}"')

    line = blocked_line(bot, write_diff, text, value)

    assert f"{kind} at diff line {line_of(text, value)}" in line


CREDENTIAL_NAMES = {
    "upper_after_underscore": 'DB_PASSWORD = "{}"',
    "after_dot": 'self.api_key = "{}"',
    "after_hyphen_yaml": 'client-secret: "{}"',
    "apikey": 'APIKEY = "{}"',
    "passwd": 'passwd = "{}"',
}


@pytest.mark.parametrize("form", CREDENTIAL_NAMES.values(), ids=CREDENTIAL_NAMES.keys())
def test_p1_s3_ac1_credential_name_forms_are_assigned_credentials(bot, write_diff, form):
    value = credential(seed=17)
    text = added(form.format(value))

    line = blocked_line(bot, write_diff, text, value)

    assert f"assigned-credential at diff line {line_of(text, value)}" in line


def test_p1_s3_ac1_crlf_diff_names_the_same_diff_line(bot, write_diff):
    value = aws(seed=3)
    text = (section("a.py") + added(f'value = "{value}"')).replace("\n", "\r\n")

    line = blocked_line(bot, write_diff, text, value)

    assert f"aws-access-key at diff line {line_of(text, value)}" in line


def test_p1_s3_ac1_interfaces_example_line_is_exact(bot, write_diff):
    key = aws(seed=9)
    begin, body, end = pem_lines(seed=2)
    lines = ["diff --git a/app.py b/app.py", "--- a/app.py", "+++ b/app.py", "@@ -0,0 +1,40 @@"]
    lines += ["+x = 1"] * (11 - len(lines))
    lines.append(f'+value = "{key}"')
    lines += ["+x = 1"] * (39 - len(lines))
    lines += [f"+{begin}", f"+{body}", f"+{end}"]
    text = "\n".join(lines) + "\n"
    assert (line_of(text, key), line_of(text, begin)) == (12, 40)

    line = blocked_line(bot, write_diff, text, key, begin, body)

    assert line == (
        "review-bot: error: secret: 2 possible secrets in the diff (aws-access-key at diff line "
        "12; private-key at diff line 40), so it was not sent to the model. Remove them and "
        "rotate any that are real, then run again."
    )


def test_p1_s3_ac1_scan_secrets_returns_kind_and_line_but_never_the_value():
    value = aws(seed=4)
    text = added(f'value = "{value}"')

    hits = secret_scan().scan_secrets(text)

    assert [(kind_of(hit), hit.diff_line) for hit in hits] == [
        ("aws-access-key", line_of(text, value))
    ]
    assert value not in repr(hits)


def test_p1_s3_ac1_scan_secrets_finds_nothing_in_a_clean_diff():
    assert tuple(secret_scan().scan_secrets(section("app.py", "+total = 1\n"))) == ()


# AC2: wherever the value sits in the file, it's blocked.

APP = section("app.py", "+def total():\n+    return 1\n")


def preamble(value: str) -> str:
    return (
        "From 1234567890abcdef Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] Rotate the key\n"
        "\n"
        f"The old key was {value}\n"
        "---\n"
        " app.py | 2 +-\n"
        "\n"
    ) + APP


def combined(body: str) -> str:
    """A `diff --cc` section of a merge: two marker columns, one per parent."""
    return (
        "diff --cc app.py\nindex 1111111,2222222..3333333\n--- a/app.py\n+++ b/app.py\n"
        "@@@ -1,2 -1,2 +1,2 @@@\n  x = 1\n" + body
    )


PLACES = {
    "added_line": lambda v: section("app.py", f'+value = "{v}"\n'),
    "removed_line": lambda v: (
        f'--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,1 @@\n x = 1\n-value = "{v}"\n'
    ),
    "removed_line_starting_with_the_value": lambda v: (
        f"--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,1 @@\n x = 1\n-{v}\n"
    ),
    "context_line": lambda v: (
        f'--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n value = "{v}"\n x = 1\n+y = 2\n'
    ),
    "commit_preamble": preamble,
    "new_path": lambda v: f"--- a/keys.py\n+++ b/{v}/keys.py\n@@ -1 +1 @@\n-a\n+b\n",
    "skipped_readme": lambda v: section("README.md", f"+The key is {v}\n") + APP,
    "skipped_uv_lock": lambda v: section("uv.lock", f'+token = "{v}"\n') + APP,
    "combined_removed_line": lambda v: combined(f"--{v}\n"),
    "combined_added_yaml_line": lambda v: combined(f'++password: "{v}"\n'),
    "added_line_starting_with_a_hyphen": lambda v: section("app.py", f"+-{v}\n"),
    "context_line_starting_with_a_hyphen": lambda v: (
        f"--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,3 @@\n -{v}\n x = 1\n+y = 2\n"
    ),
    "unreadable_hunk_header": lambda v: (
        f'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ garbage @@\n x\n+value = "{v}"\n'
    ),
}
PLACE_VALUES = {"aws": aws(seed=13), "github": github(seed=13)}


@pytest.mark.parametrize("value", PLACE_VALUES.values(), ids=PLACE_VALUES.keys())
@pytest.mark.parametrize("build", PLACES.values(), ids=PLACES.keys())
def test_p1_s3_ac2_value_anywhere_in_the_file_exits_16_without_the_value(
    bot, write_diff, build, value
):
    blocked_line(bot, write_diff, build(value), value)


def test_p1_s3_ac2_combined_added_yaml_credential_counts_as_first_on_its_line(bot, write_diff):
    value = credential(seed=60)
    text = combined(f'++password: "{value}"\n')

    line = blocked_line(bot, write_diff, text, value)

    assert f"assigned-credential at diff line {line_of(text, value)}" in line


# AC3: more than five hits.


def test_p1_s3_ac3_seven_hits_name_the_first_five_and_the_total(bot, write_diff):
    values = [aws(seed=seed) for seed in range(7)]
    text = section("app.py", "".join(f'+key_{n} = "{v}"\n' for n, v in enumerate(values)))
    numbers = [line_of(text, value) for value in values]

    line = blocked_line(bot, write_diff, text, *values)

    for number in numbers[:5]:
        assert f"aws-access-key at diff line {number}" in line
    for number in numbers[5:]:
        assert f"diff line {number}" not in line
    assert "7 possible secrets" in line


# AC4: the scan runs before the parser, and after the size check.


def test_p1_s3_ac4_non_diff_text_with_a_secret_exits_16_not_15(bot, write_diff):
    value = aws(seed=21)

    blocked_line(bot, write_diff, f"notes for the release: {value}\n", value)


OVERSIZED = {
    "value_within_the_limit": lambda v: f"# {v}\n" + section("app.py", "+x = 1\n" * 4),
    "value_past_the_limit": lambda v: section("app.py", "+x = 1\n" * 4 + f'+value = "{v}"\n'),
}
LIMIT = 64


@pytest.mark.parametrize("build", OVERSIZED.values(), ids=OVERSIZED.keys())
def test_p1_s3_ac4_secret_in_an_oversized_diff_exits_6(bot, write_diff, build):
    value = aws(seed=22)
    path = write_diff(build(value))
    assert path.stat().st_size > LIMIT
    provider = bot.mock()

    run = bot.review(path, "--max-diff-bytes", str(LIMIT), provider=provider)

    assert run.exit_code == 6, run.stderr
    assert run.stdout == ""
    assert provider.requests == []
    assert value not in run.stderr
    assert len(run.stderr.splitlines()) == 1, run.stderr


# AC5: the threshold doesn't lower the block.


def test_p1_s3_ac5_threshold_critical_still_exits_16(bot, write_diff):
    value = credential(seed=23)

    blocked_line(
        bot, write_diff, added(f'password = "{value}"'), value, flags=("--threshold", "critical")
    )


# AC6: near misses are reviewed normally.

NEAR_MISSES = {
    "aws_15_chars": f'access_key_id = "{aws(length=15)}"',
    "password_changeme": 'password = "changeme"',
    "password_short": 'password = "short"',
    "api_key_placeholder": 'api_key = "your-api-key-goes-here"',
    "api_key_from_environ": 'api_key = os.environ["KEY"]',
    "token_from_call": "token = get_token()",
    "openai_replace_me": 'openai.api_key = "' + "sk" + '-REPLACE-ME"',
    "sha256_digest": f'checksum = "{hashlib.sha256(b"review-bot").hexdigest()}"',
    "parameter_annotation": 'def f(token: "AccessTokenProvider")',
    "yaml_type_annotation": 'token: "AccessTokenProvider"',
    "optional_annotation": 'token: "Optional[AccessToken]"',
    "dotted_annotation": 'token: "auth.AccessTokenClass"',
    "tokenizer_name": 'tokenizer_name = "bert-base-uncased-v2"',
    "token_url": 'TOKEN_URL = "https://auth.acme.com/oauth"',
    "task_queue": 'queue_name = "task-queue-worker-for-billing"',
    "secret_with_spaces": 'secret = "a sentence with spaces in it"',
}


@pytest.mark.parametrize("line", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_p1_s3_ac6_near_miss_is_reviewed_normally(bot, write_diff, line):
    assert_reviewed(bot, write_diff, added(line), line)


@pytest.mark.parametrize("line", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_p1_s3_ac6_near_miss_gives_no_hit(line):
    assert tuple(secret_scan().scan_secrets(added(line))) == ()


def test_p1_s3_ac6_bare_task_queue_name_is_reviewed_normally(bot, write_diff):
    line = "task-queue-worker-for-billing"

    assert_reviewed(bot, write_diff, added(line), line)


# AC6, beyond its list: the Interfaces table's boundaries and exclusions.

BOUNDARY_MISSES = {
    "aws_17_chars": f'value = "{aws(length=17)}"',
    "aws_after_a_letter": f'value = "x{aws()}"',
    "aws_after_an_underscore": f'value = "_{aws()}"',
    "github_37_chars": f'value = "{github()}Z"',
    "github_after_a_hyphen": f'value = "-{github()}"',
    "openai_without_a_digit": 'value = "' + "sk" + "-" + chars(string.ascii_letters, 24) + '"',
    "openai_after_a_letter": 'value = "ta' + "sk" + "-" + chars(ALNUM, 24, 3) + '"',
    "slack_9_chars": 'value = "' + "xo" + "xb-" + chars(ALNUM, 9) + '"',
    "credential_15_chars": f'password = "{credential()[:15]}"',
    "credential_http_url": 'token = "http://' + chars(ALNUM, 20) + '"',
    "credential_example": 'token = "example' + chars(ALNUM, 16) + '"',
    "credential_placeholder": 'secret = "' + chars(ALNUM, 8) + "placeholder" + '"',
    "credential_xxx": 'api_key = "' + chars(ALNUM, 8) + "xxx" + chars(ALNUM, 8, 1) + '"',
    "credential_angle": 'api_key = "<' + chars(ALNUM, 18) + '>"',
    "credential_template": 'password = "${' + chars(ALNUM, 18) + '}"',
    "name_inside_a_word": 'tokens = "' + credential() + '"',
    "name_ending_after_a_letter": 'mytoken = "' + credential() + '"',
    "unquoted_name_colon_mid_line": 'x = {token: "' + credential() + '"}',
    "parameter_list_with_a_digit": 'def f(token: "Token2ProviderClass"): ...',
}


@pytest.mark.parametrize("line", BOUNDARY_MISSES.values(), ids=BOUNDARY_MISSES.keys())
def test_p1_s3_ac6_interfaces_boundary_is_reviewed_normally(bot, write_diff, line):
    assert_reviewed(bot, write_diff, added(line), line)


# Spec Notes: the known false-positive cost.


def test_p1_s3_notes_type_like_string_with_a_digit_in_a_multiline_list_exits_16(bot, write_diff):
    text = section("app.py", "+def f(\n+    token: " + '"Token2ProviderClass",\n+):\n')

    line = blocked_line(bot, write_diff, text, "Token2ProviderClass")

    assert "assigned-credential at diff line 6" in line


# AC7: redact.

REDACT_VALUES = {
    **KINDS,
    "github_pat": MORE_FORMS["github_pat"][1],
    "slack_xoxp": MORE_FORMS["xoxp"][1],
}


@pytest.mark.parametrize("value", REDACT_VALUES.values(), ids=REDACT_VALUES.keys())
def test_p1_s3_ac7_redact_replaces_each_vendor_value(value):
    text = f'+value = "{value}"\nkept line\nagain {value} here\n'

    assert secret_scan().redact(text) == (
        f'+value = "{REDACTED}"\nkept line\nagain {REDACTED} here\n'
    )


@pytest.mark.parametrize("form", ASSIGNMENT_FORMS.values(), ids=ASSIGNMENT_FORMS.keys())
def test_p1_s3_ac7_redact_replaces_an_assigned_credential_value_only(form):
    value = credential(seed=31)
    text = "before\n" + form.format(value) + "\nafter\n"

    assert secret_scan().redact(text) == "before\n" + form.format(REDACTED) + "\nafter\n"


def test_p1_s3_ac7_redact_replaces_a_whole_pem_block_with_one_marker():
    begin, body, end = pem_lines(seed=5)
    text = f"before\n{begin}\n{body}\n{body[::-1]}\n{end}\nafter\n"

    assert secret_scan().redact(text) == f"before\n{REDACTED}\nafter\n"


def test_p1_s3_ac7_redact_replaces_a_whole_pgp_block_with_one_marker():
    begin, body, end = pem_lines(seed=14, pgp=True)
    text = f"before\n{begin}\n\n{body}\n{end}\nafter\n"

    assert secret_scan().redact(text) == f"before\n{REDACTED}\nafter\n"


@pytest.mark.parametrize(("opening", "other"), OTHER_QUOTE_FORMS.values(), ids=OTHER_QUOTE_FORMS)
def test_p1_s3_ac7_redact_replaces_a_literal_holding_the_other_quote(opening, other):
    value = other_quote_inside(other)
    text = f"password = {opening}{value}{opening}\n"

    assert secret_scan().redact(text) == f"password = {opening}{REDACTED}{opening}\n"


def test_p1_s3_ac7_redact_replaces_a_pem_block_in_added_lines_with_one_marker():
    begin, body, end = pem_lines(seed=6)
    text = f"+before\n+{begin}\n+{body}\n+{end}\n+after\n"

    assert secret_scan().redact(text) == f"+before\n+{REDACTED}\n+after\n"


def test_p1_s3_ac7_redact_pem_block_without_end_runs_to_the_end_of_the_text():
    begin, body, _ = pem_lines(seed=7)
    text = f"before\n{begin}\n{body}\n{body[::-1]}\n"

    assert secret_scan().redact(text) == f"before\n{REDACTED}"


def test_p1_s3_ac7_redact_pem_end_without_begin_runs_from_the_start_of_the_text():
    _, body, end = pem_lines(seed=8)
    text = f"{body}\n{body[::-1]}\n{end}\nafter\n"

    assert secret_scan().redact(text) == f"{REDACTED}\nafter\n"


CLEAN_TEXTS = {
    "empty": "",
    "plain_diff": APP,
    "near_misses": "\n".join(NEAR_MISSES.values()) + "\n",
    "crlf": "a\r\nb = 1\r\n",
}


@pytest.mark.parametrize("text", CLEAN_TEXTS.values(), ids=CLEAN_TEXTS.keys())
def test_p1_s3_ac7_redact_returns_text_without_a_secret_unchanged(text):
    assert secret_scan().redact(text) == text
