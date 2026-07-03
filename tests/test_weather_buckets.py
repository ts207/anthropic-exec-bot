from __future__ import annotations

import pytest

from polybot.sources.base import SourceReading
from polybot.strategies.weather import Bucket, parse_bucket_label


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("24°C", Bucket("24°C", 24, 24, "C")),
        ("21°C or below", Bucket("21°C or below", None, 21, "C")),
        ("31°C or higher", Bucket("31°C or higher", 31, None, "C")),
        ("100-101°F", Bucket("100-101°F", 100, 101, "F")),
        ("between 26°C and 28°C", Bucket("between 26°C and 28°C", 26, 28, "C")),
    ],
)
def test_parse_bucket_label(label: str, expected: Bucket) -> None:
    assert parse_bucket_label(label) == expected


@pytest.mark.parametrize("label", ["twenty four", "24", "abc °C", "10-11-12°C"])
def test_parse_bucket_label_rejects_garbage(label: str) -> None:
    with pytest.raises(ValueError):
        parse_bucket_label(label)


def test_bucket_boundaries_are_inclusive() -> None:
    bucket = parse_bucket_label("100-101°F")
    assert bucket.contains(100)
    assert bucket.contains(101)
    assert not bucket.contains(99)
    assert not bucket.contains(102)
