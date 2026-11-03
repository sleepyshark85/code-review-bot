from review_bot.__main__ import main


def test_main_before_phase_0_exits_with_error_code() -> None:
    assert main() == 2
