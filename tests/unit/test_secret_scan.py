"""Unit tests for `secret_scan`.

No realistic secret may be committed, even here, so every secret is assembled at run time.
"""

import time

import pytest

from review_bot.secret_scan import (
    SecretHit,
    SecretKind,
    SecretSpan,
    path_holds_secret,
    redact,
    scan_secrets,
    secret_line_spans,
)
from review_bot.secret_scan import _Span as Span
from review_bot.secret_scan import _spans_clear_of as spans_clear_of

AWS_KEY = "AK" + "IA" + "ABCDEFGH" + "23456789"
GITHUB_TOKEN = "gh" + "p_" + "a1B2" * 9
GITHUB_PAT = "github" + "_pat_" + "a1_B" * 6
ANTHROPIC_KEY = "s" + "k-ant-" + "api03_" + "a1-B" * 5
OPENAI_KEY = "s" + "k-" + "proj" + "a1_B" * 5
SLACK_TOKEN = "xo" + "xb-" + "1234-abcDEF"
PEM_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_END = "-----END " + "RSA PRIVATE KEY-----"
PGP_BEGIN = "-----BEGIN " + "PGP PRIVATE KEY BLOCK-----"
PGP_END = "-----END " + "PGP PRIVATE KEY BLOCK-----"
LINE_SEPARATOR = chr(0x2028)

CREDENTIAL = "q7" + "Lm" + "Zx92" + "Pw4R" + "tK8v"


def kinds(text: str) -> list[SecretKind]:
    return [hit.kind for hit in scan_secrets(text)]


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f"key = {AWS_KEY}", SecretKind.AWS_ACCESS_KEY),
        (f"auth {GITHUB_TOKEN}", SecretKind.GITHUB_TOKEN),
        (f"auth {GITHUB_PAT}", SecretKind.GITHUB_TOKEN),
        (f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}", SecretKind.ANTHROPIC_KEY),
        (f"OPENAI_API_KEY={OPENAI_KEY}", SecretKind.OPENAI_KEY),
        (f"hook: {SLACK_TOKEN}", SecretKind.SLACK_TOKEN),
        (PEM_BEGIN, SecretKind.PRIVATE_KEY),
        ("-----BEGIN " + "PRIVATE KEY-----", SecretKind.PRIVATE_KEY),
        (PGP_BEGIN, SecretKind.PRIVATE_KEY),
        (f'password = "{CREDENTIAL}"', SecretKind.ASSIGNED_CREDENTIAL),
    ],
    ids=[
        "aws",
        "github",
        "github-pat",
        "anthropic",
        "openai",
        "slack",
        "pem-rsa",
        "pem",
        "pgp",
        "assign",
    ],
)
def test_scan_secrets_detects_each_kind(line: str, kind: SecretKind) -> None:
    assert kinds(f"+{line}\n") == [kind]


@pytest.mark.parametrize(
    "line",
    [
        f'"api_key": "{CREDENTIAL}"',
        f"'db_password': '{CREDENTIAL}'",
        f'token: str = "{CREDENTIAL}"',
        f'self.token: Final[str] = "{CREDENTIAL}"',
        f'password: "{CREDENTIAL}"',
        f'    password: "{CREDENTIAL}",',
        f'  - my-secret: "{CREDENTIAL}"',
        f'client(apikey="{CREDENTIAL}")',
        f'GITHUB_TOKEN = "{CREDENTIAL}"',
        f'openai.api_key = "{CREDENTIAL}"',
        "password = \"P4ss'" + 'w0rd-1234567"',
        f"token = '{CREDENTIAL}\"quoted'",
        f'api_key = "EXAMPLE-{CREDENTIAL}"',
        f'token = "{CREDENTIAL}XXX"',
    ],
    ids=[
        "json",
        "single-quoted-json",
        "typed",
        "typed-attribute",
        "yaml",
        "yaml-trailing-comma",
        "yaml-list",
        "keyword-argument",
        "upper-case",
        "dotted",
        "single-quote-inside",
        "double-quote-inside",
        "upper-case-example",
        "upper-case-xxx",
    ],
)
def test_scan_secrets_detects_an_assigned_credential(line: str) -> None:
    assert kinds(f"+{line}\n") == [SecretKind.ASSIGNED_CREDENTIAL]


