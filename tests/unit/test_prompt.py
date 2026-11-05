from review_bot.prompt import build_request


def test_build_request_passes_the_model_through() -> None:
    assert build_request("diff", model="some-model").model == "some-model"


def test_build_request_user_prompt_is_delimiters_around_unchanged_diff() -> None:
    diff = "+{self.id}\r\n@@ -1 +1,99 @@\n"

    lines = build_request(diff, model="m").user_prompt.split("\n")

    opening, closing = lines[0], lines[-1]
    assert "\n".join(lines[1:-1]) == diff
    assert opening != closing


def test_system_prompt_anchors_line_numbers_to_the_new_version() -> None:
    system_prompt = build_request("d", model="m").system_prompt

    assert "the line number in the new version of the file" in system_prompt
    assert "for a removed line, the nearest line in the new version" in system_prompt
