"""Guard: Alembic migrations apply cleanly and match the ORM metadata.

Catches migration breakage / model-migration drift in CI (production schema is managed
by Alembic, so this must stay green).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

from degreezeor.config import REPO_ROOT
from degreezeor.core.models import Base


def test_migrations_apply_and_match_models() -> None:
    from alembic import command
    from alembic.config import Config

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "mig.db"
        url = f"sqlite+pysqlite:///{db}"
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")

        engine = create_engine(url)
        migrated = set(inspect(engine).get_table_names())
        engine.dispose()

    model_tables = set(Base.metadata.tables.keys())
    # Every model table is created by the migrations (plus alembic's bookkeeping table).
    missing = model_tables - migrated
    assert not missing, f"migrations missing tables present in models: {missing}"
    assert "alembic_version" in migrated
