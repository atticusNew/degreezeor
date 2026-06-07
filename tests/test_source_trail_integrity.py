"""Source-trail integrity (PLAN.md §15 Phase-1 CI gate).

Every entry in a scorecard's source trail must resolve to a stored immutable
landing with a valid sha256 content hash, and the action's own record must always
be present. This is the automated form of "audit any number back to its bytes".
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from degreezeor.api.presentation import build_scorecard
from degreezeor.core.hashing import sha256_hex
from degreezeor.core.models import (
    Action,
    DataSource,
    EvaluationUnit,
    Law,
    Metric,
    Objective,
    RawLanding,
)

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _seed_scored_eu(session) -> int:
    congress = DataSource(name="Congress.gov", tier=0, base_url="https://api.congress.gov/v3")
    bls = DataSource(name="BLS", tier=1, base_url="https://api.bls.gov")
    session.add_all([congress, bls])
    session.flush()

    law_url = "https://api.congress.gov/v3/law/111/pub/5?format=json"
    sum_url = "https://api.congress.gov/v3/bill/111/hr/1/summaries?format=json"
    bls_url = "https://api.bls.gov/publicAPI/v2/timeseries/data/CES0000000001?startyear=2006&endyear=2013"
    # Landings for THIS EU (law, summary, in-window series) + a decoy from another EU/window.
    for src, url, nid, content in [
        (congress, law_url, "law/111/pub/5", b"law-bytes"),
        (congress, sum_url, "bill/111/hr/1/summaries", b"summary-bytes"),
        (bls, bls_url, "CES0000000001", b"series-2006-2013"),
        (bls, "https://api.bls.gov/publicAPI/v2/timeseries/data/CES0000000001?startyear=2018&endyear=2025",
         "CES0000000001", b"series-2018-2025-OTHER-EU"),
    ]:
        session.add(RawLanding(
            source_id=src.id, source_url=url, native_identifier=nid,
            content_hash=sha256_hex(content), byte_size=len(content),
            retrieved_at=datetime.now(UTC), storage_path=f"/tmp/{sha256_hex(content)}",
        ))
    session.flush()

    action = Action(type="law", title="Test Act", action_date=date(2009, 2, 17),
                    source_id=congress.id, source_url=law_url, native_identifier="PL111-5",
                    content_hash=sha256_hex(b"law-bytes"), domain="Economics and Public Finance",
                    implemented=True)
    session.add(action)
    session.flush()
    session.add(Law(action_id=action.id, public_law_number="111-5", enacted_date=date(2009, 2, 17)))
    obj = Objective(action_id=action.id, text="create jobs", source_id=congress.id,
                    source_url=sum_url, objective_level="agency")
    session.add(obj)
    metric = Metric(code="nonfarm_employment", name="Nonfarm", unit="thousands",
                    direction_good="up", source_id=bls.id, native_series_id="CES0000000001")
    session.add(metric)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, objective_id=obj.id, metric_id=metric.id,
                        lag_window_months=24, sign_goal=1, status="scored")
    session.add(eu)
    session.flush()
    return eu.id


def test_every_source_trail_entry_resolves_with_valid_hash(session) -> None:
    eu_id = _seed_scored_eu(session)
    card = build_scorecard(session, eu_id)
    assert card is not None

    landing_hashes = {row.content_hash for row in session.query(RawLanding).all()}
    trail = card["source_trail"]
    assert trail, "source trail must not be empty for a scored EU"
    for entry in trail:
        assert SHA256_RE.match(entry["content_hash"]), f"invalid hash: {entry['content_hash']}"
        assert entry["content_hash"] in landing_hashes, "trail entry must resolve to a stored landing"
        assert entry["source_url"].startswith("https://"), "every source must be an official URL"


def test_action_record_is_always_in_the_trail(session) -> None:
    eu_id = _seed_scored_eu(session)
    card = build_scorecard(session, eu_id)
    urls = {e["source_url"] for e in card["source_trail"]}
    assert card["action"]["source_url"] in urls


def test_trail_is_window_scoped_no_foreign_eu_series(session) -> None:
    eu_id = _seed_scored_eu(session)
    card = build_scorecard(session, eu_id)
    urls = {e["source_url"] for e in card["source_trail"]}
    # The 2018-2025 series snapshot belongs to a different EU/window and must be excluded.
    assert not any("startyear=2018" in u for u in urls)
    assert any("startyear=2006" in u for u in urls)
