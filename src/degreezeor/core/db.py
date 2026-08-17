"""Database engine and session management (SQLAlchemy 2.0, Postgres target)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from degreezeor.config import settings

# `future=True` semantics are default in SQLAlchemy 2.0. We keep echo off; all
# meaningful state changes are recorded in the append-only audit log instead.
# pool_pre_ping + a short recycle + TCP keepalives keep long ingestion runs resilient to
# idle/dropped Postgres connections (e.g. a managed DB recycling a connection mid-run).
_connect_args: dict = {}
if settings.database_url.startswith("postgresql"):
    _connect_args = {
        "keepalives": 1, "keepalives_idle": 30,
        "keepalives_interval": 10, "keepalives_count": 5,
    }
engine = create_engine(
    settings.database_url, pool_pre_ping=True, pool_recycle=280, connect_args=_connect_args
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope. Commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
