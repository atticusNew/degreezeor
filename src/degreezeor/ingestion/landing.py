"""Immutable, content-addressed landing for raw official responses.

Raw bytes are written to ``data/landing/<sha256>`` and recorded in ``raw_landing``
with full provenance, and an INGEST event is appended to the audit chain. Parsing
happens downstream from this anchor, so every derived value can be traced back to
the exact bytes the platform received.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from degreezeor.config import settings
from degreezeor.core import audit
from degreezeor.core.interfaces import RawFetch
from degreezeor.core.models import DataSource, RawLanding


def ensure_source(session: Session, *, name: str, tier: int, base_url: str, license: str | None = None) -> DataSource:
    src = session.execute(select(DataSource).where(DataSource.name == name)).scalar_one_or_none()
    if src is None:
        src = DataSource(name=name, tier=tier, base_url=base_url, license=license)
        session.add(src)
        session.flush()
    return src


def land(session: Session, fetch: RawFetch) -> RawLanding:
    """Persist a raw fetch immutably; return its landing row (idempotent by hash)."""
    src = ensure_source(session, name=fetch.source_name, tier=fetch.tier, base_url=fetch.source_url)

    existing = session.execute(
        select(RawLanding).where(RawLanding.content_hash == fetch.content_hash)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    settings.landing_dir.mkdir(parents=True, exist_ok=True)
    path = settings.landing_dir / fetch.content_hash
    if not path.exists():  # content-addressed => identical bytes only written once
        path.write_bytes(fetch.content)

    row = RawLanding(
        source_id=src.id,
        source_url=fetch.source_url,
        native_identifier=fetch.native_identifier,
        content_hash=fetch.content_hash,
        byte_size=len(fetch.content),
        retrieved_at=fetch.retrieved_at,
        storage_path=str(path),
    )
    session.add(row)
    session.flush()
    audit.append(
        session,
        event_type="INGEST",
        payload={
            "source": fetch.source_name,
            "tier": fetch.tier,
            "url": fetch.source_url,
            "native_identifier": fetch.native_identifier,
            "content_hash": fetch.content_hash,
            "byte_size": len(fetch.content),
        },
    )
    return row
