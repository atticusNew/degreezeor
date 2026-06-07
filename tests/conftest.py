"""Test fixtures.

Unit tests run against an in-memory SQLite database to stay fast and dependency-free.
Because the ORM uses only portable column types, the same schema runs on SQLite and
Postgres — itself a guard against accidental Postgres-only dead-ends. The end-to-end
pipeline test runs against the real Postgres target.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from degreezeor.core.models import Base


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    s = maker()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()
