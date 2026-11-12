import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest
from audit_records import data_of
from diff_samples import FINDING_DIFF
from git_samples import commit, init_repo

from review_bot.cli import main, read_policy, read_policy_option, settings_within_policy
from review_bot.context.ledger import ToolName
from review_bot.errors import ConfigError
from review_bot.governance.policy import BUILTIN_POLICY, MAX_POLICY_BYTES, PolicyError
from review_bot.providers.mock import MockProvider
from review_bot.settings import Settings, load_settings

ORG_POLICY = (
    b'version = "org-1"\n[tools]\nallowed = ["read", "glob"]\n[cost]\nmax_cost_usd = "0.60"\n'
)
NO_TOOLS = b'version = "locked"\n[tools]\nallowed = []\n'


@pytest.fixture
def diff_path(tmp_path: Path) -> Path:
    path = tmp_path / "change.diff"
    path.write_text(FINDING_DIFF, encoding="utf-8")
    return path


def policy_file(tmp_path: Path, data: bytes, name: str = "p.toml") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_read_policy_without_a_path_is_the_builtin_policy() -> None:
    assert read_policy(None) is BUILTIN_POLICY


def test_read_policy_option_takes_the_flag_then_the_variable_then_the_builtin_policy(
    tmp_path: Path,
) -> None:
    flagged = str(policy_file(tmp_path, ORG_POLICY))
    variable = {"REVIEW_BOT_POLICY": str(policy_file(tmp_path, NO_TOOLS, name="v.toml"))}

    assert read_policy_option(flagged, variable).version == "org-1"
    assert read_policy_option(None, variable).version == "locked"
    assert read_policy_option(None, {}) is BUILTIN_POLICY


def test_read_policy_option_refuses_an_empty_flag_as_load_settings_does() -> None:
    with pytest.raises(ConfigError) as raised:
        read_policy_option("", {})
    with pytest.raises(ConfigError) as from_settings:
        load_settings({"policy": ""})

    assert str(raised.value) == str(from_settings.value)
    assert "--policy" in str(raised.value)


def test_read_policy_option_refuses_an_empty_variable_as_load_settings_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVIEW_BOT_POLICY", "")

    with pytest.raises(ConfigError) as raised:
        read_policy_option(None, {"REVIEW_BOT_POLICY": ""})
    with pytest.raises(ConfigError) as from_settings:
        load_settings({})

    assert str(raised.value) == str(from_settings.value)
    assert "REVIEW_BOT_POLICY" in str(raised.value)


def test_read_policy_records_the_path_as_given_and_the_files_hash(tmp_path: Path) -> None:
    path = policy_file(tmp_path, ORG_POLICY)

    policy = read_policy(str(path))

    assert (policy.version, policy.source) == ("org-1", str(path))
    assert policy.sha256 == hashlib.sha256(ORG_POLICY).hexdigest()
    assert policy.tools == frozenset({ToolName.READ, ToolName.GLOB})


def test_read_policy_of_a_missing_file_names_it(tmp_path: Path) -> None:
    missing = tmp_path / "absent.toml"

    with pytest.raises(PolicyError, match=r"absent\.toml can't be used: it doesn't exist"):
        read_policy(str(missing))


def test_read_policy_of_a_directory_says_it_cant_be_read(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="can't be used: it can't be read: "):
        read_policy(str(tmp_path))


def test_read_policy_reports_a_file_over_the_limit(tmp_path: Path) -> None:
    path = policy_file(tmp_path, b'version = "v"\n' + b"#" * (2 * MAX_POLICY_BYTES))

    with pytest.raises(PolicyError, match="over the limit"):
        read_policy(str(path))


def test_settings_within_policy_applies_the_ceiling(tmp_path: Path) -> None:
    path = policy_file(tmp_path, ORG_POLICY)

    settings, policy = settings_within_policy(Settings(policy=str(path)))

    assert (settings.max_cost_usd, policy.version) == (Decimal("0.60"), "org-1")


