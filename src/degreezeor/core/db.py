"""Database engine and session management (SQLAlchemy 2.0, Postgres target)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from degreezeor.config import settings

# `future=True` semantics are default in SQLAlchemy 2.0. We keep echo off; all
# meaningful state changes are recorded in the append-only audit log instead.
engine = create_engine(settings.database_url, pool_pre_ping=True)
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
