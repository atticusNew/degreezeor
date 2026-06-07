"""Append-only, hash-chained audit log.

Every meaningful event (ingest, pre-registration, score run, correction) is
appended as a record whose ``this_hash = sha256(prev_hash + canonical(payload))``.
Any tampering with a historical record breaks the chain from that point forward,
which :func:`verify_chain` detects. This is the spine of auditability: a third
party can replay the chain and confirm nothing was silently altered.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.core.hashing import canonical_json, sha256_hex
from degreezeor.core.models import AuditRecord

GENESIS_HASH = "0" * 64


def _compute_hash(prev_hash: str, actor: str, event_type: str, payload_json: str) -> str:
    return sha256_hex(f"{prev_hash}|{actor}|{event_type}|{payload_json}")


def last_hash(session: Session) -> str:
    row = session.execute(
        select(AuditRecord.this_hash).order_by(AuditRecord.id.desc()).limit(1)
    ).scalar_one_or_none()
    return row or GENESIS_HASH


def append(session: Session, *, event_type: str, payload: Any, actor: str = "system") -> AuditRecord:
    """Append an event to the chain. Returns the persisted record."""
    payload_json = canonical_json(payload)
    prev = last_hash(session)
    this = _compute_hash(prev, actor, event_type, payload_json)
    record = AuditRecord(
        actor=actor,
        event_type=event_type,
        payload_json=payload_json,
        prev_hash=prev,
        this_hash=this,
    )
    session.add(record)
    session.flush()
    return record


def verify_chain(session: Session) -> tuple[bool, int | None]:
    """Replay the chain. Returns (ok, first_broken_id_or_None)."""
    prev = GENESIS_HASH
    records = session.execute(select(AuditRecord).order_by(AuditRecord.id.asc())).scalars()
    for rec in records:
        expected = _compute_hash(prev, rec.actor, rec.event_type, rec.payload_json)
        if expected != rec.this_hash or rec.prev_hash != prev:
            return False, rec.id
        prev = rec.this_hash
    return True, None
