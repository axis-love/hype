"""Tests for Candidate TypedDict + validate_candidate (flow_001168)."""
from __future__ import annotations

import math

import pytest

from newsbot.collectors.base import new_candidate, validate_candidate


def test_required_fields_validated() -> None:
    with pytest.raises(ValueError, match="title"):
        new_candidate(title="", url="http://x.com", source="hn", source_name="HN")
    with pytest.raises(ValueError, match="source"):
        new_candidate(title="T", url="http://x.com", source="", source_name="HN")
    with pytest.raises(ValueError, match="source_name"):
        new_candidate(title="T", url="http://x.com", source="hn", source_name="")


def test_default_values() -> None:
    c = new_candidate(title="T", url="http://x.com", source="hn", source_name="HN")
    assert isinstance(c, dict)
    assert c["score"] == 0.0
    assert c["crosspost_count"] == 1
    assert c["penalty"] == 1.0
    assert c["source_type"] == "hn"
    assert c.get("upvotes") is None
    assert c.get("stars") is None


def test_new_candidate_returns_plain_dict_normalised_source() -> None:
    c = new_candidate(
        title="T", url="http://x.com", source="hackernews", source_name="HN",
    )
    assert type(c) is dict
    assert c["source"] == "hn"
    assert c["source_type"] == "hn"
    assert c["score"] == 0.0
    assert c["penalty"] == 1.0
    assert c["crosspost_count"] == 1


def test_new_candidate_validates_required() -> None:
    with pytest.raises(ValueError):
        new_candidate(title="", url="", source="hn", source_name="HN")


def test_new_candidate_warns_unknown_field() -> None:
    with pytest.raises(ValueError, match="upvoets"):
        new_candidate(
            title="T", url="http://x.com", source="hn", source_name="HN",
            upvoets=100,
        )


def test_empty_url_rejected() -> None:
    with pytest.raises(ValueError, match="url"):
        new_candidate(title="T", url="", source="hn", source_name="HN")


def test_negative_engagement_rejected() -> None:
    with pytest.raises(ValueError, match="upvotes"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvotes=-1)
    with pytest.raises(ValueError, match="comments"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", comments=-5)
    with pytest.raises(ValueError, match="stars"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", stars=-10)


def test_negative_score_rejected() -> None:
    with pytest.raises(ValueError, match="score"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", score=-1.0)


def test_upvote_ratio_range() -> None:
    with pytest.raises(ValueError, match="upvote_ratio"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvote_ratio=1.5)
    with pytest.raises(ValueError, match="upvote_ratio"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvote_ratio=-0.1)
    new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvote_ratio=0.85)


def test_new_candidate_validates_extra_fields() -> None:
    with pytest.raises(ValueError, match="upvotes"):
        new_candidate(
            title="T", url="https://example.com", source="hn", source_name="HN",
            upvotes=-5,
        )


def test_hackernews_alias_normalized_to_hn() -> None:
    c = new_candidate(title="T", url="http://x.com", source="hackernews", source_name="HN")
    assert c["source"] == "hn"


def test_unknown_source_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        new_candidate(title="T", url="http://x.com", source="myspace", source_name="X")


def test_empty_source_rejected() -> None:
    with pytest.raises(ValueError, match="source"):
        new_candidate(title="T", url="http://x.com", source="", source_name="HN")


def test_string_engagement_rejected() -> None:
    with pytest.raises(ValueError, match="upvotes"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvotes="10")  # type: ignore[arg-type]


def test_nan_engagement_rejected() -> None:
    with pytest.raises(ValueError, match="upvotes"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvotes=math.nan)


def test_inf_engagement_rejected() -> None:
    with pytest.raises(ValueError, match="upvotes"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvotes=math.inf)


def test_bool_upvotes_rejected() -> None:
    with pytest.raises(ValueError, match="bool"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvotes=True)  # type: ignore[arg-type]


def test_bool_score_rejected() -> None:
    with pytest.raises(ValueError, match="bool"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", score=True)  # type: ignore[arg-type]


def test_invalid_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="published_at"):
        new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", published_at="not-a-date")


def test_empty_timestamp_allowed() -> None:
    new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", published_at="")


def test_valid_timestamp_accepted() -> None:
    new_candidate(
        title="T", url="https://example.com", source="hn", source_name="HN",
        published_at="2026-08-01T12:00:00+00:00",
    )


def test_engagement_zero_accepted() -> None:
    c = new_candidate(title="T", url="https://example.com", source="hn", source_name="HN", upvotes=0, score=0.0)
    assert c["upvotes"] == 0
    assert c["score"] == 0.0


def test_validate_candidate_same_messages() -> None:
    with pytest.raises(ValueError, match="title"):
        validate_candidate({"title": "", "url": "http://x.com", "source": "hn", "source_name": "HN"})
    with pytest.raises(ValueError, match="source"):
        validate_candidate({"title": "T", "url": "http://x.com", "source": "", "source_name": "HN"})
