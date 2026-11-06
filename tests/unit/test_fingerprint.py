import re

import pytest

from review_bot.fingerprint import Fingerprinter, fingerprint_for

BASE = {"file": "app.py", "rule": "swallowed-exception", "anchor_text": "except E:", "ordinal": 0}


def fingerprint(**overrides: object) -> str:
    fields = BASE | overrides
    return fingerprint_for(
        file=str(fields["file"]),
        rule=str(fields["rule"]),
        anchor_text=str(fields["anchor_text"]),
        ordinal=int(str(fields["ordinal"])),
    )


def test_fingerprint_for_a_fixed_input_is_pinned() -> None:
    # The sample diff's swallowed exception. Changing this value orphans every stored identity.
    assert (
        fingerprint_for(
            file="billing/invoice.py",
            rule="swallowed-exception",
            anchor_text="        except Exception:",
            ordinal=0,
        )
        == "7b7508acb73b5e4b"
    )


def test_fingerprint_for_is_16_lowercase_hex_digits() -> None:
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint())


@pytest.mark.parametrize("anchor_text", ["  except E:", "except E:\t", "\texcept   E:  \r"])
def test_fingerprint_for_ignores_whitespace_runs_and_ends(anchor_text: str) -> None:
    assert fingerprint(anchor_text=anchor_text) == fingerprint()


@pytest.mark.parametrize(
    "override",
    [
        {"file": "other.py"},
        {"rule": "ai:reliability"},
        {"anchor_text": "except F:"},
        {"anchor_text": "exceptE:"},
        {"ordinal": 1},
    ],
)
def test_fingerprint_for_changes_with_each_part(override: dict[str, object]) -> None:
    assert fingerprint(**override) != fingerprint()


def test_fingerprint_for_keeps_parts_apart_whatever_they_hold() -> None:
    assert fingerprint(file="a", rule="b,c") != fingerprint(file="a,b", rule="c")


def test_fingerprint_for_hashes_a_path_with_a_lone_surrogate() -> None:
    assert re.fullmatch(r"[0-9a-f]{16}", fingerprint(file="caf\udcff.py", anchor_text="\udcff"))


def test_fingerprint_for_rejects_a_negative_ordinal() -> None:
    with pytest.raises(ValueError, match="can't be negative"):
        fingerprint(ordinal=-1)


def test_fingerprinter_numbers_findings_sharing_every_other_part_in_order() -> None:
    fingerprinter = Fingerprinter()

    first = fingerprinter.fingerprint(file="app.py", rule="r", anchor_text="pass")
    other = fingerprinter.fingerprint(file="app.py", rule="r", anchor_text="raise")
    second = fingerprinter.fingerprint(file="app.py", rule="r", anchor_text="  pass")

    assert [first, other, second] == [
        fingerprint_for(file="app.py", rule="r", anchor_text="pass", ordinal=0),
        fingerprint_for(file="app.py", rule="r", anchor_text="raise", ordinal=0),
        fingerprint_for(file="app.py", rule="r", anchor_text="pass", ordinal=1),
    ]
