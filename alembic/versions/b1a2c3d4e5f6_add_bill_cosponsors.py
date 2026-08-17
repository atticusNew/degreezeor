"""add bill_cosponsors (activity/record layer)

Revision ID: b1a2c3d4e5f6
Revises: 8c5a96609f70
Create Date: 2026-06-08

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1a2c3d4e5f6"
down_revision: str | Sequence[str] | None = "8c5a96609f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bill_cosponsors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=False),
        sa.Column("official_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["action_id"], ["actions.id"]),
        sa.ForeignKeyConstraint(["official_id"], ["officials.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id", "official_id", name="uq_bill_cosponsor"),
    )
    op.create_index("ix_bill_cosponsors_action_id", "bill_cosponsors", ["action_id"])
    op.create_index("ix_bill_cosponsors_official_id", "bill_cosponsors", ["official_id"])


def downgrade() -> None:
    op.drop_index("ix_bill_cosponsors_official_id", table_name="bill_cosponsors")
    op.drop_index("ix_bill_cosponsors_action_id", table_name="bill_cosponsors")
    op.drop_table("bill_cosponsors")
