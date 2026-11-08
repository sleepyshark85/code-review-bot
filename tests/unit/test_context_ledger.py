from review_bot.context.ledger import LineSpan, merged_spans_by_file


def test_merged_spans_by_file_groups_by_sorted_path() -> None:
    spans = [LineSpan("b.py", 1, 1), LineSpan("a.py", 4, 5)]

    assert merged_spans_by_file(spans) == {
        "a.py": [LineSpan("a.py", 4, 5)],
        "b.py": [LineSpan("b.py", 1, 1)],
    }


def test_merged_spans_by_file_merges_overlapping_and_touching_spans_in_any_order() -> None:
    spans = [LineSpan("a.py", 10, 12), LineSpan("a.py", 1, 3), LineSpan("a.py", 4, 4)]
    spans += [LineSpan("a.py", 11, 20), LineSpan("a.py", 13, 14)]

    assert merged_spans_by_file(spans) == {
        "a.py": [LineSpan("a.py", 1, 4), LineSpan("a.py", 10, 20)],
    }


def test_merged_spans_by_file_keeps_spans_with_a_gap_apart() -> None:
    spans = [LineSpan("a.py", 1, 1), LineSpan("a.py", 3, 3)]

    assert merged_spans_by_file(spans) == {"a.py": spans}


def test_merged_spans_by_file_of_nothing_is_empty() -> None:
    assert merged_spans_by_file([]) == {}