@pytest.mark.parametrize(
    "line",
    [
        "AK" + "IA" + "A" * 15,
        "AK" + "IA" + "A" * 17,
        "XAK" + "IA" + "A" * 16,
        "gh" + "p_" + "a" * 35,
        "gh" + "p_" + "a" * 37,
        "github" + "_pat_" + "a" * 21,
        "s" + "k-ant-" + "a" * 19,
        "s" + "k-" + "a" * 30,
        "s" + "k-" + "a1" * 9,
        "ta" + "sk-" + "queue1-worker-for-billing",
        "mask-" + "abc123" * 5,
        "xo" + "xb-" + "a" * 9,
        "-----BEGIN " + "PUBLIC KEY-----",
        'password = "changeme"',
        'password = "short"',
        'api_key = "your-api-key-goes-here"',
        'token = "place' + 'holder-12345678"',
        'token = "xxxxxxxxxxxxxxxxxxxx"',
        'token = "<insert-token-here-1>"',
        'token = "${GITHUB_TOKEN_VALUE}"',
        'token = "replace-with-a-token-1"',
        'api_key = os.environ["KEY"]',
        "token = get_token()",
        'TOKEN_URL = "https://auth.acme.com/oauth"',
        'token_url = "http://auth.acme.com/oauth"',
        'tokenizer_name = "bert-base-uncased-v2"',
        f'password_hash = "{CREDENTIAL}"',
        f'mytoken = "{CREDENTIAL}"',
        'secret = "a sentence with spaces in it"',
        f"if token == '{CREDENTIAL}':",
        'def f(token: "AccessTokenProvider")',
        'def f(token: "Token2ProviderClass")',
        'token: "AccessTokenProvider"',
        'token: "Optional[AccessToken]"',
        'token: "auth.AccessTokenClass"',
        'token: "Token|AccessTokenClass"',
        f'f(token: int, b = "{CREDENTIAL}")',
        f"secret = \"{CREDENTIAL}'",
        f'password = "abc"{CREDENTIAL}"',
        f'"api_key\': "{CREDENTIAL}"',
        "x--" + AWS_KEY,
        "+x " + "-" + SLACK_TOKEN,
    ],
)
def test_scan_secrets_ignores_a_near_miss(line: str) -> None:
    assert scan_secrets(f"+{line}\n") == ()


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f"-{AWS_KEY}", SecretKind.AWS_ACCESS_KEY),
        (f"--{AWS_KEY}", SecretKind.AWS_ACCESS_KEY),
        (f" -{AWS_KEY}", SecretKind.AWS_ACCESS_KEY),
        (f"+-{AWS_KEY}", SecretKind.AWS_ACCESS_KEY),
        (f"--{GITHUB_TOKEN}", SecretKind.GITHUB_TOKEN),
        (f"+ -{SLACK_TOKEN}", SecretKind.SLACK_TOKEN),
        (f'++password: "{CREDENTIAL}"', SecretKind.ASSIGNED_CREDENTIAL),
        (f'-+  - password: "{CREDENTIAL}"', SecretKind.ASSIGNED_CREDENTIAL),
        (f'--password = "{CREDENTIAL}"', SecretKind.ASSIGNED_CREDENTIAL),
    ],
    ids=[
        "removed",
        "combined-removed",
        "combined-context-removed",
        "added-dash",
        "combined-github",
        "spaced-markers",
        "combined-yaml",
        "combined-yaml-list",
        "combined-assign",
    ],
)
def test_scan_secrets_reads_a_leading_marker_run_as_markers(line: str, kind: SecretKind) -> None:
    assert kinds(f"{line}\n") == [kind]


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        (f'token = "{GITHUB_TOKEN}"', SecretKind.GITHUB_TOKEN),
        (f'openai.api_key = "{OPENAI_KEY}"', SecretKind.OPENAI_KEY),
    ],
    ids=["github", "openai"],
)
def test_scan_secrets_reports_a_vendor_key_in_a_credential_once_under_its_own_kind(
    line: str, kind: SecretKind
) -> None:
    assert kinds(f"+{line}\n") == [kind]


