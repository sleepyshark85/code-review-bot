import pytest

from review_bot.context.paths import (
    is_denied,
    is_valid_glob,
    is_valid_path,
    shown_path,
)


@pytest.mark.parametrize(
    "path",
    [
        "billing/invoice.py",
        "a",
        "a" * 1000,
        "docs/café.md",
        "a b/c-d_e.f",
        "ab:c",
        "x:notes.md",
        "c:x",
        "a/b:c",
        ".github/x",
    ],
)
def test_is_valid_path_accepts_a_relative_slash_separated_path(path: str) -> None:
    assert is_valid_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/etc/passwd",
        "C:/x",
        "C:",
        "c:/x",
        "billing\\invoice.py",
        "a//b",
        "./a",
        "a/./b",
        "a/../b",
        "a/",
        "a/..",
        "",
        "a\x00b",
        "a\nb",
        "a\x1bb",
        "a\x7fb",
        "a\x85b",
        "a\udcffb",
        "a" * 1001,
        ":(top)a",
        ":!x",
    ],
)
def test_is_valid_path_refuses_a_path_outside_the_rules(path: str) -> None:
    assert not is_valid_path(path)


@pytest.mark.parametrize("pattern", ["**", "billing/*.py", "**/*.py", "*.md", "a..b/*"])
def test_is_valid_glob_accepts_a_glob_inside_the_commit(pattern: str) -> None:
    assert is_valid_glob(pattern)


@pytest.mark.parametrize(
    "pattern", ["../*", "a/../*", "**/..", "/etc/*", ":(top)*", ":!x", "billing\\*.py"]
)
def test_is_valid_glob_refuses_an_escaping_or_magic_glob(pattern: str) -> None:
    assert not is_valid_glob(pattern)


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        "sub/.git/HEAD",
        ".GIT/config",
        ".git",
        ".env",
        "config/.env.production",
        "deploy/server.PEM",
        "tls.key",
        "cert.p12",
        "cert.pfx",
        "store.jks",
        "app.keystore",
        "putty.ppk",
        "vault.kdbx",
        "infra/terraform.tfstate",
        "prod.tfvars",
        "id_rsa",
        "home/.ssh/ID_DSA",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa.old",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        ".htpasswd",
        ".pgpass",
        "credentials",
        "aws/credentials.json",
    ],
)
def test_is_denied_matches_git_internals_and_secrets_files(path: str) -> None:
    assert is_denied(path)


@pytest.mark.parametrize(
    "path",
    [
        "id_ed25519.pub",
        "id_rsa.PUB",
        "id_rsa_parser.py",
        ".envrc",
        "environment.py",
        "credentials_test.py",
        "pem/readme.md",
        ".gitignore",
        ".github/workflows/ci.yml",
        "key.py",
    ],
)
def test_is_denied_leaves_ordinary_files_alone(path: str) -> None:
    assert not is_denied(path)


@pytest.mark.parametrize("path", ["billing/invoice.py", "docs/café.md", "a b"])
def test_shown_path_shows_a_plain_path_as_it_is(path: str) -> None:
    assert shown_path(path) == path


@pytest.mark.parametrize(
    ("path", "shown"),
    [
        ("a\nb", '"a\\nb"'),
        ("a\x1bb", '"a\\033b"'),
        ("caf\udcff.md", '"caf\\377.md"'),
        ("café\n", '"caf\\303\\251\\n"'),
        ('"quoted"', '"\\"quoted\\""'),
        ("a\\b", '"a\\\\b"'),
    ],
)
def test_shown_path_c_quotes_a_path_with_control_characters_quotes_or_bad_bytes(
    path: str, shown: str
) -> None:
    assert shown_path(path) == shown
