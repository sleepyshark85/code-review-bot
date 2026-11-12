import time

import pytest

from review_bot.governance.redaction import (
    Masked,
    RedactionKind,
    add_counts,
    mask_diff,
    mask_personal_data,
)

EMAIL = frozenset({RedactionKind.EMAIL})
PHONE = frozenset({RedactionKind.PHONE})
IPV4 = frozenset({RedactionKind.IPV4})
ALL = frozenset(RedactionKind)


def test_an_email_address_is_masked_in_place() -> None:
    masked = mask_personal_data('contact = "jane.doe@rightship.com"', EMAIL)

    assert masked == Masked('contact = "[REDACTED:email]"', {RedactionKind.EMAIL: 1})


@pytest.mark.parametrize(
    "text",
    [
        "user@example.com",
        "ops@mail.example.org",
        "dev@example.net",
        "a@b.test",
        "x@y.invalid",
        "q@host.example",
        "USER@EXAMPLE.COM",
    ],
)
def test_an_email_in_a_documentation_domain_is_left_alone(text: str) -> None:
    assert mask_personal_data(text, EMAIL) == Masked(text)


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("Mail jane@rightship.com.", "Mail [REDACTED:email]."),
        ("<a.b+tag@mail.rightship.com.au>", "<[REDACTED:email]>"),
        ("jane@notexample.com", "[REDACTED:email]"),
        ("jane@example.com.evil.org", "[REDACTED:email]"),
    ],
)
def test_an_email_ends_at_its_domain(text: str, masked: str) -> None:
    assert mask_personal_data(text, EMAIL).text == masked


@pytest.mark.parametrize("text", ["lodash@4.17.21", "@decorator", "a@b", "a@b.c", "x@y.com1"])
def test_text_that_isnt_an_email_is_left_alone(text: str) -> None:
    assert mask_personal_data(text, EMAIL) == Masked(text)


@pytest.mark.parametrize("phone", ["+61 2 9999 0000", "+12025550143", "+1-202-555-0143"])
def test_an_e164_phone_number_is_masked(phone: str) -> None:
    assert mask_personal_data(f"call {phone} now", PHONE) == Masked(
        "call [REDACTED:phone] now", {RedactionKind.PHONE: 1}
    )


@pytest.mark.parametrize(
    "text",
    [
        "12025550143",
        "+123",
        "+1234567",
        "+" + "1" * 16,
        "+1234 5678 9012 3456",
        "a+12025550143",
        "+12025550143x",
        "+1  202 555 0143",
    ],
    ids=[
        "no-plus",
        "too-short",
        "seven-digits",
        "sixteen-digits",
        "sixteen-in-groups",
        "after-a-letter",
        "before-a-letter",
        "double-space",
    ],
)
def test_text_that_isnt_an_e164_number_is_left_alone(text: str) -> None:
    assert mask_personal_data(text, PHONE).counts == {}


def test_a_phone_number_of_exactly_8_and_15_digits_is_masked() -> None:
    masked = mask_personal_data("+12345678 +123456789012345", PHONE)

    assert masked == Masked("[REDACTED:phone] [REDACTED:phone]", {RedactionKind.PHONE: 2})


@pytest.mark.parametrize("address", ["10.0.0.12", "8.8.8.8", "255.255.255.255", "192.0.3.1"])
def test_an_ipv4_address_is_masked(address: str) -> None:
    assert mask_personal_data(f"host = '{address}'", IPV4) == Masked(
        "host = '[REDACTED:ipv4]'", {RedactionKind.IPV4: 1}
    )


@pytest.mark.parametrize(
    "text",
    [
        "192.0.2.5",
        "198.51.100.7",
        "203.0.113.9",
        "999.1.1.1",
        "1.2.3.256",
        "1.2.3.4.5",
        "v1.2.3",
        "v1.2.3.4",
        "1234.1.1.1",
    ],
)
def test_text_that_isnt_a_personal_ipv4_address_is_left_alone(text: str) -> None:
    assert mask_personal_data(text, IPV4) == Masked(text)


def test_an_ipv4_address_at_the_end_of_a_sentence_is_masked() -> None:
    assert mask_personal_data("It runs on 10.0.0.12.", IPV4).text == "It runs on [REDACTED:ipv4]."


def test_only_the_kinds_asked_for_are_masked() -> None:
    text = "jane@rightship.com +12025550143 10.0.0.1"

    assert mask_personal_data(text, PHONE).text == "jane@rightship.com [REDACTED:phone] 10.0.0.1"
    assert mask_personal_data(text, frozenset()) == Masked(text)


def test_every_kind_is_counted_once_per_value() -> None:
    text = "a@rightship.com\nb@rightship.com +12025550143\nuser@10.0.0.1"

    masked = mask_personal_data(text, ALL)

    assert masked.text == (
        "[REDACTED:email]\n[REDACTED:email] [REDACTED:phone]\nuser@[REDACTED:ipv4]"
    )
    assert masked.counts == {
        RedactionKind.EMAIL: 2,
        RedactionKind.PHONE: 1,
        RedactionKind.IPV4: 1,
    }


def test_masked_text_masks_to_itself() -> None:
    once = mask_personal_data("jane@rightship.com +12025550143 10.0.0.1", ALL).text

    assert mask_personal_data(once, ALL) == Masked(once)


def test_a_match_never_crosses_a_line() -> None:
    text = "jane@\nrightship.com\n+1202\n5550143\n10.0.\n0.1"

    assert mask_personal_data(text, ALL) == Masked(text)


def test_mask_diff_keeps_each_lines_marker_and_its_number() -> None:
    diff = (
        "@@ -1,2 +1,3 @@\n"
        "-old = 'jane@rightship.com'\n"
        "+12025550143\n"
        "++61 2 9999 0000\n"
        " host = 10.0.0.12\n"
    )

    masked = mask_diff(diff, ALL)

    assert masked.text == (
        "@@ -1,2 +1,3 @@\n"
        "-old = '[REDACTED:email]'\n"
        "+12025550143\n"
        "+[REDACTED:phone]\n"
        " host = [REDACTED:ipv4]\n"
    )
    assert masked.counts == {
        RedactionKind.EMAIL: 1,
        RedactionKind.PHONE: 1,
        RedactionKind.IPV4: 1,
    }


def test_mask_diff_with_no_kinds_returns_the_diff_as_it_is() -> None:
    diff = "+jane@rightship.com\n"

    assert mask_diff(diff, frozenset()) == Masked(diff)


def test_add_counts_sums_by_kind_and_leaves_out_kinds_with_none() -> None:
    total = add_counts(
        [{RedactionKind.EMAIL: 1}, {}, {RedactionKind.EMAIL: 2, RedactionKind.IPV4: 0}]
    )

    assert total == {RedactionKind.EMAIL: 3}


@pytest.mark.parametrize(
    "line",
    [
        "@" * 400_000,
        "+" * 400_000,
        "." * 400_000,
        "a@" * 200_000,
        "a." * 199_999 + "@",
        "x@" + "a." * 199_999,
        "+1 " * 133_333,
        "1." * 200_000,
        "a+" * 200_000,
        "-" * 400_000,
    ],
    ids=[
        "at",
        "plus",
        "dot",
        "a-at",
        "local-run",
        "domain-run",
        "phone-groups",
        "dotted",
        "a-plus",
        "dash",
    ],
)
def test_a_long_hostile_line_is_masked_in_linear_time(line: str) -> None:
    started = time.perf_counter()

    mask_personal_data(line, ALL)
    mask_diff(line, ALL)

    assert time.perf_counter() - started < 3.0