@pytest.mark.parametrize(
    "line",
    [f'password = "-{AWS_KEY}"', f'"token": "--{GITHUB_TOKEN}"'],
    ids=["dash-aws", "dashes-github"],
)
def test_scan_secrets_reports_a_credential_whose_value_only_resembles_a_vendor_key(
    line: str,
) -> None:
    assert kinds(f"+{line}\n") == [SecretKind.ASSIGNED_CREDENTIAL]


def test_scan_secrets_reports_a_key_nested_in_a_private_key_line_under_both_kinds() -> None:
    line = f"+-----BEGIN {AWS_KEY} PRIVATE KEY-----\n"

    assert kinds(line) == [SecretKind.PRIVATE_KEY, SecretKind.AWS_ACCESS_KEY]


# Vendor spans can nest (a key inside a private key's BEGIN line) or overlap (a Slack token
# running into one). These spans are built directly, to reach shapes no line produces today.
OUTER = Span(0, 48, SecretKind.PRIVATE_KEY)
NESTED = Span(11, 31, SecretKind.AWS_ACCESS_KEY)
SLACK = Span(0, 25, SecretKind.SLACK_TOKEN)
OVERLAPPING = Span(15, 42, SecretKind.PRIVATE_KEY)


def credential(start: int, end: int) -> Span:
    return Span(start, end, SecretKind.ASSIGNED_CREDENTIAL)


@pytest.mark.parametrize(
    ("others", "span"),
    [
        ([OUTER, NESTED], credential(35, 60)),
        ([NESTED, OUTER], credential(35, 60)),
        ([OUTER, NESTED], credential(40, 48)),
        ([SLACK, OVERLAPPING], credential(30, 50)),
        ([SLACK, OVERLAPPING], credential(10, 20)),
    ],
    ids=["past-nested", "unsorted", "inside-outer-only", "second-overlapping", "both"],
)
def test_spans_clear_of_drops_a_span_overlapping_a_nested_or_overlapping_span(
    others: list[Span], span: Span
) -> None:
    assert spans_clear_of([span], others) == []


@pytest.mark.parametrize(
    ("others", "span"),
    [
        ([OUTER, NESTED], credential(48, 60)),
        ([SLACK, OVERLAPPING], credential(42, 60)),
        ([Span(10, 20, SecretKind.SLACK_TOKEN)], credential(0, 10)),
        ([], credential(0, 10)),
    ],
    ids=["after-outer", "after-overlapping", "just-before", "no-others"],
)
def test_spans_clear_of_keeps_a_span_that_only_touches_or_misses_them(
    others: list[Span], span: Span
) -> None:
    assert spans_clear_of([span], others) == [span]


def test_scan_secrets_reports_an_anthropic_key_only_as_anthropic() -> None:
    assert kinds(f"+{ANTHROPIC_KEY}\n") == [SecretKind.ANTHROPIC_KEY]


def test_scan_secrets_numbers_lines_from_one_by_newline_only() -> None:
    text = f"first\r\nsecond{LINE_SEPARATOR}still second {AWS_KEY}\n\n{SLACK_TOKEN} {GITHUB_TOKEN}"

    assert scan_secrets(text) == (
        SecretHit(SecretKind.AWS_ACCESS_KEY, 2),
        SecretHit(SecretKind.SLACK_TOKEN, 4),
        SecretHit(SecretKind.GITHUB_TOKEN, 4),
    )


