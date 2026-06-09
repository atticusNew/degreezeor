"""add analytics_events (first-party usage metrics)

Revision ID: e4f5a6b7c8d9
Revises: d3c4e5f6a7b8
Create Date: 2026-06-09

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "d3c4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("visitor_id", sa.String(length=40), nullable=False),
        sa.Column("path", sa.String(length=200), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analytics_events_visitor_id", "analytics_events", ["visitor_id"])
    op.create_index("ix_analytics_events_ts", "analytics_events", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_analytics_events_ts", table_name="analytics_events")
    op.drop_index("ix_analytics_events_visitor_id", table_name="analytics_events")
    op.drop_table("analytics_events")
