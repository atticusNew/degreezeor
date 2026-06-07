from __future__ import annotations

from sqlalchemy import update

from degreezeor.core import audit
from degreezeor.core.models import AuditRecord


def test_chain_appends_and_verifies(session) -> None:
    audit.append(session, event_type="INGEST", payload={"action": 1})
    audit.append(session, event_type="PREREG", payload={"eu": 1, "metric": "unrate"})
    audit.append(session, event_type="SCORE", payload={"eu": 1, "score": "42.0"})
    session.flush()

    ok, broken = audit.verify_chain(session)
    assert ok is True
    assert broken is None


def test_chain_links_each_record_to_predecessor(session) -> None:
    r1 = audit.append(session, event_type="A", payload={"n": 1})
    r2 = audit.append(session, event_type="B", payload={"n": 2})
    session.flush()
    assert r2.prev_hash == r1.this_hash
    assert r1.prev_hash == audit.GENESIS_HASH


def test_tampering_breaks_the_chain(session) -> None:
    audit.append(session, event_type="A", payload={"n": 1})
    r2 = audit.append(session, event_type="B", payload={"n": 2})
    audit.append(session, event_type="C", payload={"n": 3})
    session.flush()

    # Silently mutate a historical record's payload (an attacker rewriting history).
    session.execute(
        update(AuditRecord).where(AuditRecord.id == r2.id).values(payload_json='{"n":999}')
    )
    session.flush()

    ok, broken = audit.verify_chain(session)
    assert ok is False
    assert broken == r2.id
