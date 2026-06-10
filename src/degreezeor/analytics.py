"""First-party, privacy-light usage analytics.

No third-party tracker, no PII, no IP storage: the client mints a random visitor id
(localStorage UUID) and we record (visitor_id, path, timestamp). From that we derive
DAU/WAU/MAU, new-vs-returning, stickiness, retention, and a short daily series — enough
to watch growth without a heavyweight analytics dependency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from degreezeor.core.models import AnalyticsEvent


def record_event(session: Session, *, visitor_id: str, path: str | None) -> bool:
    vid = (visitor_id or "").strip()[:40]
    if not vid:
        return False
    session.add(AnalyticsEvent(visitor_id=vid, path=(path or "")[:200]))
    return True


def forget_visitor(session: Session, *, visitor_id: str) -> int:
    """Delete all events for one anonymous visitor id (used by the per-device opt-out, so the
    owner's own phone/laptop can be excluded — both going forward and retroactively)."""
    from sqlalchemy import delete

    vid = (visitor_id or "").strip()[:40]
    if not vid:
        return 0
    res = session.execute(delete(AnalyticsEvent).where(AnalyticsEvent.visitor_id == vid))
    return res.rowcount or 0


def _distinct_visitors_since(session: Session, since: datetime) -> int:
    return session.execute(
        select(func.count(func.distinct(AnalyticsEvent.visitor_id)))
        .where(AnalyticsEvent.ts >= since)
    ).scalar() or 0


def compute_metrics(session: Session) -> dict[str, Any]:
    now = datetime.now(UTC)
    day, week, month = now - timedelta(days=1), now - timedelta(days=7), now - timedelta(days=30)

    total = session.execute(
        select(func.count(func.distinct(AnalyticsEvent.visitor_id)))
    ).scalar() or 0
    pageviews_30d = session.execute(
        select(func.count()).select_from(AnalyticsEvent).where(AnalyticsEvent.ts >= month)
    ).scalar() or 0
    dau = _distinct_visitors_since(session, day)
    wau = _distinct_visitors_since(session, week)
    mau = _distinct_visitors_since(session, month)

    # First-seen per visitor (cohorting): new = first event within the last 7d.
    first_seen = (
        select(AnalyticsEvent.visitor_id, func.min(AnalyticsEvent.ts).label("first"))
        .group_by(AnalyticsEvent.visitor_id).subquery()
    )
    new_7d = session.execute(
        select(func.count()).select_from(first_seen).where(first_seen.c.first >= week)
    ).scalar() or 0
    # Returning = active in last 7d but first seen earlier than 7d ago.
    active_7d_ids = select(AnalyticsEvent.visitor_id).where(AnalyticsEvent.ts >= week).distinct().subquery()
    returning_7d = session.execute(
        select(func.count()).select_from(first_seen)
        .join(active_7d_ids, active_7d_ids.c.visitor_id == first_seen.c.visitor_id)
        .where(first_seen.c.first < week)
    ).scalar() or 0

    # Day-1 retention: of visitors first seen 2 days ago (a full prior day), how many
    # returned the next day. A simple, honest cohort metric.
    cohort_start = now - timedelta(days=2)
    cohort_end = now - timedelta(days=1)
    cohort_ids = session.execute(
        select(first_seen.c.visitor_id)
        .where(first_seen.c.first >= cohort_start, first_seen.c.first < cohort_end)
    ).scalars().all()
    retained_d1 = 0
    if cohort_ids:
        retained_d1 = session.execute(
            select(func.count(func.distinct(AnalyticsEvent.visitor_id)))
            .where(AnalyticsEvent.visitor_id.in_(cohort_ids), AnalyticsEvent.ts >= cohort_end)
        ).scalar() or 0

    # Short daily series (last 14 days) of distinct visitors, for a growth sparkline.
    daily: list[dict[str, Any]] = []
    for i in range(13, -1, -1):
        d0 = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        d1 = d0 + timedelta(days=1)
        n = session.execute(
            select(func.count(func.distinct(AnalyticsEvent.visitor_id)))
            .where(AnalyticsEvent.ts >= d0, AnalyticsEvent.ts < d1)
        ).scalar() or 0
        daily.append({"date": d0.date().isoformat(), "visitors": n})

    return {
        "total_visitors": total,
        "dau": dau, "wau": wau, "mau": mau,
        "pageviews_30d": pageviews_30d,
        "stickiness_dau_mau": round(dau / mau, 3) if mau else 0.0,
        "new_visitors_7d": new_7d,
        "returning_visitors_7d": returning_7d,
        "day1_retention_cohort": len(cohort_ids),
        "day1_retention_returned": retained_d1,
        "day1_retention_rate": round(retained_d1 / len(cohort_ids), 3) if cohort_ids else 0.0,
        "daily_visitors_14d": daily,
    }
