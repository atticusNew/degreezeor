"""First-party usage analytics: anonymous event recording + DAU/WAU/MAU/retention math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from degreezeor.analytics import compute_metrics, forget_visitor, record_event
from degreezeor.core.models import AnalyticsEvent


def test_record_event_rejects_empty_visitor(session) -> None:
    assert record_event(session, visitor_id="", path="#/") is False
    assert record_event(session, visitor_id="v1", path="#/officials") is True


def test_metrics_count_distinct_visitors_and_pageviews(session) -> None:
    now = datetime.now(UTC)
    # 3 distinct visitors today (a active twice), 1 visitor active only 20 days ago.
    for vid, when in [("a", now), ("a", now), ("b", now), ("c", now),
                      ("old", now - timedelta(days=20))]:
        session.add(AnalyticsEvent(visitor_id=vid, path="#/", ts=when))
    session.flush()
    m = compute_metrics(session)
    assert m["total_visitors"] == 4
    assert m["dau"] == 3            # a, b, c today
    assert m["mau"] == 4            # + old within 30 days
    assert m["pageviews_30d"] == 5  # all five events
    assert 0.0 <= m["stickiness_dau_mau"] <= 1.0
    assert len(m["daily_visitors_14d"]) == 14


def test_forget_visitor_removes_own_device_from_unique_visitors(session) -> None:
    for vid in ("owner", "owner", "real_user"):
        record_event(session, visitor_id=vid, path="#/")
    session.flush()
    assert compute_metrics(session)["total_visitors"] == 2
    deleted = forget_visitor(session, visitor_id="owner")
    session.flush()
    assert deleted == 2  # both of the owner's events removed
    assert compute_metrics(session)["total_visitors"] == 1  # only the real user remains