def test_scan_secrets_finds_a_secret_anywhere_in_the_text() -> None:
    text = f"Subject: rotate {AWS_KEY}\n+++ b/{GITHUB_TOKEN}.py\n-{SLACK_TOKEN}\n"

    assert kinds(text) == [
        SecretKind.AWS_ACCESS_KEY,
        SecretKind.GITHUB_TOKEN,
        SecretKind.SLACK_TOKEN,
    ]


def test_scan_secrets_hit_holds_no_value() -> None:
    [hit] = scan_secrets(AWS_KEY)

    assert AWS_KEY not in repr(hit)


@pytest.mark.parametrize(
    "line",
    [
        "token: " * 57_000,
        '"token": ' * 44_000,
        "a_" * 200_000,
        "-" * 400_000,
        "s" + "k-" + "a" * 399_990,
        "-----BEGIN " + "A " * 199_990,
        'password: "' + "a" * 399_980,
        "x" + "-----END " * 44_000,
        "+- " * 133_000 + "x",
        "-" * 399_990 + "password",
        " " * 399_990 + "AK" + "IA",
        "\t-" * 199_990 + "token:",
        'password = "' + "'" * 399_980,
        'password: "' + "'" * 399_980,
        '"token": "' + "'a" * 199_990,
        '"token":\'' * 40_000,
        f'token="{AWS_KEY}" ' * 13_700,
        f'token="{CREDENTIAL}" {AWS_KEY} ' * 8_600,
    ],
    ids=[
        "names",
        "json-names",
        "underscores",
        "dashes",
        "key-prefix",
        "pem",
        "yaml",
        "pem-ends",
        "marker-run",
        "marker-run-then-name",
        "space-run-then-prefix",
        "yaml-indent-run",
        "other-quotes",
        "yaml-other-quotes",
        "json-other-quotes",
        "json-names-other-quote",
        "vendor-keys-in-credentials",
        "credentials-beside-vendor-keys",
    ],
)
def test_scan_and_redact_a_long_hostile_line_in_linear_time(line: str) -> None:
    started = time.perf_counter()

    scan_secrets(line)
    redact(line)

    assert time.perf_counter() - started < 3.0


def test_redact_replaces_each_value_and_keeps_the_rest() -> None:
    text = f'a {AWS_KEY} b\npassword = "{CREDENTIAL}"\r\n{SLACK_TOKEN}'

    assert redact(text) == 'a [REDACTED] b\npassword = "[REDACTED]"\r\n[REDACTED]'


def test_redact_replaces_a_whole_private_key_block() -> None:
    text = f"before\n{PEM_BEGIN}\nMIIEpAIB\nAKCAQEA\n{PEM_END}\nafter\n"

    assert redact(text) == "before\n[REDACTED]\nafter\n"


def test_redact_replaces_a_private_key_block_with_no_end_to_the_end_of_the_text() -> None:
    assert redact(f"before\n{PEM_BEGIN}\nMIIEpAIB\n") == "before\n[REDACTED]"


def test_redact_replaces_a_private_key_end_with_no_begin_from_the_start_of_the_text() -> None:
    assert redact(f"MIIEpAIB\n{PEM_END}\nafter") == "[REDACTED]\nafter"


def test_redact_replaces_a_stray_end_after_a_block_from_where_that_block_ended() -> None:
    text = f"{PEM_BEGIN}\nMII\n{PEM_END}\nMII\n{PEM_END}\nafter"

    assert redact(text) == "[REDACTED][REDACTED]\nafter"


def test_redact_replaces_a_whole_pgp_private_key_block() -> None:
    text = f"before\n{PGP_BEGIN}\n\nlQOYBF\n=a1B2\n{PGP_END}\nafter"

    assert redact(text) == "before\n[REDACTED]\nafter"


def test_redact_keeps_the_other_quote_inside_a_value_redacted() -> None:
    assert redact(f"token = '{CREDENTIAL}\"x' # kept") == "token = '[REDACTED]' # kept"


def test_redact_treats_a_second_begin_inside_a_block_as_part_of_it() -> None:
    text = f"{PEM_BEGIN}\n{PEM_BEGIN}\nMII\n{PEM_END}\nafter"

    assert redact(text) == "[REDACTED]\nafter"


