from app.ai.providers import _plate_candidates


def test_plate_candidates_are_normalized_and_require_mixed_alphanumeric_text():
    assert _plate_candidates("KA 03 MN 1234\njust words\n2025") == ["KA03MN1234"]
