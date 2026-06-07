"""Dispute workflow: filing is recorded immutably; the public-diff logic is correct."""

from __future__ import annotations

from datetime import date

from degreezeor.core import audit
from degreezeor.core.models import Action, EvaluationUnit
from degreezeor.disputes import _diff, file_dispute


def _seed_eu(session) -> int:
    action = Action(type="law", title="Test Act", action_date=date(2010, 1, 1),
                    source_id=1, source_url="https://x", native_identifier="PLX")
    session.add(action)
    session.flush()
    eu = EvaluationUnit(action_id=action.id, status="scored")
    session.add(eu)
    session.flush()
    return eu.id


def test_filing_creates_dispute_and_audit_record(session) -> None:
    eu_id = _seed_eu(session)
    d = file_dispute(session, eu_id=eu_id, filer="jane@example.org", claim="Baseline looks wrong.")
    session.flush()
    assert d.status == "open"
    # The filing is recorded on the append-only, tamper-evident audit chain.
    ok, broken = audit.verify_chain(session)
    assert ok and broken is None
    from sqlalchemy import select

    from degreezeor.core.models import AuditRecord
    events = session.execute(select(AuditRecord.event_type)).scalars().all()
    assert "DISPUTE_FILED" in events


def test_filing_unknown_eu_raises(session) -> None:
    import pytest

    with pytest.raises(ValueError):
        file_dispute(session, eu_id=999, filer="x", claim="y")


def test_diff_detects_no_change() -> None:
    snap = {"reproducible_hash": "abc", "composite": "0.30", "confidence": "0.69",
            "gated": False, "components": {"outcome": "0.9", "durability": "0.0"}}
    assert _diff(snap, dict(snap)) == {}


def test_diff_detects_changes() -> None:
    before = {"reproducible_hash": "abc", "composite": "0.30", "confidence": "0.69",
              "gated": False, "components": {"outcome": "0.9"}}
    after = {"reproducible_hash": "xyz", "composite": "0.55", "confidence": "0.69",
             "gated": False, "components": {"outcome": "1.4"}}
    d = _diff(before, after)
    assert d["composite"] == {"before": "0.30", "after": "0.55"}
    assert d["reproducible_hash"]["after"] == "xyz"
    assert d["components"]["outcome"] == {"before": "0.9", "after": "1.4"}
    assert "confidence" not in d  # unchanged