def test_main_records_the_policy_in_the_report_and_the_audit_log(
    diff_path: Path,
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = policy_file(tmp_path, ORG_POLICY)

    main(
        [str(diff_path), "--provider", "mock", "--format", "json", "--policy", str(path)],
        provider=MockProvider(findings=[]),
    )

    report = json.loads(capsys.readouterr().out)
    sha256 = hashlib.sha256(ORG_POLICY).hexdigest()
    assert report["policy"] == {"version": "org-1", "sha256": sha256, "source": str(path)}
    assert list(report)[-1] == "policy"
    start = data_of(audit_log_in_tmp_path, "run.start")
    assert (start["policy_version"], start["policy_sha256"]) == ("org-1", sha256)


def test_main_without_a_policy_records_the_builtin_one(
    diff_path: Path, audit_log_in_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main([str(diff_path), "--format", "json"], provider=MockProvider(findings=[]))

    report = json.loads(capsys.readouterr().out)
    assert report["policy"] == {
        "version": "builtin-v1",
        "sha256": BUILTIN_POLICY.sha256,
        "source": "builtin",
    }


def test_main_takes_the_policy_flag_over_the_variable(
    diff_path: Path,
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("REVIEW_BOT_POLICY", str(tmp_path / "absent.toml"))
    path = policy_file(tmp_path, ORG_POLICY)

    main(
        [str(diff_path), "--format", "json", "--policy", str(path)],
        provider=MockProvider(findings=[]),
    )

    assert json.loads(capsys.readouterr().out)["policy"]["version"] == "org-1"


def test_main_with_an_invalid_policy_exits_2_before_the_audit_log(
    diff_path: Path,
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = policy_file(tmp_path, b'version = "v"\nfoo = 1\n')
    provider = MockProvider(findings=[])

    assert main([str(diff_path), "--policy", str(path)], provider=provider) == 2

    err = capsys.readouterr().err
    assert "p.toml is invalid: unknown key 'foo'" in err
    assert provider.requests == []
    assert not audit_log_in_tmp_path.exists()


def test_main_with_a_cap_above_the_ceiling_exits_2_before_any_call(
    diff_path: Path,
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = policy_file(tmp_path, ORG_POLICY)
    provider = MockProvider(findings=[])

    exit_code = main(
        [str(diff_path), "--policy", str(path), "--max-cost-usd", "1.00"], provider=provider
    )

    assert exit_code == 2
    assert provider.requests == []
    assert "1.00" in capsys.readouterr().err


def test_main_in_context_mode_under_a_policy_with_no_tools_exits_2(
    diff_path: Path,
    tmp_path: Path,
    audit_log_in_tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = init_repo(tmp_path / "repo")
    commit(repo, {"a.py": "x\n"})
    path = policy_file(tmp_path, NO_TOOLS)
    provider = MockProvider(findings=[])

    argv = [str(diff_path), "--policy", str(path), "--context-repo", str(repo)]
    assert main(argv, provider=provider) == 2

    assert "The policy `locked` allows no tools" in capsys.readouterr().err
    assert provider.requests == []


def test_main_in_context_mode_offers_only_the_allowed_tools_and_refuses_the_rest(
    diff_path: Path, tmp_path: Path, audit_log_in_tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "repo")
    commit(repo, {"a.py": "x\n"})
    path = policy_file(tmp_path, ORG_POLICY)
    provider = MockProvider(findings=[], tool_calls=[("grep", {"literal": "x"})])

    main(
        [str(diff_path), "--provider", "mock", "--policy", str(path), "--context-repo", str(repo)],
        provider=provider,
    )

    [request] = provider.requests
    assert request.tools is not None
    assert [tool.name for tool in request.tools.definitions] == ["read", "glob"]
    call = data_of(audit_log_in_tmp_path, "context.tool_call")
    assert (call["tool"], call["outcome"], call["reason"]) == ("grep", "refused", "policy_denied")
