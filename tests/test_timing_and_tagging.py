import pytest

from race_engineer.tagging import tag
from race_engineer.timing import format_gap, format_time, parse_time


@pytest.mark.parametrize("text, ms", [
    ("1:42.31", 102_310),
    ("1:42.310", 102_310),
    ("1:42", 102_000),
    ("102.31", 102_310),
    ("59.9", 59_900),
])
def test_parse_time(text, ms):
    assert parse_time(text) == ms


@pytest.mark.parametrize("text", ["1:72.0", "abc", "1:42.3105", ""])
def test_parse_time_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_time(text)


def test_format_time_round_trips():
    assert format_time(102_310) == "1:42.310"
    assert format_time(59_900) == "59.900"
    assert format_gap(-250) == "-0.250"


def test_tag_front_mid_corner_with_corners():
    tags = tag("Front feels slippery mid corner T3, T7 and turn 3")
    assert tags.end == "front"
    assert tags.phase == "mid"
    assert tags.corners == ["T3", "T7"]


def test_tag_rear_exit():
    tags = tag("rear spins when I pick up the throttle")
    assert (tags.end, tags.phase) == ("rear", "exit")


def test_tag_leaves_ambiguous_fields_empty():
    tags = tag("front unstable on the brakes and rear steps out on exit")
    assert tags.end is None
    assert tags.phase is None