def test_redact_with_no_secret_returns_the_input_unchanged() -> None:
    text = f'token = get_token()\r\npassword = "short"\n{LINE_SEPARATOR}'

    assert redact(text) == text


def test_secret_line_spans_cover_each_hit_line() -> None:
    text = f"a\nkey = {AWS_KEY}\nb\ntoken {GITHUB_TOKEN}\n"

    assert secret_line_spans(text) == (SecretSpan(2, 2), SecretSpan(4, 4))


def test_secret_line_spans_cover_a_private_key_block_from_begin_to_end() -> None:
    text = "a\n" + PEM_BEGIN + "\nbody\nbody\n" + PEM_END + "\nb\n"

    assert secret_line_spans(text) == (SecretSpan(2, 5),)


def test_secret_line_spans_run_an_unclosed_block_to_the_end() -> None:
    text = "a\n" + PEM_BEGIN + "\nbody\nbody"

    assert secret_line_spans(text) == (SecretSpan(2, 4),)


def test_secret_line_spans_run_an_end_without_begin_from_the_previous_block() -> None:
    text = "a\n" + PEM_BEGIN + "\n" + PEM_END + "\nb\nc\n" + PGP_END + "\n"

    assert secret_line_spans(text) == (SecretSpan(2, 6),)


def test_secret_line_spans_run_an_end_without_begin_from_line_1() -> None:
    assert secret_line_spans("a\nb\n" + PEM_END) == (SecretSpan(1, 3),)


def test_secret_line_spans_merge_spans_that_overlap() -> None:
    text = PEM_BEGIN + f"\nkey = {AWS_KEY}\n" + PEM_END + f"\nkey = {AWS_KEY}\nok\n"

    assert secret_line_spans(text) == (SecretSpan(1, 3), SecretSpan(4, 4))


def test_secret_line_spans_of_text_without_secrets_are_empty() -> None:
    assert secret_line_spans("nothing\nhere\n") == ()


@pytest.mark.parametrize(
    "path",
    [f"{AWS_KEY}/notes.txt", f"cfg_{AWS_KEY}.txt", f"a/b-{AWS_KEY}", f"x.{AWS_KEY}.json"],
)
def test_path_holds_secret_finds_a_key_anywhere_in_a_path(path: str) -> None:
    assert path_holds_secret(path)


PATH_KEYS = {
    "aws": AWS_KEY,
    "github": GITHUB_TOKEN,
    "github_pat": GITHUB_PAT,
    "anthropic": ANTHROPIC_KEY,
    "openai": OPENAI_KEY,
    "slack": SLACK_TOKEN,
}


@pytest.mark.parametrize("form", ["cfg_{}.txt", "cfg-{}.txt", "d/{}"])
@pytest.mark.parametrize("key", PATH_KEYS.values(), ids=PATH_KEYS.keys())
def test_path_holds_secret_finds_every_key_format_a_name_embeds(key: str, form: str) -> None:
    assert path_holds_secret(form.format(key))


def test_path_holds_secret_finds_an_assigned_credential() -> None:
    assert path_holds_secret(f'notes/password="{CREDENTIAL}"')


@pytest.mark.parametrize(
    "path",
    [
        "billing/invoice.py",
        "akia/notes.txt",
        "task-runner-2024-abcdefghijkl.py",
        "id_rsa.pub",
        "cfg-sk-" + "a" * 30 + ".txt",  # an OpenAI-like run with no digit
    ],
)
def test_path_holds_secret_leaves_ordinary_paths_alone(path: str) -> None:
    assert not path_holds_secret(path)


@pytest.mark.parametrize(
    "path", ["a_" * 2000, "sk-" * 1333, "ghp_" * 1000, "github_pat" * 400, "-" * 4000]
)
def test_path_holds_secret_on_a_4000_character_path_is_quick(path: str) -> None:
    started = time.perf_counter()
    path_holds_secret(path)

    assert time.perf_counter() - started < 0.05
